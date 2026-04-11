# API 設計

CLI はこの API を呼ぶ薄いクライアントとして実装する。
全エンドポイントで ServiceAccount JWT による認証・認可を行う（詳細は [auth_policy.md](../auth_policy.md) 参照）。

## 1. 共通エラーレスポンス仕様

全エンドポイントで共通して発生しうるエラーを以下に定義する。

| HTTP ステータス | 発生条件 | レスポンスボディ例 |
|---|---|---|
| 401 | JWT が無効・期限切れ・存在しない | `{ "detail": "Unauthorized" }` |
| 403 | namespace に CJob ユーザー設定（`cjob.io/username` annotation）がない | `{ "detail": "Namespace is not configured as a CJob user namespace" }` |
| 404 | 存在しない job_id、または他ユーザーの job_id | `{ "detail": "Job not found" }` |
| 409 | リセット処理中（`DELETING` ジョブが存在する namespace への投入） | `{ "detail": "リセット処理中のためジョブを投入できません。しばらく待ってから再試行してください" }` |
| 429 | namespace 内の active ジョブ数が `MAX_QUEUED_JOBS_PER_NAMESPACE` に到達（POST /v1/jobs, POST /v1/sweep） | `{ "detail": "投入可能なジョブ数の上限（<MAX_QUEUED_JOBS_PER_NAMESPACE>件）に達しています" }` |
| 500 | namespace の読み取り失敗など内部エラー | `{ "detail": "Internal server error" }` |
| 503 | DB 書き込み失敗など内部サービス一時不可 | `{ "detail": "Service temporarily unavailable" }` |

**404 の方針**：他ユーザーのジョブへのアクセスも 404 を返す。ジョブの存在自体を隠すことで情報漏洩を防ぐ。

**401 の方針**：TokenReview が失敗した場合（JWT 無効・期限切れ）に返す。レスポンスボディは固定文字列とし、詳細なエラー原因は含めない。

**403 の方針**：JWT 認証は成功したが、namespace に `cjob.io/username` annotation が設定されておらず CJob ユーザーとして認識できない場合に返す。

**レート制限の方針**：Submit API は各リクエストで K8s TokenReview API を呼ぶため、大量リクエストは K8s API サーバへの負荷につながりうる。ただし Submit API 自身の CPU/memory limit（500m / 512Mi）が事実上のスループット上限として機能するため、現在の規模（10 名程度）においては明示的なレート制限は不要と判断する。ユーザー数が数十名以上に拡大する場合は `slowapi` 等による namespace ごとのレート制限を検討すること。

## 2. POST /v1/jobs

ジョブを1件投入する。

### request

```json
{
  "command": "python main.py --alpha 0.1 --beta 16",
  "image": "your-registry/cjob-jupyter:2.1.0",
  "cwd": "/home/jovyan/project-a/exp1",
  "env": {
    "OMP_NUM_THREADS": "4",
    "PYTHONPATH": "/home/jovyan/project-a"
  },
  "resources": {
    "cpu": "2",
    "memory": "4Gi",
    "gpu": 0,
    "flavor": "cpu"
  },
  "time_limit_seconds": 3600
}
```

`resources` 内の各フィールドは省略可能。省略時のデフォルト値:

| フィールド | デフォルト | 説明 |
|---|---|---|
| `cpu` | `"1"` | CPU リソース |
| `memory` | `"1Gi"` | メモリリソース |
| `gpu` | `0` | GPU 数 |
| `flavor` | サーバ側デフォルト（ConfigMap: `DEFAULT_FLAVOR`、デフォルト `cpu`） | ResourceFlavor 名 |

### response (201 Created)

```json
{
  "job_id": 1,
  "status": "QUEUED"
}
```

### バリデーション

`resources.flavor` が `RESOURCE_FLAVORS` に存在しない場合は 400 を返す。

```json
{ "detail": "指定された flavor 'xxx' は存在しません。利用可能な flavor: cpu, gpu-a100" }
```

要求リソース（CPU / メモリ）が指定 flavor の有効上限を超える場合は 400 を返す。
有効上限は `min(最大ノード allocatable, nominalQuota)` で決定される。
単一ノードに収まらないジョブ、またはクォータを超えるジョブは Kueue が受理できず、DISPATCHED 状態のまま無期限に滞留することを防ぐ。
指定 flavor のノードが `node_resources` テーブルに存在しない場合（Watcher 未起動等）はこのバリデーションをスキップする。
`flavor_quotas` テーブルにデータがない場合は最大ノード allocatable のみでバリデーションする。

```json
{ "detail": "要求 CPU (128) が flavor 'cpu' の最大ノード (64000m) を超えています" }
```

```json
{ "detail": "要求メモリ (2Ti) が flavor 'cpu' の最大ノード (262144Mi) を超えています" }
```

```json
{ "detail": "要求 CPU (128) が flavor 'cpu' のクォータ (64000m) を超えています" }
```

```json
{ "detail": "要求メモリ (2Ti) が flavor 'cpu' のクォータ (262144Mi) を超えています" }
```

`resources.gpu > 0` の場合、指定 flavor の `gpu_resource_name` が未設定（GPU なし flavor）なら 400 を返す。GPU ノードが登録されていなければ 400 を返す。要求 GPU が flavor の有効上限（`min(最大ノード GPU, nominalQuota GPU)`）を超える場合も 400 を返す。

```json
{ "detail": "flavor 'cpu' は GPU をサポートしていません" }
```

```json
{ "detail": "flavor 'gpu-a100' に GPU ノードが登録されていません" }
```

```json
{ "detail": "要求 GPU (8) が flavor 'gpu-a100' の最大ノード (4) を超えています" }
```

```json
{ "detail": "要求 GPU (8) が flavor 'gpu-a100' のクォータ (4) を超えています" }
```

`time_limit_seconds` は省略可能。省略時はサーバ側デフォルト（ConfigMap: `DEFAULT_TIME_LIMIT_SECONDS`、デフォルト 86400 = 24時間）を使用する。
`MAX_TIME_LIMIT_SECONDS`（デフォルト 604800 = 7日）を超える値を指定した場合は 400 を返す。0 以下の値を指定した場合も 400 を返す。

```json
{ "detail": "time_limit_seconds は 604800 秒（7日）以下で指定してください" }
```

```json
{ "detail": "time_limit_seconds は 1 以上で指定してください" }
```

`command` が空文字の場合は 400 を返す。

```json
{ "detail": "command は空にできません" }
```

`image` が空文字の場合は 400 を返す。

```json
{ "detail": "image は空にできません" }
```

namespace のジョブ総数（QUEUED / DISPATCHING / DISPATCHED / HELD / CANCELLED の合計）が
`MAX_QUEUED_JOBS_PER_NAMESPACE`（デフォルト 500）に達している場合は 429 を返す。
RUNNING は `DISPATCH_BUDGET_PER_NAMESPACE`（flavor ごと）で上限が管理されており、無制限に蓄積しないためカウント対象外とする。DISPATCHING / DISPATCHED も同様に budget で制限されるが、ステータス遷移の過渡期に一時的に存在する状態であり滞留時間が短いため、現時点ではカウント対象に含める。
CANCELLED ジョブを含めることで、cancel → 再投入の無制限サイクルによる DB 肥大化を防ぐ。
上限に達した場合は `cjob delete` で CANCELLED ジョブを削除してから再投入すること。

```json
{ "detail": "投入可能なジョブ数の上限（500件）に達しています" }
```

namespace に `DELETING` 状態のジョブが1件でも存在する場合は 409 を返す（リセット処理中）。

```json
{ "detail": "リセット処理中のためジョブを投入できません。しばらく待ってから再試行してください" }
```

## 2.1 POST /v1/sweep

パラメータスイープジョブを 1 件投入する。K8s Indexed Job として実行され、各タスクは `$CJOB_INDEX`（0-origin）で識別される。

`parallelism` は省略可能で、デフォルトは 1。

CLI が受け付ける `_INDEX_` プレースホルダーは CLI クライアント側で `$CJOB_INDEX` に置換されてから本 API に送信される（[cli.md](cli.md) §3、[dispatcher.md](dispatcher.md) §3.3.1 参照）。したがって本 API のリクエストでは常に `$CJOB_INDEX` 形式の置換済みコマンドを受け取る。

### request

```json
{
  "command": "python main.py --trial $CJOB_INDEX",
  "image": "your-registry/cjob-jupyter:2.1.0",
  "cwd": "/home/jovyan/project-a",
  "env": {
    "OMP_NUM_THREADS": "4"
  },
  "resources": {
    "cpu": "2",
    "memory": "4Gi",
    "gpu": 0,
    "flavor": "cpu"
  },
  "completions": 100,
  "parallelism": 10,
  "time_limit_seconds": 21600
}
```

### response (201 Created)

```json
{
  "job_id": 3,
  "status": "QUEUED"
}
```

### バリデーション

`POST /v1/jobs` と共通のバリデーション（単一ノードリソース超過、GPU バリデーション、time_limit、ジョブ数上限、DELETING チェック）に加え、以下の sweep 固有バリデーションを行う。

- `completions` が 1 未満 → 400
- `completions` が `MAX_SWEEP_COMPLETIONS`（デフォルト 1000）を超える → 400
- `parallelism` が 1 未満 → 400
- `parallelism` が `completions` を超える → 400
- `parallelism × Pod リソース` が指定 flavor の有効上限（`min(allocatable 合計, nominalQuota)`）を超える → 400

```json
{ "detail": "completions は 1 以上 1000 以下で指定してください" }
```

```json
{ "detail": "parallelism は 1 以上 completions 以下で指定してください" }
```

```json
{ "detail": "parallelism × 要求 CPU (20000m) が flavor 'cpu' の CPU 合計 (256000m) を超えています" }
```

```json
{ "detail": "parallelism × 要求 GPU (8) が flavor 'gpu-a100' の GPU 合計 (4) を超えています" }
```

## 3. GET /v1/jobs

ジョブ一覧を取得する。JWT の namespace に属するジョブのみ返す。

### クエリパラメータ

| パラメータ | 型 | 省略時の挙動 |
|---|---|---|
| `status` | 文字列（任意） | 全ステータスを返す |
| `flavor` | 文字列（任意） | 全 flavor を返す |
| `time_limit_ge` | 整数（任意、秒） | フィルタしない |
| `time_limit_lt` | 整数（任意、秒） | フィルタしない |
| `limit` | 整数（任意） | 全件返す（注: CLI はデフォルトで `limit=50` を送信する。API を直接利用する場合は適切な `limit` の指定を推奨） |
| `order` | 文字列（`"asc"` or `"desc"`） | `"asc"`（JOB_ID 昇順） |

`time_limit_ge` / `time_limit_lt` は `time_limit_seconds` の範囲フィルタ。`time_limit_ge` は「以上」、`time_limit_lt` は「未満」。両方指定した場合は AND 条件。

`limit` 指定時は常に最新（JOB_ID が大きい）N 件を選択し、`order` に応じてソートして返す。

```
GET /v1/jobs
GET /v1/jobs?status=RUNNING
GET /v1/jobs?status=FAILED&limit=10
GET /v1/jobs?limit=50&order=desc
GET /v1/jobs?flavor=gpu-a100
GET /v1/jobs?status=QUEUED&time_limit_ge=21600
GET /v1/jobs?time_limit_lt=43200
GET /v1/jobs?time_limit_ge=21600&time_limit_lt=43200
```

### response

```json
{
  "jobs": [
    {
      "job_id": 1,
      "status": "RUNNING",
      "flavor": "cpu",
      "command": "python main.py --alpha 0.1 --beta 16",
      "created_at": "2026-03-23T12:34:56Z",
      "finished_at": null,
      "time_limit_seconds": 86400,
      "completions": null,
      "parallelism": null,
      "succeeded_count": null,
      "failed_count": null
    }
  ],
  "total_count": 1,
  "log_base_dir": "/home/jovyan/.cjob/logs"
}
```

`total_count` はフィルタ条件に一致するジョブの総数（`limit` 適用前）を返す。

`log_base_dir` はサーバー側の ConfigMap で設定されたログベースディレクトリ（`LOG_BASE_DIR`）を返す。CLI は reset 時のログ一括削除にこの値を使用する。

## 4. GET /v1/jobs/{job_id}

個別ジョブの詳細を取得する。

### response

```json
{
  "job_id": 1,
  "status": "SUCCEEDED",
  "namespace": "user-alice",
  "command": "python main.py --alpha 0.1 --beta 16",
  "cwd": "/home/jovyan/project-a/exp1",
  "cpu": "2",
  "memory": "4Gi",
  "gpu": 0,
  "flavor": "cpu",
  "time_limit_seconds": 86400,
  "k8s_job_name": "cjob-alice-1",
  "log_dir": "/home/jovyan/.cjob/logs/1",
  "created_at": "2026-03-23T12:34:56Z",
  "dispatched_at": "2026-03-23T12:35:02Z",
  "started_at": "2026-03-23T12:35:10Z",
  "finished_at": "2026-03-23T12:37:10Z",
  "last_error": null,
  "completions": null,
  "parallelism": null,
  "succeeded_count": null,
  "failed_count": null,
  "completed_indexes": null,
  "failed_indexes": null,
  "node_name": ["worker07", "worker08"]
}
```

`node_name` はジョブの実行に使用されたノード名のリスト（`list[str] | null`）。Watcher が RUNNING 遷移時および sweep の進行状況変化時に累積記録する。QUEUED / DISPATCHED 等の未実行ジョブでは `null`。通常ジョブでは単一要素のリスト、sweep ジョブでは使用された全ノード名のリストとなる。

### エラーレスポンス

存在しない job_id または他ユーザーの job_id の場合は 404 を返す。

```json
{ "detail": "Job not found" }
```

## 5. POST /v1/jobs/{job_id}/cancel

ジョブをキャンセルする。

| 状態 | API の処理 |
|---|---|
| `QUEUED` | DB を `CANCELLED` に更新する。Dispatcher が次回スキャン時に `CANCELLED` ならスキップする |
| `HELD` | DB を `CANCELLED` に更新する。K8s Job は未作成のため削除不要 |
| `DISPATCHING` | DB を `CANCELLED` に更新する。CAS 更新の前にキャンセルが行われた場合は Dispatcher がスキップする。CAS 更新の後にキャンセルが行われた場合は K8s Job が作成されるが、Watcher が定期監視時に `CANCELLED` ジョブの K8s Job を削除する（`DISPATCHED` / `RUNNING` と同じ経路） |
| `DISPATCHED` / `RUNNING` | DB を `CANCELLED` に更新する。Watcher が定期監視時に `CANCELLED` ジョブの K8s Job を削除する |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | 変更不要。`skipped` として返す |
| `DELETING` | reset 処理中のため変更不要。`skipped` として返す |

K8s Job 削除後、Watcher は DB の status が `CANCELLED` であることを確認した上で状態を維持する（`FAILED` に遷移させない）。

### response

```json
{
  "job_id": 1,
  "status": "CANCELLED"
}
```

### エラーレスポンス

存在しない job_id または他ユーザーの job_id の場合は 404 を返す。

```json
{ "detail": "Job not found" }
```

## 6. POST /v1/jobs/cancel

複数ジョブを一括キャンセルする。範囲指定・個別複数指定はCLI側で展開してから送る。

### request

```json
{
  "job_ids": [1, 2, 3, 4, 5]
}
```

### response

```json
{
  "cancelled":  [1, 2, 3],
  "skipped":    [4, 5],
  "not_found":  []
}
```

`skipped` は対象ジョブがすでに SUCCEEDED / FAILED / CANCELLED / DELETING の場合。

## 7. POST /v1/jobs/{job_id}/hold

ジョブを保留にする。保留中のジョブは Dispatcher の dispatch 対象から除外される。

| 状態 | API の処理 |
|---|---|
| `QUEUED` | DB を `HELD` に更新する。Dispatcher は `status = 'QUEUED'` のジョブのみ取得するため、`HELD` ジョブは自動的にスキップされる |
| `DISPATCHING` / `DISPATCHED` / `RUNNING` | 既にディスパッチ処理中または実行中のため保留不可。`skipped` として返す |
| `HELD` | 既に保留中。`skipped` として返す |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | 完了済み。`skipped` として返す |
| `DELETING` | reset 処理中。`skipped` として返す |

### response

```json
{
  "job_id": 1,
  "status": "HELD"
}
```

### エラーレスポンス

存在しない job_id または他ユーザーの job_id の場合は 404 を返す。

```json
{ "detail": "Job not found" }
```

## 8. POST /v1/jobs/hold

複数ジョブを一括保留にする。範囲指定・個別複数指定は CLI 側で展開してから送る。

`job_ids` を省略した場合（`--all` 相当）は namespace 内の全 `QUEUED` ジョブを保留対象とする。

### request（個別指定）

```json
{
  "job_ids": [1, 2, 3, 4, 5]
}
```

### request（全件保留）

```json
{}
```

### response

```json
{
  "held":      [1, 2, 3],
  "skipped":   [4, 5],
  "not_found": []
}
```

`skipped` は対象ジョブが QUEUED 以外の状態（HELD / DISPATCHING / DISPATCHED / RUNNING / SUCCEEDED / FAILED / CANCELLED / DELETING）の場合。

## 9. POST /v1/jobs/{job_id}/release

保留中のジョブをキューに戻す。

| 状態 | API の処理 |
|---|---|
| `HELD` | DB を `QUEUED` に更新する。Dispatcher が次回スキャン時に dispatch 対象として選択する |
| `QUEUED` / `DISPATCHING` / `DISPATCHED` / `RUNNING` | 保留中ではない。`skipped` として返す |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | 完了済み。`skipped` として返す |
| `DELETING` | reset 処理中。`skipped` として返す |

### response

```json
{
  "job_id": 1,
  "status": "QUEUED"
}
```

### エラーレスポンス

存在しない job_id または他ユーザーの job_id の場合は 404 を返す。

```json
{ "detail": "Job not found" }
```

## 10. POST /v1/jobs/release

複数ジョブの保留を一括解除する。範囲指定・個別複数指定は CLI 側で展開してから送る。

`job_ids` を省略した場合（`--all` 相当）は namespace 内の全 `HELD` ジョブを解除対象とする。

### request（個別指定）

```json
{
  "job_ids": [1, 2, 3, 4, 5]
}
```

### request（全件解除）

```json
{}
```

### response

```json
{
  "released":  [1, 2, 3],
  "skipped":   [4, 5],
  "not_found": []
}
```

`skipped` は対象ジョブが HELD 以外の状態の場合。

## 11. POST /v1/jobs/{job_id}/set

QUEUED / HELD ジョブのパラメータを変更する。指定されたフィールドのみ更新し、未指定のフィールドは現在値を維持する。

| 状態 | API の処理 |
|---|---|
| `QUEUED` | 指定パラメータを更新する。Dispatcher が次回スキャン時に更新後の値で dispatch する |
| `HELD` | 指定パラメータを更新する。release 後に更新後の値で dispatch される |
| `DISPATCHING` / `DISPATCHED` / `RUNNING` | K8s Job が作成済みのため変更不可。`skipped` として返す |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | 完了済み。`skipped` として返す |
| `DELETING` | reset 処理中。`skipped` として返す |

### request

```json
{
  "flavor": "cpu-sub",
  "cpu": "4",
  "memory": "16Gi",
  "time_limit_seconds": 43200
}
```

全フィールドは省略可能。ただし、1つ以上のフィールドを指定する必要がある。全フィールドが省略された場合は 400 を返す。

| フィールド | 型 | 説明 |
|---|---|---|
| `cpu` | `string \| null` | CPU リソース（例: `"2"`, `"500m"`） |
| `memory` | `string \| null` | メモリリソース（例: `"4Gi"`, `"500Mi"`） |
| `gpu` | `int \| null` | GPU 数 |
| `flavor` | `string \| null` | ResourceFlavor 名 |
| `time_limit_seconds` | `int \| null` | 実行時間上限（秒） |

### response

```json
{
  "job_id": 1,
  "status": "QUEUED"
}
```

`status` はジョブの現在のステータス。変更成功時は `QUEUED` または `HELD`、`skipped` 時は実際のステータスを返す。

### バリデーション

バリデーションは指定フィールドと未指定フィールドの現在値をマージした状態に対して行う。バリデーションルールは §2（POST /v1/jobs）と同じ（flavor 存在チェック、CPU/メモリ上限、GPU 互換性、time_limit 範囲）。

```json
{ "detail": "変更するパラメータを1つ以上指定してください" }
```

### エラーレスポンス

存在しない job_id または他ユーザーの job_id の場合は 404 を返す。

```json
{ "detail": "Job not found" }
```

### Race condition 対策

Dispatcher の CAS（`UPDATE ... WHERE status = 'QUEUED'`）との競合を防ぐため、パラメータ更新は `WHERE status IN ('QUEUED', 'HELD')` 条件付きの UPDATE で行う。status が変更されていた場合（rowcount = 0）は `skipped` として返す。

## 12. POST /v1/jobs/set

複数ジョブのパラメータを一括変更する。全ジョブに同じ変更を適用する。範囲指定・個別複数指定は CLI 側で展開してから送る。

### request

```json
{
  "job_ids": [1, 2, 3],
  "flavor": "cpu-sub",
  "cpu": "4"
}
```

### response

```json
{
  "modified":  [1, 2],
  "skipped":   [3],
  "not_found": []
}
```

`skipped` は対象ジョブが QUEUED / HELD 以外の状態の場合。バリデーションエラーは各ジョブで独立して発生するため、一部のジョブが成功し一部が失敗する可能性がある。ただし、全ジョブに同じパラメータを適用するため、バリデーションエラーが発生する場合は通常すべてのジョブで発生する。

## 13. POST /v1/jobs/delete

完了済みジョブを削除する。範囲指定・個別複数指定は CLI 側で展開してから送る。

CANCELLED / SUCCEEDED / FAILED 状態のジョブのみ削除対象とする。
QUEUED / DISPATCHING / DISPATCHED / RUNNING / HELD 状態のジョブは削除せず `skipped` として返す。
DELETING 状態のジョブは Watcher によるリセットクリーンアップが進行中のため削除せず `skipped` として返す。

`job_ids` を省略した場合（`--all` 相当）は namespace 内の全完了済みジョブを削除対象とする。
カウンタのリセットは行わない。

### request（個別指定）

```json
{
  "job_ids": [1, 2, 3]
}
```

### request（全件削除）

```json
{}
```

### response

```json
{
  "deleted":   [1, 2],
  "log_dirs":  ["/home/jovyan/.cjob/logs/1", "/home/jovyan/.cjob/logs/2"],
  "skipped":   [
    { "job_id": 3, "reason": "running" },
    { "job_id": 4, "reason": "deleting" }
  ],
  "not_found": []
}
```

`log_dirs` は削除されたジョブの `log_dir` をリストで返す。CLI はこの値を使って PVC 上のログディレクトリを削除する。`deleted` と同じ順序で対応する。

`skipped` は対象ジョブが QUEUED / DISPATCHING / DISPATCHED / RUNNING / HELD / DELETING の場合。
`reason` の値は `"running"`（QUEUED / DISPATCHING / DISPATCHED / RUNNING）、`"held"`（HELD）、`"deleting"`（DELETING）の3種類。
CLI は `reason` に基づいてメッセージを分岐する。QUEUED / DISPATCHING / DISPATCHED / RUNNING の場合は先に `cjob cancel` するよう促し、HELD の場合は `cjob release` または `cjob cancel` するよう促し、DELETING の場合はリセット処理中である旨を表示する（[cli.md](cli.md) §10 参照）。

**§6（cancel）との設計上の違い：**
§6 の `skipped` は「すでに終了済み・処理済み」という単一の意味しか持たないため job_id のフラットなリストで十分である。一方 §11 の `skipped` は「実行中（cancel を促すべき）」と「保留中（release か cancel を促すべき）」と「DELETING（何もできない）」で CLI が取るべきアクションが根本的に異なる。また、CLI が事前に `GET /v1/jobs/{job_id}` で状態を確認してから分岐する方式はレース条件を生じさせるため採用しない。`reason` をレスポンスに含めることで、スキップ判定と理由取得を原子的に行える。

## 14. POST /v1/reset

ユーザーの全ジョブ履歴をリセットし、job_id の採番を 1 に戻す。

リセット可能条件：全ジョブが CANCELLED / SUCCEEDED / FAILED のいずれかであること。
以下のいずれかに該当する場合は 409 を返す。

- QUEUED / DISPATCHING / DISPATCHED / RUNNING / HELD のジョブが1件でも存在する（未完了ジョブあり）
- DELETING のジョブが1件でも存在する（前回の reset 処理がまだ完了していない）

条件を満たした場合、Submit API は全ジョブのステータスを `DELETING` に変更して即座に返す。
実際の K8s Job 削除・DB レコード削除・カウンターリセットは Watcher が非同期で実行する。
そのため、レスポンスが返った時点ではリセットはまだ完了していない。

job_id カウンターのリセット（`next_id = 1`）は Watcher が全 `DELETING` レコードの処理を完了した後に行う。
これにより reset 完了前に新規ジョブを投入しても job_id=1 は発行されず、K8s Job 名の衝突が起きない。

### response（成功時・202 Accepted）

```json
{
  "status": "accepted"
}
```

### response（実行中ジョブあり・409）

```json
{
  "message": "完了していないジョブがあるためリセットできません",
  "blocking_job_ids": [3, 7, 12]
}
```

### response（リセット処理進行中・409）

```json
{
  "message": "リセット処理が進行中のため再実行できません。しばらく待ってから再試行してください"
}
```

## 15. GET /v1/usage

自身の namespace の直近 `FAIR_SHARE_WINDOW_DAYS` 日分のリソース使用状況を取得する。`namespace_daily_usage` テーブルから日別の消費量を集計して返す。

### response

```json
{
  "window_days": 7,
  "daily": [
    {
      "date": "2026-03-23",
      "cpu_millicores_seconds": 86400000,
      "memory_mib_seconds": 176947200,
      "gpu_seconds": 0
    },
    {
      "date": "2026-03-24",
      "cpu_millicores_seconds": 45000000,
      "memory_mib_seconds": 92160000,
      "gpu_seconds": 0
    }
  ],
  "total_cpu_millicores_seconds": 131400000,
  "total_memory_mib_seconds": 269107200,
  "total_gpu_seconds": 0
}
```

`daily` は `usage_date` 昇順でソートされる。ウィンドウ内に使用実績がない場合は `daily` が空配列、各 total が 0 となる。

### resource_quota

`namespace_resource_quotas` テーブルから自 namespace の ResourceQuota 情報を取得し、`resource_quota` フィールドとして返す。ResourceQuota が設定されていない、または Watcher が未同期でテーブルに行がない場合は `null` を返す。

`hard_count` / `used_count` は K8s ResourceQuota の `count/jobs.batch` に対応する。ResourceQuota に `count/jobs.batch` が含まれていない場合は `null` となる。

```json
{
  "window_days": 7,
  "daily": [...],
  "total_cpu_millicores_seconds": 131400000,
  "total_memory_mib_seconds": 269107200,
  "total_gpu_seconds": 0,
  "resource_quota": {
    "hard_cpu_millicores": 300000,
    "hard_memory_mib": 1280000,
    "hard_gpu": 4,
    "hard_count": 50,
    "used_cpu_millicores": 280000,
    "used_memory_mib": 819200,
    "used_gpu": 1,
    "used_count": 12
  }
}
```

ResourceQuota が存在しない場合:

```json
{
  "window_days": 7,
  "daily": [...],
  "total_cpu_millicores_seconds": 0,
  "total_memory_mib_seconds": 0,
  "total_gpu_seconds": 0,
  "resource_quota": null
}
```

## 16. GET /v1/flavors

利用可能な ResourceFlavor の一覧とリソース情報を返す。認証不要。

### response

```json
{
  "flavors": [
    {
      "name": "cpu",
      "has_gpu": false,
      "nodes": [
        {"node_name": "worker07", "cpu_millicores": 128000, "memory_mib": 515481, "gpu": 0},
        {"node_name": "worker08", "cpu_millicores": 128000, "memory_mib": 515481, "gpu": 0}
      ],
      "quota": {"cpu": "256", "memory": "1000Gi", "gpu": "0"}
    },
    {
      "name": "gpu-a100",
      "has_gpu": true,
      "nodes": [
        {"node_name": "gworker02", "cpu_millicores": 128000, "memory_mib": 515686, "gpu": 4}
      ],
      "quota": {"cpu": "64", "memory": "500Gi", "gpu": "4"}
    }
  ],
  "default_flavor": "cpu"
}
```

各 flavor の `nodes` には `node_resources` テーブルからその flavor に属するノードの一覧が含まれる。Watcher 未起動でノード情報がない flavor は `nodes` が空配列となる。

`quota` には `flavor_quotas` テーブルから取得した ClusterQueue の nominalQuota が含まれる。Watcher 未同期で quota 情報がない flavor は `quota` が `null` となる。

`default_flavor` は ConfigMap `DEFAULT_FLAVOR` の値。

## 17. GET /v1/cli/version

PVC 上に配置された CLI バイナリの最新バージョンを返す。認証不要。

Submit API は PVC（`cli-binary`）の `latest` ファイルを読み取り、最新バージョン文字列を返す。

### response

```json
{
  "version": "1.2.0"
}
```

### エラーレスポンス

PVC に `latest` ファイルが存在しない場合（バイナリ未配置）は 404 を返す。

```json
{ "detail": "CLI binary not found" }
```

## 18. GET /v1/cli/versions

PVC 上に配置された CLI バイナリの全バージョン一覧を返す。認証不要。

Submit API は PVC（`cli-binary`）のディレクトリエントリをスキャンし、利用可能なバージョンの一覧を返す。`latest` ファイルおよびディレクトリ以外のエントリは除外する。バージョンは semver 降順でソートされる（`packaging.version.Version` によるパース。パース不能なエントリは除外）。

### response

```json
{
  "versions": ["1.3.1-beta.2", "1.3.1-beta.1", "1.3.0", "1.2.0", "1.1.0"],
  "latest": "1.3.0"
}
```

### エラーレスポンス

PVC に `latest` ファイルが存在しない場合は 404 を返す。

```json
{ "detail": "CLI binary not found" }
```

## 19. GET /v1/cli/download

PVC 上に配置された CLI バイナリを返す。認証不要。

### クエリパラメータ

| パラメータ | 型 | 省略時の挙動 |
|---|---|---|
| `version` | 文字列（任意） | `latest` ファイルが指すバージョンのバイナリを返す |

`version` 指定時はそのバージョンの `<version>/cjob` バイナリを返す。省略時は `latest` ファイルからバージョンを読み取る（後方互換）。

`Content-Type: application/octet-stream` で返す。

```
GET /v1/cli/download
GET /v1/cli/download?version=1.3.1-beta.1
```

### エラーレスポンス

バージョン文字列の形式が不正な場合は 400 を返す。

```json
{ "detail": "Invalid version format" }
```

バイナリが存在しない場合は 404 を返す。

```json
{ "detail": "CLI binary not found" }
```

# API 設計

CLI はこの API を呼ぶ薄いクライアントとして実装する。
全エンドポイントで ServiceAccount JWT による認証・認可を行う（詳細は [auth_policy.md](../auth_policy.md) 参照）。

## 1. 共通エラーレスポンス仕様

全エンドポイントで共通して発生しうるエラーを以下に定義する。

| HTTP ステータス | 発生条件 | レスポンスボディ例 |
|---|---|---|
| 401 | JWT が無効・期限切れ・存在しない | `{ "detail": "Unauthorized" }` |
| 404 | 存在しない job_id、または他ユーザーの job_id | `{ "detail": "Job not found" }` |
| 409 | リセット処理中（`DELETING` ジョブが存在する namespace への投入） | `{ "detail": "リセット処理中のためジョブを投入できません。しばらく待ってから再試行してください" }` |
| 503 | DB 書き込み失敗など内部サービス一時不可 | `{ "detail": "Service temporarily unavailable" }` |

**404 の方針**：他ユーザーのジョブへのアクセスも 404 を返す。ジョブの存在自体を隠すことで情報漏洩を防ぐ。

**401 の方針**：TokenReview が失敗した場合（JWT 無効・期限切れ）に返す。レスポンスボディは固定文字列とし、詳細なエラー原因は含めない。

**レート制限の方針**：Submit API は各リクエストで K8s TokenReview API を呼ぶため、大量リクエストは K8s API サーバへの負荷につながりうる。ただし Submit API 自身の CPU/memory limit（500m / 512Mi）が事実上のスループット上限として機能するため、想定規模（20ユーザー）においては明示的なレート制限は不要と判断する。ユーザー数や利用規模が拡大する場合は `slowapi` 等による namespace ごとのレート制限を検討すること。

## 2. POST /v1/jobs

ジョブを1件投入する。

### request

```json
{
  "command": "python main.py --alpha 0.1 --beta 16",
  "image": "yusekiya/stg-jupyter:2.1.0",
  "cwd": "/home/jovyan/project-a/exp1",
  "env": {
    "OMP_NUM_THREADS": "4",
    "PYTHONPATH": "/home/jovyan/project-a"
  },
  "resources": {
    "cpu": "2",
    "memory": "4Gi",
    "gpu": 0
  },
  "time_limit_seconds": 3600
}
```

### response

```json
{
  "job_id": 1,
  "status": "QUEUED"
}
```

### バリデーション

`resources.gpu > 0` の場合は 400 を返す。GPU 対応は初期スコープ外（[implementation.md](implementation.md) §1 参照）であり、
将来 GPU 対応を追加する際にこのバリデーションを外す。

```json
{ "detail": "GPU ジョブは現在サポートされていません" }
```

要求リソース（CPU / メモリ）がクラスタ内の最大ノードの allocatable を超える場合は 400 を返す。
単一ノードに収まらないジョブは原理的に実行不可能であり、DISPATCHED 状態のまま無期限に滞留することを防ぐ。
`node_resources` テーブルが空の場合（Watcher 未起動等）はこのバリデーションをスキップする。

```json
{ "detail": "要求 CPU (128) がクラスタ内の最大ノード (64000m) を超えています" }
```

```json
{ "detail": "要求メモリ (2Ti) がクラスタ内の最大ノード (262144Mi) を超えています" }
```

`time_limit_seconds` は省略可能。省略時はサーバ側デフォルト（ConfigMap: `DEFAULT_TIME_LIMIT_SECONDS`、デフォルト 86400 = 24時間）を使用する。
`MAX_TIME_LIMIT_SECONDS`（デフォルト 604800 = 7日）を超える値を指定した場合は 400 を返す。

```json
{ "detail": "time_limit_seconds は 604800 秒（7日）以下で指定してください" }
```

`command` が空文字の場合は 400 を返す。

```json
{ "detail": "command は空にできません" }
```

namespace のジョブ総数（QUEUED / DISPATCHING / DISPATCHED / RUNNING / CANCELLED の合計）が
`MAX_QUEUED_JOBS_PER_NAMESPACE`（デフォルト 2000）に達している場合は 429 を返す。
CANCELLED ジョブを含めることで、cancel → 再投入の無制限サイクルによる DB 肥大化を防ぐ。
上限に達した場合は `cjob delete` で CANCELLED ジョブを削除してから再投入すること。

```json
{ "detail": "投入可能なジョブ数の上限（2000件）に達しています" }
```

namespace に `DELETING` 状態のジョブが1件でも存在する場合は 409 を返す（リセット処理中）。

```json
{ "detail": "リセット処理中のためジョブを投入できません。しばらく待ってから再試行してください" }
```

## 3. GET /v1/jobs

ジョブ一覧を取得する。JWT の namespace に属するジョブのみ返す。

### クエリパラメータ

| パラメータ | 型 | 省略時の挙動 |
|---|---|---|
| `status` | 文字列（任意） | 全ステータスを返す |
| `limit` | 整数（任意） | 全件返す |
| `order` | 文字列（`"asc"` or `"desc"`） | `"asc"`（JOB_ID 昇順） |

`limit` 指定時は常に最新（JOB_ID が大きい）N 件を選択し、`order` に応じてソートして返す。

```
GET /v1/jobs
GET /v1/jobs?status=RUNNING
GET /v1/jobs?status=FAILED&limit=10
GET /v1/jobs?limit=50&order=desc
```

### response

```json
{
  "jobs": [
    {
      "job_id": 1,
      "status": "RUNNING",
      "command": "python main.py --alpha 0.1 --beta 16",
      "created_at": "2026-03-23T12:34:56Z",
      "finished_at": null
    }
  ],
  "total_count": 1
}
```

`total_count` はフィルタ条件に一致するジョブの総数（`limit` 適用前）を返す。

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
  "time_limit_seconds": 86400,
  "k8s_job_name": "cjob-alice-1",
  "log_dir": "/home/jovyan/.cjob/logs/1",
  "created_at": "2026-03-23T12:34:56Z",
  "dispatched_at": "2026-03-23T12:35:02Z",
  "started_at": "2026-03-23T12:35:10Z",
  "finished_at": "2026-03-23T12:37:10Z"
}
```

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

## 7. POST /v1/jobs/delete

完了済みジョブを削除する。範囲指定・個別複数指定は CLI 側で展開してから送る。

CANCELLED / SUCCEEDED / FAILED 状態のジョブのみ削除対象とする。
QUEUED / DISPATCHING / DISPATCHED / RUNNING 状態のジョブは削除せず `skipped` として返す。
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
  "skipped":   [
    { "job_id": 3, "reason": "running" },
    { "job_id": 4, "reason": "deleting" }
  ],
  "not_found": []
}
```

`skipped` は対象ジョブが QUEUED / DISPATCHING / DISPATCHED / RUNNING / DELETING の場合。
`reason` の値は `"running"`（QUEUED / DISPATCHING / DISPATCHED / RUNNING）と `"deleting"`（DELETING）の2種類。
CLI は `reason` に基づいてメッセージを分岐する。QUEUED / DISPATCHING / DISPATCHED / RUNNING の場合は先に `cjob cancel` するよう促し、DELETING の場合はリセット処理中である旨を表示する（[cli.md](cli.md) §8 参照）。

**§6（cancel）との設計上の違い：**
§6 の `skipped` は「すでに終了済み・処理済み」という単一の意味しか持たないため job_id のフラットなリストで十分である。一方 §7 の `skipped` は「実行中（cancel を促すべき）」と「DELETING（何もできない）」で CLI が取るべきアクションが根本的に異なる。また、CLI が事前に `GET /v1/jobs/{job_id}` で状態を確認してから分岐する方式はレース条件を生じさせるため採用しない。`reason` をレスポンスに含めることで、スキップ判定と理由取得を原子的に行える。

## 8. POST /v1/reset

ユーザーの全ジョブ履歴をリセットし、job_id の採番を 1 に戻す。

リセット可能条件：全ジョブが CANCELLED / SUCCEEDED / FAILED のいずれかであること。
以下のいずれかに該当する場合は 409 を返す。

- QUEUED / DISPATCHING / DISPATCHED / RUNNING のジョブが1件でも存在する（未完了ジョブあり）
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

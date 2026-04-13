# Watcher / Reconciler 設計

## 1. 役割

Watcher / Reconciler は Kubernetes 側の実行状態を DB に反映する。

- Job 状態の監視
- Pod 状態の監視
- `RUNNING` / `SUCCEEDED` / `FAILED` への遷移
- `CANCELLED` ジョブの K8s Job 削除
- `DELETING` ジョブの K8s Job 削除・DB レコード削除・カウンターリセット
- orphan Job 検出
- DB と Kubernetes のズレ修正
- Prometheus カウンターメトリクス（`cjob_jobs_completed_total`）の提供（`WATCHER_METRICS_PORT` で `/metrics` エンドポイント）

Watcher のメインループは各スキャンサイクル完了時に `/tmp/liveness` ファイルをタッチする。Kubernetes の Liveness probe がこのファイルの最終更新時刻を確認し、ループ停止を検知して再起動できるようにする（[deployment.md](../deployment.md) §13.5 参照）。

Watcher は K8s Job の `cjob.io/namespace` ラベルから直接 namespace を取得するため、namespace の命名規則に依存しない（Watcher は既存のラベルを読み取るのみで、namespace 名の構築は行わない）。

## 1.1 ノードリソース同期

Watcher は K8s API からノードの `allocatable` リソースを定期取得し、DB の `node_resources` テーブルに書き込む（[database.md](database.md) §6 参照）。

- 取得間隔は `NODE_RESOURCE_SYNC_INTERVAL_SEC`（デフォルト 300 秒）で制御する。メインループ（`DISPATCH_BUDGET_CHECK_INTERVAL_SEC` 間隔）の N サイクルに 1 回実行する
- `RESOURCE_FLAVORS` 設定（[resources.md](resources.md) 参照）の各 flavor 定義を順にイテレーションし、`label_selector` で K8s API からノードを取得する。各ノードにはその flavor 定義の `name` を flavor 値として記録する
- GPU リソース数は flavor 定義の `gpu_resource_name` を使って `status.allocatable` から取得する。`gpu_resource_name` が未設定の flavor は GPU 数を 0 として記録する
- 各 flavor の取得結果をノード名で重複排除してマージする。複数の flavor のラベルに一致するノードは、`RESOURCE_FLAVORS` で先に定義された flavor が優先される
- DB に記録する CPU・memory は DaemonSet Pod の request 分を差し引いた effective allocatable である（CPU・memory のみ対象、GPU は差し引かない）。`list_pod_for_all_namespaces()` を `WATCHER_K8S_LIST_PAGE_SIZE` でページ取得し（§5.2 参照）、ページごとに `metadata.ownerReferences` に `kind: DaemonSet` を含みかつ `spec.nodeName` が設定済みかつ `status.phase` が `Pending` / `Running` の Pod をノード単位で集計する（ページ取得が終わった時点で生 Pod オブジェクトは破棄される）。各 Pod の `spec.containers[].resources.requests` を合計して `allocatable` から差し引く（initContainers は対象外）。requests 未設定のコンテナは 0 として扱い、差し引き結果が負になる場合は 0 にクランプする
- 初回は Watcher 起動直後に即実行し、以降は設定間隔で繰り返す
- K8s API から取得したノード一覧に存在しないが DB に残っているノード（撤去・ラベル除去）は DELETE する
- K8s API 呼び出し失敗時はログを出力してスキップし、次回サイクルで再試行する（DB の既存データはそのまま維持される）。特定 flavor のノード取得に失敗しても、他の flavor のノード同期は継続する。DaemonSet Pod 取得 API の呼び出しが失敗した場合は、当該サイクルの node 同期全体をスキップする（不正確な effective allocatable を DB に書き込まないため）

## 1.2 nominalQuota 同期

Watcher は K8s API から ClusterQueue の nominalQuota を定期取得し、DB の `flavor_quotas` テーブルに書き込む（[database.md](database.md) §7 参照）。

- ノードリソース同期（§1.1）と同じサイクルで実行する
- `CustomObjectsApi.get_cluster_custom_object()` で ClusterQueue（`CLUSTER_QUEUE_NAME`、デフォルト `cjob-cluster-queue`）を取得する
- `spec.resourceGroups[0].flavors[]` の各 flavor について、`resources[]` から nominalQuota を読み取る。リソース名 `cpu` → cpu 列、`memory` → memory 列、それ以外 → gpu 列にマッピングする
- K8s API 呼び出し失敗時はログを出力してスキップし、次回サイクルで再試行する（DB の既存データはそのまま維持される）

## 1.3 ResourceQuota 同期

Watcher は K8s API から各 user namespace の ResourceQuota 使用状況を定期取得し、DB の `namespace_resource_quotas` テーブルに書き込む（[database.md](database.md) §8 参照）。

- `RESOURCE_QUOTA_SYNC_INTERVAL_SEC`（デフォルト 10 秒）の間隔で実行する。ノードリソース同期（§1.1）や nominalQuota 同期（§1.2）とは独立したサイクルで動作する
- `CoreV1Api.list_namespace(label_selector=USER_NAMESPACE_LABEL)` で全ユーザー namespace を取得する。ジョブの有無に関わらず全ユーザー namespace を追跡対象とする（JupyterHub 等の User Pod によるリソース消費をジョブ投入前から把握するため）
- `CoreV1Api.list_resource_quota_for_all_namespaces(field_selector="metadata.name=RESOURCE_QUOTA_NAME")` で全 namespace の ResourceQuota を 1 回の API コールで取得する
- 取得結果からユーザー namespace に該当するもののみを処理し、`spec.hard` と `status.used` から `requests.cpu`、`requests.memory`、GPU リソース（`RESOURCE_FLAVORS` 設定の `gpu_resource_name` を使用）、および `count/jobs.batch` を取得する。CPU / memory は `parse_cpu_millicores()` / `parse_memory_mib()` でパースする。`count/jobs.batch` は `spec.hard` に含まれている場合のみ整数値を取得し、含まれていない場合は `NULL` として UPSERT する
- ユーザー namespace に該当する ResourceQuota が取得結果に含まれない場合は DB の該当行を DELETE する。Dispatcher はその namespace に対して制限なしとして扱う
- K8s API エラーの場合はログを出力して処理をスキップし、DB の既存データを維持する
- ユーザー namespace でなくなった namespace（ラベル除去）の行を DELETE する

## 2. 必要性

Dispatcher が DB スキャンで Job を作成しても、その後の実行状態（RUNNING / SUCCEEDED / FAILED）は Kubernetes 側でのみ確定する。
Dispatcher だけでは K8s Job の完了・失敗を検知できないため、Watcher が必要である。

## 3. 最小アルゴリズム

1. Kubernetes Job 一覧を `WATCHER_K8S_LIST_PAGE_SIZE`（§5.5）で定期監視し、ページごとに軽量 dataclass（§5.1）に変換する。**API 呼び出しがいずれかのページで失敗した場合は reconcile サイクル全体をスキップする**（ステップ 2〜8 および DELETING Phase 2 は K8s Job 一覧が完全であることを前提としており、不完全な一覧で処理を続行するとステップ 8 が正常なジョブを FAILED に誤遷移させ、DELETING Phase 2 が K8s Job 残存のまま DB をクリーンアップする危険がある）
2. Job の `status.conditions` を以下のルールで解釈する

   | K8s Job の `status.conditions` | DB status | 備考 |
   |---|---|---|
   | `type: Complete, status: True` | `SUCCEEDED` | |
   | `type: Failed, status: True, reason: DeadlineExceeded` | `FAILED` | `last_error` に `"time limit exceeded"` を設定 |
   | `type: Failed, status: True` | `FAILED` | Pod の exit code 非0・起動失敗を含む |
   | 条件なし・Pod が Running 中 | `RUNNING` | 初回 RUNNING 遷移時に `started_at` を記録し、全 Pod の `spec.nodeName` から `node_name` を取得して記録し、`namespace_daily_usage` に累計消費量を加算する（[database.md](database.md) §5.2 参照） |

   **完了フォールバック（使用量記録）:** 1 スキャンサイクル以内に完了したジョブは Watcher が RUNNING を観測できず、DISPATCHED から直接 SUCCEEDED / FAILED に遷移する。この場合 `started_at` は NULL のままなので、完了遷移時に `started_at` が NULL であれば `_record_resource_usage` を呼び出して `namespace_daily_usage` に使用量を加算する。`started_at` は NULL のまま維持する（実際に RUNNING を観測していないため）。sweep ジョブにも同じフォールバックが適用される。

3. `cjob.io/job-id` ラベルと `cjob.io/namespace` ラベルから対応する `job_id` を特定する（`k8s_job_name` による照合は使用しない）
4. DB 状態を更新する。ただし DB の status が `CANCELLED` または `DELETING` のジョブは上書きしない（K8s 側が完了・失敗していても DB の意図的な状態を維持する）。なお `HELD` ジョブは K8s Job が未作成のためこのステップの対象にならない
5. DB の status が `CANCELLED` のジョブに対応する K8s Job が存在する場合は削除する（K8s Job 削除後も DB の status は `CANCELLED` のまま維持する）
6. DB の status が `DELETING` のジョブを二フェーズで処理する

   **フェーズ 1（削除要求）：**
   対応する K8s Job が存在する場合は削除する（`propagation_policy="Background"` で Pod も連動削除）

   **フェーズ 2（完了確認とクリーンアップ）：**
   次以降のスキャンサイクルで、namespace 内の全 `DELETING` ジョブについて対応する K8s Job が K8s 上に存在しないことを確認する。全件の K8s Job が消滅していた場合、以下を**単一トランザクション**で実行する。

   1. `jobs` テーブルから該当 namespace の全レコードを削除する（`job_events` は `ON DELETE CASCADE` で連動削除される）
   2. `user_job_counters` の `next_id` を 1 にリセットする

   （トランザクション内で途中クラッシュした場合はすべてロールバックされ、次のサイクルで再実行される）

   （`propagation_policy="Background"` の削除は非同期で完結するため、フェーズ 1 と同一サイクルでフェーズ 2 を実行してはならない）

7. `cjob.io/job-id` ラベルに対応する DB レコードが存在しない K8s Job（orphan Job）は削除する
8. DB 上で DISPATCHED / RUNNING だが、対応する K8s Job が K8s 上に存在しないジョブを FAILED に遷移させる（`last_error` に `"K8s Job not found (TTL expired or manually deleted)"` を設定し、`finished_at` を現在時刻に設定する）。これにより `ttlSecondsAfterFinished` による K8s Job の自動削除や、手動削除によって DB と K8s の状態が乖離した場合に自動修復される

**`ttlSecondsAfterFinished` とスキャンサイクル間隔の関係:**

`ttlSecondsAfterFinished` は Watcher のスキャンサイクル間隔（現在は `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` を共有）より十分に長く設定する必要がある。TTL が短すぎると、Watcher の一時的な停止（再起動・障害等）中に完了した K8s Job が TTL で削除され、ステップ 8 により正常完了したジョブが FAILED として記録される。現在の設定（TTL 300秒 vs サイクル間隔 10秒）では Watcher の再起動（通常 1〜2 分）に対しても十分な余裕がある。TTL またはサイクル間隔を変更する場合は、この関係を維持すること

## 4. sweep ジョブの監視

### 4.1 インデックス追跡

ポーリングサイクルごとに K8s API から `status.completedIndexes` / `status.failedIndexes` / `status.succeeded` / `status.failed` を取得し、DB の対応カラムを更新する。

```sql
UPDATE jobs
SET completed_indexes = :completed_indexes,
    failed_indexes = :failed_indexes,
    succeeded_count = :succeeded_count,
    failed_count = :failed_count
WHERE namespace = :namespace
  AND job_id = :job_id;
```

### 4.2 状態遷移の判定

K8s Job の `status.conditions` に従う（通常ジョブと同じロジック）。`Complete` または `Failed` の condition が出現した時点で最終ステータスを決定する。

- K8s が `Complete` を返した場合: `failed_count > 0` なら **FAILED**、`failed_count == 0` なら **SUCCEEDED**
- K8s が `Failed` を返した場合（`activeDeadlineSeconds` 超過等）: **FAILED**

これにより、部分的に失敗したタスクがある sweep は常に FAILED として扱われる。

### 4.3 RUNNING への遷移

最初の Pod が RUNNING になった時点（K8s Job の `status.active >= 1`）で DB を RUNNING に更新する。通常ジョブと同様に `started_at` と `node_name` を記録する。

### 4.3.1 node_name の記録

`node_name` はジョブの実行期間を通じて使用された全ノード名の累積リストである。DB 上ではカンマ区切りの TEXT（例: `"node-1,node-2"`）として格納される。通常ジョブでは Pod が1つなので結果的に単一ノード名となり、sweep ジョブとの分岐は不要である。

**記録のトリガー条件:**

1. **RUNNING 遷移時**: `CoreV1Api().list_namespaced_pod()` で Job の全 Pod を取得し、各 Pod の `spec.nodeName` を `node_name` にマージする
2. **sweep の `succeeded_count` / `failed_count` 変化時**: Pod リストを取得し、新しいノード名があれば `node_name` に追加する。毎サイクルではなくカウンタ変化時のみ API を呼ぶことで K8s API（etcd）への追加負荷を最小限に抑える
3. **完了フォールバック**: RUNNING を経由せず直接 SUCCEEDED/FAILED に遷移したジョブは、完了遷移時に `node_name` が未記録であれば Pod からの取得を試みる（Pod は `ttlSecondsAfterFinished` の間は残存している）

累積記録（append-only）方式を採用し、一度記録したノード名は削除しない。Pod が reconcile 間隔より短い時間で起動・完了・削除された場合、そのノード名を取りこぼす可能性がある。Pod が既に削除済みの場合は `node_name` は NULL のままとなる。

### 4.4 リソース使用量の加算

RUNNING 遷移時に `time_limit_seconds × リソース量 × parallelism` を加算する。同時に使用するリソースの最大量を反映し、DRF の公平性計算で sweep ジョブが適切に重く評価される。RUNNING を観測せず直接完了した場合は §3 の完了フォールバックにより同じ計算で使用量が加算される。

### 4.5 CANCELLED 時の処理

通常ジョブと同じ流れで処理する。部分的に完了したタスクの `completed_indexes` / `failed_indexes` は、直前のポーリングサイクルで更新済みの値が DB に残る。

## 5. メモリ使用量の制御

Watcher の reconcile サイクルと node_sync サイクルは K8s API のレスポンスと DB クエリ結果をメモリ上に保持する。ジョブ数・Pod 数に比例してメモリ消費が増大するため、規模が大きくなると OOMKilled を起こしやすい。以下の方針でピークメモリを抑制する。

### 5.1 K8s Job 取得のページネーションと軽量表現

`BatchV1Api.list_job_for_all_namespaces()` は `limit` / `continue` パラメータによるページネーションに対応している。Watcher は `WATCHER_K8S_LIST_PAGE_SIZE`（デフォルト 500）でページ取得し、各ページから reconcile に必要な最小限のフィールドだけを軽量 dataclass（`LightK8sJob`）に抽出して保持する。生の `V1Job` オブジェクトは抽出直後に参照を解放し、ページ単位でガベージコレクションされるようにする。

`LightK8sJob` が保持するフィールド:
- `namespace`, `job_id`（`cjob.io/namespace` / `cjob.io/job-id` ラベルから抽出）
- `name`（`metadata.name`）
- `conditions`（`status.conditions` を `(type, status, reason)` の tuple に変換）
- `active`, `succeeded`, `failed`, `completed_indexes`, `failed_indexes`

上記以外の `V1Job` 情報（Pod template、ラベル全体、annotations 等）は reconcile で参照しないため、軽量表現への変換時点で破棄する。これにより保持オブジェクトあたりのメモリは 1/10 程度に削減できる。

reconcile サイクル中、軽量表現の全 Job リストと `k8s_map` はサイクル完了まで保持されるため、ページネーションだけではピーク削減の効果が限定的である。軽量化との組み合わせで、API レスポンスのパース時のピークも reconcile サイクル中の常駐量も抑制できる。

**ページネーションとサイクル全体の失敗扱い:** ページの途中で `ApiException` が発生した場合（`continue` トークンの期限切れや API Server 一時エラーを含む）、reconcile サイクル全体をスキップする（§3 ステップ 1 と同じ扱い）。部分的な Job リストで処理を続行するとステップ 8 が正常なジョブを FAILED に誤遷移させる危険があるため、ページ単位の失敗は許容しない。

### 5.2 DaemonSet Pod 取得のページネーションとページ単位集計

`CoreV1Api.list_pod_for_all_namespaces()`（ノードリソース同期 §1.1 で使用）もページネーションに対応する。Watcher は同じ `WATCHER_K8S_LIST_PAGE_SIZE` でページ取得し、各ページから DaemonSet Pod の CPU / memory request をノード単位で集計する。集計結果のみを残し、生の Pod オブジェクトはページごとに破棄する。

K8s API には ownerReference による直接フィルタがないため API レベルで DaemonSet Pod のみを取得することはできないが、ページ単位で集計・破棄するピークメモリは全 Pod 一覧を保持する場合と比べて大幅に削減される。

ページの途中で API 呼び出しが失敗した場合は、node_sync サイクル全体をスキップし DB の既存データを維持する（§1.1 の既存のエラーハンドリング方針と一致）。

### 5.3 DB クエリの軽量化

reconcile サイクルの DB 読み込みは以下の方針でメモリ常駐量を抑える。

- **K8s Job に対応する DB Job の取得**: `k8s_map` のキー `(namespace, job_id)` の集合に限定して取得する（`tuple_(Job.namespace, Job.job_id).in_(...)`）。namespace 単位で全 Job を取得する従来方式と比べ、HELD / QUEUED / CANCELLED など reconcile で使わない Job のロードを回避できる
- **DELETING ジョブの取得**: DELETING Phase 2 の namespace 単位のクリーンアップ判定で必要なため、namespace 指定の全件取得を維持する（DELETING ジョブ数は通常少ないためメモリ影響は小さい）
- **ステップ 8 の DISPATCHED / RUNNING 突合**: 存在チェックのために `(namespace, job_id)` の tuple だけを SELECT する。`k8s_map` に存在しない Job についてのみ、対象を絞った ORM クエリで行を読み込み FAILED 遷移とイベント挿入を行う

### 5.4 Pod 取得の namespace 単位バッチング

reconcile 中の `node_name` 記録に使用する `CoreV1Api.list_namespaced_pod()` は、Job 単位で `label_selector=job-name=...` を指定して呼び出していた（Job 数 × API 呼び出しの N+1）。これを namespace 単位のキャッシュに統合する。

- reconcile サイクル内で初めてその namespace の Pod が必要になった時点で `list_namespaced_pod(namespace, label_selector="job-name")` を 1 回だけ呼び出し、`job-name` ラベルから `[node_name, ...]` へのマップを構築する
- 同じ namespace の以降の Job はキャッシュから解決する
- サイクル終了時にキャッシュは破棄される

これにより API 呼び出し回数は namespace 数に比例するだけとなり、同時に保持する `V1PodList` の個数も最大 1 個となる。Pod 取得の失敗は従来通り該当 Job のノード名を空として扱い、reconcile は継続する。

### 5.5 設定

| 設定項目 | デフォルト | 用途 |
|---|---|---|
| `WATCHER_K8S_LIST_PAGE_SIZE` | 500 | `list_job_for_all_namespaces()` と `list_pod_for_all_namespaces()` のページサイズ。値を大きくするとページ数が減り API 往復コストが下がるが、1 ページのレスポンスサイズが増える |

この設定は Watcher のデフォルト ConfigMap には含めず、必要に応じて管理者が overlay の ConfigMap に追加する運用とする（[deployment.md](../deployment.md) §5 参照）。

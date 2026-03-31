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

Watcher のメインループは各スキャンサイクル完了時に `/tmp/liveness` ファイルをタッチする。Kubernetes の Liveness probe がこのファイルの最終更新時刻を確認し、ループ停止を検知して再起動できるようにする（[deployment.md](../deployment.md) §13.5 参照）。

Watcher は K8s Job の `cjob.io/namespace` ラベルから直接 namespace を取得するため、namespace の命名規則に依存しない（Watcher は既存のラベルを読み取るのみで、namespace 名の構築は行わない）。

## 1.1 ノードリソース同期

Watcher は K8s API からノードの `allocatable` リソースを定期取得し、DB の `node_resources` テーブルに書き込む（[database.md](database.md) §6 参照）。

- 取得間隔は `NODE_RESOURCE_SYNC_INTERVAL_SEC`（デフォルト 300 秒）で制御する。メインループ（`DISPATCH_BUDGET_CHECK_INTERVAL_SEC` 間隔）の N サイクルに 1 回実行する
- `RESOURCE_FLAVORS` 設定（[resources.md](resources.md) 参照）の各 flavor 定義を順にイテレーションし、`label_selector` で K8s API からノードを取得する。各ノードにはその flavor 定義の `name` を flavor 値として記録する
- GPU リソース数は flavor 定義の `gpu_resource_name` を使って `status.allocatable` から取得する。`gpu_resource_name` が未設定の flavor は GPU 数を 0 として記録する
- 各 flavor の取得結果をノード名で重複排除してマージする。複数の flavor のラベルに一致するノードは、`RESOURCE_FLAVORS` で先に定義された flavor が優先される
- 初回は Watcher 起動直後に即実行し、以降は設定間隔で繰り返す
- K8s API から取得したノード一覧に存在しないが DB に残っているノード（撤去・ラベル除去）は DELETE する
- K8s API 呼び出し失敗時はログを出力してスキップし、次回サイクルで再試行する（DB の既存データはそのまま維持される）。特定 flavor のノード取得に失敗しても、他の flavor のノード同期は継続する

## 2. 必要性

Dispatcher が DB スキャンで Job を作成しても、その後の実行状態（RUNNING / SUCCEEDED / FAILED）は Kubernetes 側でのみ確定する。
Dispatcher だけでは K8s Job の完了・失敗を検知できないため、Watcher が必要である。

## 3. 最小アルゴリズム

1. Kubernetes Job 一覧を定期監視（または watch API を使用）
2. Job の `status.conditions` を以下のルールで解釈する

   | K8s Job の `status.conditions` | DB status | 備考 |
   |---|---|---|
   | `type: Complete, status: True` | `SUCCEEDED` | |
   | `type: Failed, status: True, reason: DeadlineExceeded` | `FAILED` | `last_error` に `"time limit exceeded"` を設定 |
   | `type: Failed, status: True` | `FAILED` | Pod の exit code 非0・起動失敗を含む |
   | 条件なし・Pod が Running 中 | `RUNNING` | 初回 RUNNING 遷移時に `started_at` を記録し、Pod の `spec.nodeName` から `node_name` を取得して記録し、`namespace_daily_usage` に累計消費量を加算する（[database.md](database.md) §5.2 参照） |

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

定期同期（§1.1）ではなく、ジョブごとの状態遷移時に1回だけ実行される処理である。

Watcher は RUNNING 遷移時に `CoreV1Api().list_namespaced_pod()` で Job の Pod を取得し、`spec.nodeName` を DB の `node_name` カラムに記録する。一瞬で完了するジョブ（RUNNING を経由せず直接 SUCCEEDED/FAILED に遷移）の場合は、完了遷移時に `node_name` が未記録であれば Pod からの取得を試みる（Pod は `ttlSecondsAfterFinished` の間は残存している）。Pod が既に削除済みの場合は `node_name` は NULL のままとなる。

### 4.4 リソース使用量の加算

RUNNING 遷移時に `time_limit_seconds × リソース量 × parallelism` を加算する。同時に使用するリソースの最大量を反映し、DRF の公平性計算で sweep ジョブが適切に重く評価される。

### 4.5 CANCELLED 時の処理

通常ジョブと同じ流れで処理する。部分的に完了したタスクの `completed_indexes` / `failed_indexes` は、直前のポーリングサイクルで更新済みの値が DB に残る。

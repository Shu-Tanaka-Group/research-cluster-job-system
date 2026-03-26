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

Watcher は K8s Job の `cjob.io/namespace` ラベルから直接 namespace を取得するため、`JOB_NAMESPACE_PREFIX` 環境変数を必要としない（Dispatcher は Job 作成時に namespace を `user-<username>` 形式で構築する際に `JOB_NAMESPACE_PREFIX` を使用するが、Watcher は既存のラベルを読み取るのみで構築は行わない）。

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
   | 条件なし・Pod が Running 中 | `RUNNING` | 初回 RUNNING 遷移時に `started_at` を記録する |

3. `cjob.io/job-id` ラベルと `cjob.io/namespace` ラベルから対応する `job_id` を特定する（`k8s_job_name` による照合は使用しない）
4. DB 状態を更新する。ただし DB の status が `CANCELLED` または `DELETING` のジョブは上書きしない（K8s 側が完了・失敗していても DB の意図的な状態を維持する）
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

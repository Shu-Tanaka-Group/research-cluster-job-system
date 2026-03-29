# 実装計画

## 1. 実装に使用するパッケージ / 技術

### 1.1 Python パッケージ

- **FastAPI**: Submit API 実装用
- **SQLAlchemy**: PostgreSQL ORM / DB access
- **psycopg**: PostgreSQL ドライバ
- **kubernetes**: Kubernetes Job 作成 / 状態監視用
- **Pydantic**: API リクエスト / レスポンス定義用

### 1.2 ミドルウェア

- **PostgreSQL**
- **Kubernetes**
- **Kueue**

### 1.3 Rust クレート（cjob CLI）

- **clap**: CLI 引数パース
- **reqwest**: HTTP クライアント（Submit API との通信）
- **tokio**: 非同期ランタイム（`--follow` のリアルタイムログ追跡）
- **serde / serde_json**: JSON シリアライズ・デシリアライズ

## 2. 実装方針の詳細

### 2.1 submit の正本管理

ジョブ投入時は次の順で行う。

1. CLI が `cwd`、`env`、`command`、および `CJOB_IMAGE`（未設定時は `JUPYTER_IMAGE`）環境変数から `image` を集める
2. CLI が ServiceAccount JWT と namespace を固定パスから読み取る
3. API が `job_id` を発行する
4. PostgreSQL に `QUEUED` で保存する（`log_dir` も同時に設定）
5. 成功を返す

### 2.2 Dispatcher の動作アルゴリズム

起動時:

1. `DISPATCHING` 状態のジョブを `QUEUED` に戻す（再起動時の整合）

メインループ（`DISPATCH_BUDGET_CHECK_INTERVAL_SEC` 秒ごとにスキャン）:

1. `namespace_daily_usage` のウィンドウ外の古い行を削除する（[dispatcher.md](dispatcher.md) §1.2 参照）
2. DB から dispatch 対象ジョブを取得する（[dispatcher.md](dispatcher.md) §1.2 のクエリ）
   - budget に余裕がある namespace のみ対象
   - `retry_after IS NULL OR retry_after <= NOW()` の条件を満たすジョブのみ
   - namespace 間をラウンドロビンで公平に取得（`DISPATCH_ROUND_SIZE` 件ずつ交互）
   - DRF（Dominant Resource Fairness）により累計消費量の少ない namespace を優先
   - `LIMIT DISPATCH_BATCH_SIZE`（デフォルト 50）で1サイクルの取得数を制限
3. 隙間充填フィルタを適用する（[dispatcher.md](dispatcher.md) §2.4 参照）
   - 滞留ジョブ（DISPATCHED のまま閾値超過）が存在する namespace の候補を制限
   - RUNNING ジョブの最短残り時間に収まるジョブのみ dispatch 対象とする
4. 取得したジョブを順に dispatch する
   1. `WHERE status='QUEUED'` 条件付き UPDATE で `DISPATCHING` に CAS 更新する
   2. 更新行数が 0（cancel API が先に `CANCELLED` へ更新済み）ならスキップ
5. Job を作成（`claimName` には job の user を使用）
6. 成功なら `DISPATCHED` に更新（`AND status='DISPATCHING'` 条件付き）
7. 一時障害なら `retry_count` をインクリメントして `retry_after` を設定して `QUEUED` に戻す（`AND status='DISPATCHING'` 条件付き・[dispatcher.md](dispatcher.md) §1.3 参照）
8. 永続障害・バリデーションエラーなら `FAILED` に更新（`AND status='DISPATCHING'` 条件付き）
9. `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` 秒スリープして次のスキャンへ

### 2.3 Watcher の最小アルゴリズム

1. Kubernetes Job 一覧を監視
2. Job の `status.conditions` を以下のルールで解釈する

   | K8s Job の `status.conditions` | DB status | 備考 |
   |---|---|---|
   | `type: Complete, status: True` | `SUCCEEDED` | |
   | `type: Failed, status: True, reason: DeadlineExceeded` | `FAILED` | `last_error` に `"time limit exceeded"` を設定 |
   | `type: Failed, status: True` | `FAILED` | Pod の exit code 非0・起動失敗を含む |
   | 条件なし・Pod が Running 中 | `RUNNING` | 初回 RUNNING 遷移時に `started_at` を記録し、`namespace_daily_usage` に累計消費量を加算 |

3. `cjob.io/job-id` ラベルと `cjob.io/namespace` ラベルで対応する `job_id` を特定する（`k8s_job_name` による照合は使用しない）
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
8. DB 上で DISPATCHED / RUNNING だが、対応する K8s Job が K8s 上に存在しないジョブを FAILED に遷移させる（`last_error` に `"K8s Job not found (TTL expired or manually deleted)"` を設定し、`finished_at` を現在時刻に設定する）

## 3. 実装手順

以下の順番で実装する。

### Step 1: 基本インフラ準備

- PostgreSQL をデプロイする（StatefulSet + PVC）
- Kueue を導入する
- ResourceFlavor / ClusterQueue を作成する
- namespace 作成スクリプトを整備する（LocalQueue / ResourceQuota / ServiceAccount 含む）

### Step 2: `cjob` CLI の最小実装

- `cjob add`
- `cjob list`
- `cjob status`
- `cjob cancel`

この段階では `cjob add` から API にジョブ定義を送れるようにする。

### Step 3: Submit API 実装

- ServiceAccount JWT による認証・認可
- API エンドポイントの実装（詳細は [api.md](api.md) を参照）

併せて PostgreSQL スキーマを作成する。

### Step 4: Dispatcher 実装

- 起動時初期化（DISPATCHING → QUEUED）
- DB スキャン実装（[dispatcher.md](dispatcher.md) §1.2 のクエリ）
- dispatch budget 計算
- Kubernetes Job 作成（tee ラップコマンド含む）
- 再試行ポリシー実装（`retry_after` タイムスタンプ方式）

### Step 5: Watcher / Reconciler 実装

- Job 状態監視
- Pod 状態監視
- DB 更新
- 失敗理由反映
- DELETING 状態の処理（K8s Job 削除・DB クリーンアップ・カウンターリセット）

### Step 6: ログ取得・削除・リセット実装

- Job Pod のコマンドに tee ラップを追加（Step 4 で実施済み）
- `cjob logs`（完了後表示）
- `cjob logs --follow`（リアルタイム追跡）
- `cjob delete`
- `cjob reset`

### Step 7: 運用機能追加

- metrics
- tracing
- cleanup policy

## 4. 初期実装のスコープ

初期実装では、以下に絞る。

- CPU / memory ジョブのみ
- `cjob add`
- `cjob list`
- `cjob status`
- `cjob cancel`
- `cjob delete`
- `cjob reset`
- `cjob logs`（`--follow` 含む）
- PostgreSQL 1 DB
- namespace ごとの LocalQueue
- 1種類の runtime image

以下は後回しにできる。

- GPU 対応
- 複数 runtime class
- 高度な retry policy
- QoS / priority
- Web UI
- Prefect 連携
- Argo 連携
- artifact 管理

## 5. 将来拡張

将来的には次を追加可能である。

- Prefect から submit API を呼ぶ orchestration 連携
- Web UI
- retry failed only
- 実行履歴可視化
- queue priority
- 複数 ClusterQueue の使い分け
- GPU / highmem クラス
- 成果物管理
- ログ集約

## 6. 最終方針

本システムは、以下の構成を採用する。

- **ジョブ投入 UX**: `cjob add <job command>`
- **CLI 実装言語**: Rust（シングルバイナリ・GitHub Releases で配布）
- **実行単位**: 1コマンド = 1 Kubernetes Job
- **スケジューリング**: DB スキャン型 Dispatcher（公平スケジューリング・投入順保証）
- **状態管理**: PostgreSQL
- **job_id 採番**: ユーザー（namespace）ごとの連番（1, 2, 3...）
- **K8s Job 名**: `cjob-<username>-<job_id>`（グローバルに一意）
- **実行制御**: Dispatcher + Kueue
- **実行基盤**: Kubernetes Job
- **実行環境**: `JUPYTER_IMAGE` 環境変数で指定された image（User Pod と同一）+ namespace PVC mounted at `${WORKSPACE_MOUNT_PATH}`（デフォルト `/home/jovyan`）
- **再現対象**: submit 時の `cwd` / exported env（仮想環境 PATH 含む）/ command
- **ログ保存**: PVC 上の `${LOG_BASE_DIR}/<job_id>/`（デフォルト `/home/jovyan/.cjob/logs/<job_id>/`）
- **ログ取得**: CLI が PVC を直接読む（ログパスは API から取得）・閲覧のみ・削除は delete / reset が担う
- **キャンセル**: 単体・範囲指定（1-10）・個別複数指定（1,3,5）・組み合わせに対応
- **削除**: `cjob delete` で完了済みジョブを個別削除（実行中ジョブは削除不可・cancel を促す。reset 処理中の DELETING ジョブも削除不可）
- **リセット**: `cjob reset` で全ジョブ履歴・ログを削除し job_id を 1 から採番し直す（全ジョブ完了時のみ実行可能）
- **認証・認可**: ServiceAccount JWT + TokenReview（詳細は [auth_policy.md](../auth_policy.md) 参照）
- **大量投入対応**: dispatch budget + DB スキャン型スケジューリングにより Job materialization を抑制する。投入上限（`MAX_QUEUED_JOBS_PER_NAMESPACE`）は QUEUED / DISPATCHING / DISPATCHED / RUNNING / CANCELLED の合計でカウントし、cancel → 再投入サイクルによる DB 肥大化を防ぐ

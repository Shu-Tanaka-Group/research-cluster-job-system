# モニタリング設計

## 1. 概要

CJob クラスタの利用状況をユーザーが確認するための Grafana ダッシュボードを提供する。

**ダッシュボードの目的**: 「今クラスタは忙しいか？ジョブを投入すべきか待つべきか？」にユーザーが一目で判断できること。

**データソース**:
- **Prometheus**: ノード/Pod メトリクス、Kueue メトリクス、CJob アプリケーションメトリクス
- **PostgreSQL**: CJob DB（ジョブ状態、待ち時間実績）

## 2. 前提作業

### 2.1 Kueue ClusterQueue リソースメトリクスの有効化

Kueue v0.16.4 では ClusterQueue のリソース使用量メトリクス（`kueue_cluster_queue_resource_usage` / `kueue_cluster_queue_nominal_quota`）はデフォルトで無効である。ダッシュボードの CPU/GPU 使用率ゲージに必要なため、有効化する。

`kueue-manager-config` ConfigMap の `controller_manager_config.yaml` に以下を追加：

```yaml
metrics:
  enableClusterQueueResources: true
```

変更後、kueue-controller-manager の Pod を再起動する：

```bash
kubectl rollout restart deployment kueue-controller-manager -n kueue-system
```

### 2.2 Grafana への PostgreSQL データソース追加

CJob の PostgreSQL に読み取り専用ユーザーを作成し、Grafana のデータソースとして登録する。

#### 読み取り専用ユーザーの作成

```sql
CREATE ROLE grafana_reader LOGIN PASSWORD '<secure-password>';
GRANT CONNECT ON DATABASE cjob TO grafana_reader;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_reader;
```

#### Grafana データソース設定

| 項目 | 値 |
|---|---|
| Name | CJob DB |
| Type | PostgreSQL |
| Host | `postgres.cjob-system.svc.cluster.local:5432` |
| Database | `cjob` |
| User | `grafana_reader` |
| TLS/SSL Mode | 環境に応じて設定 |

### 2.3 CJob アプリケーションメトリクスの scrape 確認

Submit API と Watcher は Prometheus カウンターメトリクスを提供する。Prometheus Operator の ServiceMonitor / PodMonitor で scrape を設定する。

| コンポーネント | ポート | パス | 備考 |
|---|---|---|---|
| Submit API | 8080 | `/metrics` | FastAPI アプリと同一ポート |
| Watcher | 9090（`WATCHER_METRICS_PORT`） | `/metrics` | メインループとは別スレッドで提供 |
| Dispatcher | 9090（`DISPATCHER_METRICS_PORT`） | `/metrics` | メインループとは別スレッドで提供 |

**提供するメトリクス**:

| メトリクス名 | 型 | ラベル | 計装箇所 | 説明 |
|---|---|---|---|---|
| `cjob_jobs_submitted_total` | Counter | — | Submit API | ジョブ投入数（`submit_job` / `submit_sweep` 成功時に increment） |
| `cjob_jobs_completed_total` | Counter | `status` | Watcher / Submit API | ジョブ完了数。`status` は `succeeded` / `failed` / `cancelled` |

`cjob_jobs_completed_total` の計装箇所:
- `succeeded` / `failed`: Watcher の `reconcile_cycle()` でステータス遷移時
- `failed`（K8s Job 消失）: Watcher の `reconcile_cycle()` でジョブ消失検出時
- `failed`（dispatch 失敗）: Dispatcher の `mark_failed()` で永続エラー・リトライ上限超過時
- `cancelled`: Submit API の `cancel_single()` でキャンセル成功時

これらのカウンターはプロセス再起動時にリセットされるが、Prometheus の `increase()` / `rate()` 関数がリセットを自動的に処理するためダッュボードへの影響はない。

### 2.4 Kueue Prometheus メトリクスの scrape 確認

Kueue の Prometheus メトリクスが ServiceMonitor または PodMonitor で scrape されていることを確認する。Kueue を Helm でインストールした場合は `enablePrometheus: true` で ServiceMonitor が作成される。マニフェストでインストールした場合は、以下を参照して手動で設定する：

```bash
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.16.4/prometheus.yaml
```

## 3. ダッシュボード設計

### 3.1 基本情報

| 項目 | 値 |
|---|---|
| タイトル | CJob クラスタ状況 |
| デフォルト時間範囲 | 6 時間 |
| 自動更新間隔 | 30 秒 |
| タグ | `cjob` |

### 3.2 パネル構成

#### Row 1: 概要（トラフィックライト）

ダッシュボード上部に配置するサマリ行。5 秒でクラスタ状態を把握できる。

| Panel | Type | DataSource | 内容 | 閾値 |
|---|---|---|---|---|
| CPU 予約率 | Gauge | Prometheus | cpu の CPU 予約率（ジョブ要求合計 / クォータ上限） | green < 60%, yellow < 85%, red >= 85% |
| CPU Sub 予約率 | Gauge | Prometheus | cpu-sub の CPU 予約率（ジョブ要求合計 / クォータ上限） | green < 60%, yellow < 85%, red >= 85% |
| GPU 予約率 | Gauge | Prometheus | gpu の GPU 予約率（ジョブ要求合計 / クォータ上限） | green < 50%, yellow < 75%, red >= 75% |
| 待機中ジョブ数 | Stat | PostgreSQL | DB 上の待機中ジョブ数（QUEUED + DISPATCHING + DISPATCHED） | green < 5, yellow < 20, red >= 20 |
| リソース割当て待ち (P50) | Stat | Prometheus | Kueue の admission wait time の中央値（直近 1 時間） | green < 60s, yellow < 300s, red >= 300s |

#### Row 2: 現在のジョブ状況

| Panel | Type | DataSource | 内容 |
|---|---|---|---|
| ジョブ状態の内訳 | Pie chart | PostgreSQL | 直近 24 時間のジョブ状態内訳 |
| 実行中ジョブ数 | Stat | PostgreSQL | 全ユーザー合計の実行中ジョブ数 |
| 成功率（直近 24 時間） | Stat | Prometheus | SUCCEEDED / (SUCCEEDED + FAILED) |
| アクティブユーザー数 | Stat | PostgreSQL | RUNNING ジョブを持つユーザー（namespace）数 |
| クラスタノード数 | Stat | PostgreSQL | node_resources テーブルのレコード数 |

#### Row 3: キューの状態

| Panel | Type | DataSource | 内容 |
|---|---|---|---|
| Flavor 別キュー使用状況 | Table | PostgreSQL | flavor ごとの実行中・待機中・保留中ジョブ数 |
| キュー内ジョブ数の推移 | Time series | Prometheus | 実行中（admitted_active）と待機中（pending）の推移 |
| ジョブ投入・完了の推移 | Time series (line) | Prometheus | 時間帯別の投入数と完了数 |

#### Row 4: CPU Flavor の詳細

| Panel | Type | DataSource | 内容 |
|---|---|---|---|
| CPU 予約量の推移 | Time series | Prometheus | cpu の CPU 予約量 vs クォータ上限 |
| メモリ予約量の推移 | Time series | Prometheus | cpu のメモリ予約量 vs クォータ上限（GiB 表示） |

#### Row 5: GPU Flavor の詳細

| Panel | Type | DataSource | 内容 |
|---|---|---|---|
| GPU 予約量の推移 | Time series | Prometheus | gpu の GPU 予約量 vs クォータ上限 |
| GPU ノード CPU 予約量 | Time series | Prometheus | gpu の CPU 予約量 vs クォータ上限 |
| GPU ノード メモリ予約量 | Time series | Prometheus | gpu のメモリ予約量 vs クォータ上限（GiB 表示） |

#### Row 6: CPU Sub Flavor の詳細

| Panel | Type | DataSource | 内容 |
|---|---|---|---|
| CPU 予約量の推移 | Time series | Prometheus | cpu-sub の CPU 予約量 vs クォータ上限 |
| メモリ予約量の推移 | Time series | Prometheus | cpu-sub のメモリ予約量 vs クォータ上限（GiB 表示） |

#### Row 7: 待ち時間の分析

| Panel | Type | DataSource | 内容 |
|---|---|---|---|
| リソース割当て待ち時間の推移 (P50 / P95) | Time series | Prometheus | Kueue の admission wait time のパーセンタイル推移 |
| 最近のジョブ待ち時間 | Table | PostgreSQL | 直近 6 時間のジョブの実績待ち時間（started_at - created_at） |

#### Row 8: 時間帯別の傾向

| Panel | Type | DataSource | 内容 |
|---|---|---|---|
| 時間帯別の混雑度（過去 7 日平均） | Bar chart | Prometheus | 0-23 時の平均ジョブ投入数。空いている時間帯を狙ってジョブ投入できる |

### 3.3 メトリクスの単位（Kueue v0.16.4）

| リソース | メトリクス単位 | 表示単位 | 変換 |
|---|---|---|---|
| CPU | コア（float64） | コア | 変換不要 |
| メモリ | バイト（float64） | GiB | `/ 1024 / 1024 / 1024` |
| GPU | 個数（float64） | 個数 | 変換不要 |

Kueue v0.16.4 では `resource.Quantity.AsApproximateFloat64()` で変換されるため、メモリはバイト単位で報告される（例: `nominalQuota: "1000Gi"` → `1073741824000`）。

使用率ゲージ（Row 1）は usage / quota の比率のため、分子・分母が同単位で相殺されるため変換不要。

## 4. 主要クエリ

### 4.1 PromQL

```promql
# 成功率（直近 24 時間）
sum(increase(cjob_jobs_completed_total{status="succeeded"}[24h]))
/
sum(increase(cjob_jobs_completed_total{status=~"succeeded|failed"}[24h]))
* 100

# ジョブ投入数の推移（10 分間隔）
sum(increase(cjob_jobs_submitted_total[10m]))

# ジョブ完了数の推移（10 分間隔）
sum(increase(cjob_jobs_completed_total[10m]))

# 時間帯別混雑度（過去 7 日平均、パネル時間範囲: 24h）
(
  sum(increase(cjob_jobs_submitted_total[1h]))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 1d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 2d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 3d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 4d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 5d)) or vector(0))
  + (sum(increase(cjob_jobs_submitted_total[1h] offset 6d)) or vector(0))
) / 7
```

```promql
# CPU 使用率ゲージ
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="cpu"}
/
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="cpu"}

# CPU Sub 使用率ゲージ
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu-sub", resource="cpu"}
/
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="cpu-sub", resource="cpu"}

# GPU 使用率ゲージ
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="gpu-a100", resource="nvidia.com/gpu"}
/
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="gpu-a100", resource="nvidia.com/gpu"}

# 待ち時間 P50（直近 1 時間）
histogram_quantile(0.5, rate(kueue_admission_wait_time_seconds_bucket{cluster_queue="cjob-cluster-queue"}[1h]))

# 待ち時間 P95（直近 30 分）
histogram_quantile(0.95, rate(kueue_admission_wait_time_seconds_bucket{cluster_queue="cjob-cluster-queue"}[30m]))

# 実行中ワークロード数
kueue_admitted_active_workloads{cluster_queue="cjob-cluster-queue"}

# 待機中ワークロード数
sum(kueue_pending_workloads{cluster_queue="cjob-cluster-queue"})

# CPU 使用量（コア）
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="cpu"}

# メモリ使用量（GiB 変換）
kueue_cluster_queue_resource_usage{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="memory"} / 1024 / 1024 / 1024

# メモリクォータ上限（GiB 変換）
kueue_cluster_queue_nominal_quota{cluster_queue="cjob-cluster-queue", flavor="cpu", resource="memory"} / 1024 / 1024 / 1024
```

### 4.2 SQL

```sql
-- 最近のジョブ待ち時間（直近 6 時間）
SELECT
  namespace AS "ユーザー",
  job_id AS "Job ID",
  CASE WHEN gpu > 0 THEN 'GPU' ELSE 'CPU' END AS "種別",
  cpu || ' CPU / ' || memory || ' / GPU ' || gpu AS "リソース",
  EXTRACT(EPOCH FROM (started_at - created_at))::int AS "待ち時間(秒)",
  started_at AS "開始時刻"
FROM jobs
WHERE status IN ('RUNNING', 'SUCCEEDED', 'FAILED')
  AND started_at IS NOT NULL
  AND created_at >= NOW() - INTERVAL '6 hours'
ORDER BY started_at DESC
LIMIT 15;

-- ジョブ状態の内訳（直近 24 時間）
SELECT
  CASE status
    WHEN 'QUEUED' THEN '待機中'
    WHEN 'DISPATCHING' THEN '投入中'
    WHEN 'DISPATCHED' THEN '実行待ち'
    WHEN 'RUNNING' THEN '実行中'
    WHEN 'HELD' THEN '保留中'
    WHEN 'SUCCEEDED' THEN '成功'
    WHEN 'FAILED' THEN '失敗'
    WHEN 'CANCELLED' THEN 'キャンセル'
    WHEN 'DELETING' THEN '削除中'
    ELSE status
  END AS "状態",
  COUNT(*) AS "件数"
FROM jobs
WHERE created_at >= NOW() - INTERVAL '24 hours'
GROUP BY status
ORDER BY
  CASE status
    WHEN 'RUNNING' THEN 1
    WHEN 'QUEUED' THEN 2
    WHEN 'HELD' THEN 3
    WHEN 'DISPATCHING' THEN 4
    WHEN 'DISPATCHED' THEN 5
    WHEN 'SUCCEEDED' THEN 6
    WHEN 'FAILED' THEN 7
    WHEN 'CANCELLED' THEN 8
    WHEN 'DELETING' THEN 9
  END;

-- 実行中ジョブ数
SELECT COUNT(*) AS "実行中" FROM jobs WHERE status = 'RUNNING';

-- アクティブユーザー数
SELECT COUNT(DISTINCT namespace) AS "ユーザー数" FROM jobs WHERE status = 'RUNNING';

-- Flavor 別キュー使用状況（flavor 追加時もクエリ変更不要）
SELECT
  flavor AS "Flavor",
  COUNT(*) FILTER (WHERE status = 'RUNNING') AS "実行中",
  COUNT(*) FILTER (WHERE status IN ('QUEUED', 'DISPATCHING', 'DISPATCHED')) AS "待機中",
  COUNT(*) FILTER (WHERE status = 'HELD') AS "保留中"
FROM jobs
WHERE status IN ('RUNNING', 'QUEUED', 'DISPATCHING', 'DISPATCHED', 'HELD')
GROUP BY flavor
ORDER BY flavor;

-- クラスタノード数
SELECT COUNT(*) AS "ノード数" FROM node_resources;
```

## 5. ダッシュボード JSON

`k8s/base/grafana/dashboard-user.json` に Grafana Import 用の JSON ファイルを配置する。Grafana UI の `Dashboards > Import` から JSON ファイルをアップロードしてデプロイする。

Import 時にデータソースの UID を環境に合わせて設定する必要がある。JSON 内のデータソース参照は以下の変数名を使用している：

| 変数名 | データソース |
|---|---|
| `${DS_PROMETHEUS}` | Prometheus |
| `${DS_CJOB_DB}` | CJob PostgreSQL |

## 6. 運用上の注意

- Kueue メトリクスはコントローラの再起動直後にリセットされるため、一時的にデータが欠落する。ダッシュボードの time range を適切に設定すれば影響は軽微
- PostgreSQL クエリはインデックスを活用している（`idx_jobs_namespace_status`）。大量のジョブ蓄積（数十万件以上）がある場合はクエリパフォーマンスに注意
- `node_resources` テーブルは Watcher が 300 秒間隔で同期するため、ノード追加/削除の反映に最大 5 分の遅延がある

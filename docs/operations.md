# 運用ガイド

## 1. DB 状態の確認

### 1.1 PostgreSQL への接続

```bash
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob
```

以下のコマンドは `-c` オプションで直接実行することもできる。

```bash
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "<SQL>"
```

### 1.2 ジョブ一覧の確認

```bash
# 全ジョブの概要
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
SELECT namespace, job_id, status, command, created_at, started_at, finished_at
FROM jobs ORDER BY namespace, job_id;
"

# namespace ごとのステータス別ジョブ数
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
SELECT namespace, status, COUNT(*) AS count
FROM jobs GROUP BY namespace, status ORDER BY namespace, status;
"

# 特定ステータスのジョブを確認（例: RUNNING）
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
SELECT namespace, job_id, command, started_at, time_limit_seconds
FROM jobs WHERE status = 'RUNNING' ORDER BY started_at;
"
```

### 1.3 累計リソース消費量の確認

```bash
# 全 namespace の累計消費量
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
SELECT * FROM namespace_resource_usage ORDER BY namespace;
"

# DRF の dominant share を確認（Dispatcher のソート順序と同等）
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
SELECT
  namespace,
  cpu_millicores_seconds,
  memory_mib_seconds,
  gpu_seconds,
  GREATEST(
    cpu_millicores_seconds * 1.0 / 256000,
    memory_mib_seconds * 1.0 / 1024000,
    gpu_seconds * 1.0 / NULLIF(0, 0)
  ) AS dominant_share,
  period_start,
  updated_at
FROM namespace_resource_usage
ORDER BY dominant_share ASC NULLS FIRST;
"

# 集計期間の残り時間を確認
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
SELECT
  namespace,
  period_start,
  NOW() - period_start AS elapsed,
  MAKE_INTERVAL(secs => 604800) - (NOW() - period_start) AS remaining
FROM namespace_resource_usage ORDER BY namespace;
"
```

### 1.4 ジョブカウンターの確認

```bash
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
SELECT * FROM user_job_counters ORDER BY namespace;
"
```

### 1.5 滞留ジョブの確認

DISPATCHED のまま長時間経過しているジョブ（隙間充填の対象）を確認する。

```bash
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
SELECT namespace, job_id, dispatched_at,
  EXTRACT(EPOCH FROM NOW() - dispatched_at)::int AS elapsed_sec
FROM jobs
WHERE status = 'DISPATCHED'
ORDER BY dispatched_at;
"
```

### 1.6 RUNNING ジョブの残り時間

```bash
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
SELECT namespace, job_id, command, time_limit_seconds,
  started_at,
  EXTRACT(EPOCH FROM
    (started_at + MAKE_INTERVAL(secs => time_limit_seconds)) - NOW()
  )::int AS remaining_sec
FROM jobs
WHERE status = 'RUNNING' AND started_at IS NOT NULL
ORDER BY remaining_sec;
"
```

## 2. コンポーネントの状態確認

### 2.1 Pod の状態

```bash
kubectl get pods -n cjob-system
```

### 2.2 ログの確認

```bash
# Dispatcher
kubectl logs -n cjob-system deployment/dispatcher --tail=50

# Watcher
kubectl logs -n cjob-system deployment/watcher --tail=50

# Submit API
kubectl logs -n cjob-system deployment/submit-api --tail=50
```

### 2.3 ConfigMap の確認

```bash
kubectl get configmap cjob-config -n cjob-system -o yaml
```

## 3. 累計リソース消費量の手動リセット

特定の namespace の累計消費量を手動でリセットする場合。

```bash
# 特定 namespace のリセット
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
UPDATE namespace_resource_usage
SET cpu_millicores_seconds = 0,
    memory_mib_seconds = 0,
    gpu_seconds = 0,
    period_start = NOW(),
    updated_at = NOW()
WHERE namespace = '<namespace>';
"

# 全 namespace のリセット
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
UPDATE namespace_resource_usage
SET cpu_millicores_seconds = 0,
    memory_mib_seconds = 0,
    gpu_seconds = 0,
    period_start = NOW(),
    updated_at = NOW();
"
```

## 4. DB スキーマの更新

バージョンアップ時に新しいテーブルやカラムを追加する場合。`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` により冪等に実行できる。

```bash
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "
CREATE TABLE IF NOT EXISTS namespace_resource_usage (
    namespace              TEXT PRIMARY KEY,
    cpu_millicores_seconds BIGINT NOT NULL DEFAULT 0,
    memory_mib_seconds     BIGINT NOT NULL DEFAULT 0,
    gpu_seconds            BIGINT NOT NULL DEFAULT 0,
    period_start           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS time_limit_seconds INTEGER NOT NULL DEFAULT 86400;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
"
```

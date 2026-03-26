# PostgreSQL 設計

## 1. `jobs` テーブル

job_id はユーザー（namespace）ごとに 1 から始まる連番とする。
グローバルな一意性は `(namespace, job_id)` の複合主キーで保証する。

```sql
CREATE TABLE jobs (
    job_id        INTEGER NOT NULL,
    "user"        TEXT NOT NULL,
    namespace     TEXT NOT NULL,
    image         TEXT NOT NULL,           -- CLI が JUPYTER_IMAGE 環境変数から取得したコンテナイメージ名
    command       TEXT NOT NULL,
    cwd           TEXT NOT NULL,
    env_json      JSONB NOT NULL DEFAULT '{}',
    cpu           TEXT NOT NULL,
    memory        TEXT NOT NULL,
    gpu           INTEGER NOT NULL DEFAULT 0,
    time_limit_seconds INTEGER NOT NULL,   -- 実行時間上限（秒）。K8s Job の activeDeadlineSeconds に設定される
    status        TEXT NOT NULL,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    retry_after   TIMESTAMPTZ,              -- K8s 一時障害時の再試行解禁時刻（NULL = 即時対象）
    k8s_job_name  TEXT,
    log_dir       TEXT,          -- /home/jovyan/.cjob/logs/<job_id>
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at TIMESTAMPTZ,
    started_at    TIMESTAMPTZ,             -- Pod が RUNNING に遷移した時刻（Watcher が記録）
    finished_at   TIMESTAMPTZ,
    last_error    TEXT,
    PRIMARY KEY (namespace, job_id)
);

-- k8s_job_name による高速検索用インデックス（orphan Job 検出・API レスポンス等で使用）
-- ※ Watcher のジョブ特定には cjob.io/job-id ラベルを使用する（k8s_job_name 照合は使用しない）
CREATE INDEX idx_jobs_k8s_job_name ON jobs (k8s_job_name);

-- Dispatcher の dispatch budget 計算を効率化するためのインデックス
CREATE INDEX idx_jobs_namespace_status ON jobs (namespace, status);
```

## 2. `user_job_counters` テーブル

ユーザーごとの job_id 採番カウンタ。reset 時に 1 に戻す。

```sql
CREATE TABLE user_job_counters (
    namespace   TEXT PRIMARY KEY,
    next_id     INTEGER NOT NULL DEFAULT 1
);
```

採番は Submit API がアトミックに行う。

```sql
-- 採番クエリ（競合防止のため RETURNING で採番と increment を同時に行う）
INSERT INTO user_job_counters (namespace, next_id)
VALUES (:namespace, 2)
ON CONFLICT (namespace) DO UPDATE
    SET next_id = user_job_counters.next_id + 1
RETURNING next_id - 1;   -- 発行された job_id
```

## 3. `job_events` テーブル

```sql
CREATE TABLE job_events (
    id           BIGSERIAL PRIMARY KEY,
    namespace    TEXT NOT NULL,
    job_id       INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (namespace, job_id) REFERENCES jobs(namespace, job_id)
        ON DELETE CASCADE   -- jobs レコード削除時に job_events も連動削除
);
```

## 4. 状態遷移

```text
QUEUED
  ├─ CANCELLED（ユーザーがキャンセル → Dispatcher が次回スキャン時にスキップ）
  └─ DISPATCHING（Dispatcher が DB スキャンで選択し DISPATCHING に更新した時点）
       ├─ CANCELLED（ユーザーがキャンセル → CAS 前ならスキップ、CAS 後なら Watcher が K8s Job 削除）
       ├─ DISPATCHED（Kubernetes Job 作成成功）
       │    ├─ CANCELLED（ユーザーがキャンセル → Watcher が K8s Job を削除）
       │    └─ RUNNING（Watcher が Pod 実行中を検知）
       │         ├─ SUCCEEDED
       │         ├─ FAILED
       │         └─ CANCELLED（ユーザーがキャンセル → Watcher が K8s Job を削除）
       ├─ QUEUED（再試行時：Dispatcher 再起動・K8s 一時障害後の retry_after 差し戻し）
       └─ FAILED（バリデーションエラー・最大 retry 超過）
CANCELLED（QUEUED / DISPATCHING / DISPATCHED / RUNNING の任意タイミングでユーザーがキャンセル）
CANCELLED / SUCCEEDED / FAILED
  └─ DELETING（POST /v1/reset 受付後、これら3状態から一括で遷移する・Watcher による K8s Job 削除と DB クリーンアップ待ち）
       └─ （削除完了後、Watcher が DB レコードを削除・カウンターをリセット）
```

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

## 4. `namespace_resource_usage` テーブル

namespace ごとの累計リソース消費量を記録する。Dispatcher の fair sharing（dispatch 優先度の調整）に使用する。

`jobs` テーブルとは独立しており、`cjob reset` による jobs レコード削除の影響を受けない。

```sql
CREATE TABLE namespace_resource_usage (
    namespace              TEXT PRIMARY KEY,
    cpu_millicores_seconds BIGINT NOT NULL DEFAULT 0,
    memory_mib_seconds     BIGINT NOT NULL DEFAULT 0,
    gpu_seconds            BIGINT NOT NULL DEFAULT 0,
    period_start           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 4.1 カラム説明

| カラム | 型 | 説明 |
|---|---|---|
| `namespace` | TEXT PK | ユーザーの namespace |
| `cpu_millicores_seconds` | BIGINT | `time_limit_seconds × cpu（ミリコア換算）` の累計。"2" → 2000, "0.5" → 500 |
| `memory_mib_seconds` | BIGINT | `time_limit_seconds × memory（MiB 換算）` の累計。"4Gi" → 4096, "500Mi" → 500 |
| `gpu_seconds` | BIGINT | `time_limit_seconds × gpu（個数）` の累計 |
| `period_start` | TIMESTAMPTZ | 現在の集計期間の開始時刻 |
| `updated_at` | TIMESTAMPTZ | 最終更新時刻 |

### 4.2 加算処理

Watcher がジョブを RUNNING に遷移させる際に、`started_at` の記録と同じトランザクション内で累計消費量を加算する。

加算量の計算: `time_limit_seconds × リソース量`（方式 C: 予約のみ、返却なし）。ジョブが `time_limit_seconds` より早く完了しても返却しない。これにより、ユーザーが `time_limit_seconds` を適切に見積もるインセンティブが生まれ、隙間充填（gap filling）の推定精度も向上する。

CANCELLED に対する特別処理は不要。RUNNING 前のキャンセルは加算されておらず、RUNNING 中のキャンセルは既に加算済みで返却しない。

```sql
INSERT INTO namespace_resource_usage (namespace, cpu_millicores_seconds, memory_mib_seconds, gpu_seconds, period_start, updated_at)
VALUES (:namespace, :delta_cpu, :delta_mem, :delta_gpu, NOW(), NOW())
ON CONFLICT (namespace) DO UPDATE SET
    cpu_millicores_seconds = namespace_resource_usage.cpu_millicores_seconds + EXCLUDED.cpu_millicores_seconds,
    memory_mib_seconds     = namespace_resource_usage.memory_mib_seconds + EXCLUDED.memory_mib_seconds,
    gpu_seconds            = namespace_resource_usage.gpu_seconds + EXCLUDED.gpu_seconds,
    updated_at = NOW();
```

アトミックな UPSERT により、初回は INSERT（`period_start = NOW()`）、以降は加算のみ（`period_start` は変更しない）。

### 4.3 期間リセット

Dispatcher が累計消費量を参照する際に `period_start` を確認し、集計期間を超過していたらリセットする。

```sql
UPDATE namespace_resource_usage
SET cpu_millicores_seconds = 0,
    memory_mib_seconds = 0,
    gpu_seconds = 0,
    period_start = NOW(),
    updated_at = NOW()
WHERE NOW() - period_start > MAKE_INTERVAL(secs => :reset_interval_seconds);
```

リセット期間は `FAIR_SHARE_RESET_INTERVAL_SEC`（デフォルト 604800 秒 = 7 日）で設定する。

### 4.4 設計判断

- **jobs テーブルと独立**: FK を持たないため、`cjob reset` の `DELETE FROM jobs` に影響されない
- **namespace 単位で集約**: ジョブごとの明細は不要。方式 C（予約のみ）により RUNNING 遷移時に確定加算するため、後から個別ジョブの寄与を差し引く必要がない
- **リソース種別ごとにカラムを分離**: CPU・メモリ・GPU の消費パターンが異なるため、Dispatcher が重み付けを柔軟に設定できるよう分離する
- **BIGINT の十分性**: `time_limit_seconds`（最大 604800）× `cpu_millicores`（最大 300000）でも 1 回あたり最大約 1.8 × 10^11。BIGINT（最大 9.2 × 10^18）で十分
- **期間ベースのリセット**: 外部 CronJob ではなく Dispatcher が参照時にリセットする方式を採用。コンポーネントを増やさずシンプルに実現できる

## 5. 状態遷移

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

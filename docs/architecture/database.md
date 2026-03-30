# PostgreSQL 設計

## 1. `jobs` テーブル

job_id はユーザー（namespace）ごとに 1 から始まる連番とする。
グローバルな一意性は `(namespace, job_id)` の複合主キーで保証する。

```sql
CREATE TABLE jobs (
    job_id        INTEGER NOT NULL,
    "user"        TEXT NOT NULL,
    namespace     TEXT NOT NULL,
    image         TEXT NOT NULL,           -- CLI が CJOB_IMAGE 環境変数から取得（未設定時は JUPYTER_IMAGE にフォールバック）
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
    completions       INTEGER,       -- sweep のタスク総数。NULL = 通常ジョブ
    parallelism       INTEGER,       -- sweep の同時実行数
    completed_indexes TEXT,          -- 成功インデックス（K8s 圧縮表記、例: "0-49,51-99"）
    failed_indexes    TEXT,          -- 失敗インデックス（K8s 圧縮表記、例: "50"）
    succeeded_count   INTEGER,       -- 成功タスク数
    failed_count      INTEGER,       -- 失敗タスク数
    node_name         TEXT,          -- ジョブ実行ノード名（Watcher が RUNNING 遷移時に記録。RUNNING スキップ時は完了遷移時に取得）
    PRIMARY KEY (namespace, job_id)
);

-- k8s_job_name による高速検索用インデックス（orphan Job 検出・API レスポンス等で使用）
-- ※ Watcher のジョブ特定には cjob.io/job-id ラベルを使用する（k8s_job_name 照合は使用しない）
CREATE INDEX idx_jobs_k8s_job_name ON jobs (k8s_job_name);

-- Dispatcher の dispatch budget 計算を効率化するためのインデックス
CREATE INDEX idx_jobs_namespace_status ON jobs (namespace, status);
```

`completions IS NULL` で通常ジョブと sweep ジョブを判別する。sweep ジョブの場合、`completed_indexes` / `failed_indexes` は K8s API の `status.completedIndexes` / `status.failedIndexes`（圧縮表記文字列）を Watcher が書き込む。`succeeded_count` / `failed_count` は `completed_indexes` のパースなしに集計値を参照するためのキャッシュカラムである。

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

## 4. `namespace_weights` テーブル

namespace ごとの fair sharing の重み（weight）を管理する。weight が大きい namespace ほど、同じ累計消費量でも DRF の dominant share が小さく評価され、dispatch 優先度が高くなる。

```sql
CREATE TABLE namespace_weights (
    namespace TEXT PRIMARY KEY,
    weight    INTEGER NOT NULL DEFAULT 1
);
```

Dispatcher の DRF ソートでは `dominant_share / weight` でソートする。テーブルに行がない namespace は weight = 1 として扱う（`COALESCE(w.weight, 1)`）。

- **weight = 0**: dispatch 対象から除外される（使用禁止）。ジョブは QUEUED に留まり、weight を 1 以上に戻すと dispatch が再開される。管理者が特定ユーザーにクラスタ全体を専有させたい場合に、他ユーザーの weight を 0 に設定することで実現できる
- **weight ≥ 1**: weight が大きい namespace ほど、同じ累計消費量でも dominant share が小さく評価され、dispatch 優先度が高くなる。例えば weight = 2 の namespace は、weight = 1 の namespace より多くのリソースを使い切るまで優先的に dispatch される

## 5. `namespace_daily_usage` テーブル

namespace ごとの日別リソース消費量を記録する。Dispatcher の fair sharing（dispatch 優先度の調整）に使用する。直近 `FAIR_SHARE_WINDOW_DAYS` 日分の合計をスライディングウィンドウで計算し、DRF の dominant share を求める。

`jobs` テーブルとは独立しており、`cjob reset` による jobs レコード削除の影響を受けない。

```sql
CREATE TABLE namespace_daily_usage (
    namespace              TEXT NOT NULL,
    usage_date             DATE NOT NULL,
    cpu_millicores_seconds BIGINT NOT NULL DEFAULT 0,
    memory_mib_seconds     BIGINT NOT NULL DEFAULT 0,
    gpu_seconds            BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (namespace, usage_date)
);
```

### 5.1 カラム説明

| カラム | 型 | 説明 |
|---|---|---|
| `namespace` | TEXT | ユーザーの namespace |
| `usage_date` | DATE | 消費が記録された日（UTC） |
| `cpu_millicores_seconds` | BIGINT | `time_limit_seconds × cpu（ミリコア換算）` のその日の合計。"2" → 2000, "0.5" → 500 |
| `memory_mib_seconds` | BIGINT | `time_limit_seconds × memory（MiB 換算）` のその日の合計。"4Gi" → 4096, "500Mi" → 500 |
| `gpu_seconds` | BIGINT | `time_limit_seconds × gpu（個数）` のその日の合計 |

### 5.2 加算処理

Watcher がジョブを RUNNING に遷移させる際に、`started_at` の記録と同じトランザクション内で当日分の消費量を加算する。

加算量の計算: `time_limit_seconds × リソース量`（方式 C: 予約のみ、返却なし）。sweep ジョブの場合は `time_limit_seconds × リソース量 × parallelism`（同時に使用するリソースの最大量を反映）。ジョブが `time_limit_seconds` より早く完了しても返却しない。これにより、ユーザーが `time_limit_seconds` を適切に見積もるインセンティブが生まれ、隙間充填（gap filling）の推定精度も向上する。

CANCELLED に対する特別処理は不要。RUNNING 前のキャンセルは加算されておらず、RUNNING 中のキャンセルは既に加算済みで返却しない。

```sql
INSERT INTO namespace_daily_usage (namespace, usage_date, cpu_millicores_seconds, memory_mib_seconds, gpu_seconds)
VALUES (:namespace, CURRENT_DATE, :delta_cpu, :delta_mem, :delta_gpu)
ON CONFLICT (namespace, usage_date) DO UPDATE SET
    cpu_millicores_seconds = namespace_daily_usage.cpu_millicores_seconds + EXCLUDED.cpu_millicores_seconds,
    memory_mib_seconds     = namespace_daily_usage.memory_mib_seconds + EXCLUDED.memory_mib_seconds,
    gpu_seconds            = namespace_daily_usage.gpu_seconds + EXCLUDED.gpu_seconds;
```

アトミックな UPSERT により、その日の初回は INSERT、以降は加算。

### 5.3 ウィンドウ集計

Dispatcher が `fetch_dispatchable_jobs()` で DRF の dominant share を計算する際に、直近 `FAIR_SHARE_WINDOW_DAYS` 日分の消費量を集計する。

```sql
-- window_days=7 の場合: CURRENT_DATE - 7 より後 = 6日前〜当日の 7 日分が集計対象
-- （ちょうど 7 日前の行は §5.4 で削除済みかつ本条件でも対象外）
SELECT namespace,
       SUM(cpu_millicores_seconds) AS cpu_millicores_seconds,
       SUM(memory_mib_seconds) AS memory_mib_seconds,
       SUM(gpu_seconds) AS gpu_seconds
FROM namespace_daily_usage
WHERE usage_date > CURRENT_DATE - :window_days
GROUP BY namespace
```

ウィンドウの外側にある古い日は自然に集計対象から外れる。毎日、最も古い日が脱落するため、一括リセットの断崖が生じない。

### 5.4 古い行の削除

Dispatcher が `fetch_dispatchable_jobs()` を実行する直前に、ウィンドウ外の古い行を削除する。

```sql
DELETE FROM namespace_daily_usage
WHERE usage_date <= CURRENT_DATE - :window_days;
```

### 5.5 設計判断

- **jobs テーブルと独立**: FK を持たないため、`cjob reset` の `DELETE FROM jobs` に影響されない
- **日別に分割**: スライディングウィンドウにより、一括リセットの断崖（リセット直後に全員の消費量が 0 になる問題）を解消する。毎日、最も古い日が自然に脱落するため、消費量の変化が滑らかになる
- **リソース種別ごとにカラムを分離**: CPU・メモリ・GPU の消費パターンが異なるため、Dispatcher が重み付けを柔軟に設定できるよう分離する
- **BIGINT の十分性**: `time_limit_seconds`（最大 604800）× `cpu_millicores`（最大 300000）でも 1 日あたり最大約 1.8 × 10^11。BIGINT（最大 9.2 × 10^18）で十分
- **行数の見積もり**: namespace 数 × ウィンドウ日数。20 namespace × 7 日 = 140 行程度であり、集計クエリのコストは無視できる

## 6. `node_resources` テーブル

クラスタ内の計算ノードごとの allocatable リソースを記録する。Watcher が K8s API からノード情報を定期取得（`NODE_RESOURCE_SYNC_INTERVAL_SEC`、デフォルト 300 秒）し、UPSERT で更新する。

```sql
CREATE TABLE node_resources (
    node_name           TEXT PRIMARY KEY,
    cpu_millicores      INTEGER NOT NULL,    -- allocatable CPU（ミリコア）
    memory_mib          INTEGER NOT NULL,    -- allocatable memory（MiB）
    gpu                 INTEGER NOT NULL DEFAULT 0,  -- allocatable GPU（nvidia.com/gpu）
    flavor              TEXT NOT NULL DEFAULT 'cpu', -- ResourceFlavor 区分（'cpu' or 'gpu'）
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`flavor` 列は Watcher がノードの取得元セレクタに基づいて設定する。`NODE_LABEL_SELECTOR` 由来のノードは `'cpu'`、`GPU_NODE_LABEL_SELECTOR` 由来のノードは `'gpu'` となる。`DEFAULT 'cpu'` により、GPU セレクタ未設定の環境や既存データとの後方互換性を確保する。

### 6.1 同期処理

Watcher が `NODE_LABEL_SELECTOR`（デフォルト `cluster-job=true`）および `GPU_NODE_LABEL_SELECTOR`（デフォルト `cluster-gpu-job=true`）に一致するノードの `status.allocatable` を取得し、ノードごとに UPSERT する。DB に存在するが K8s から消えたノード（撤去・ラベル除去）は DELETE する。

```sql
-- UPSERT（ノードごと）
INSERT INTO node_resources (node_name, cpu_millicores, memory_mib, gpu, flavor, updated_at)
VALUES (:name, :cpu, :mem, :gpu, :flavor, NOW())
ON CONFLICT (node_name) DO UPDATE SET
    cpu_millicores = :cpu,
    memory_mib = :mem,
    gpu = :gpu,
    flavor = :flavor,
    updated_at = NOW();

-- K8s から消えたノードの削除
DELETE FROM node_resources WHERE node_name != ALL(:current_node_names);
```

### 6.2 参照パターン

**Submit API（リソース超過リジェクト判定）**: 各リソースについて、全ノードの最大値を取得する。要求リソースがいずれかのノードの allocatable を超える場合、そのジョブは原理的に実行不可能であるため 400 でリジェクトする。

```sql
SELECT MAX(cpu_millicores) AS max_cpu,
       MAX(memory_mib) AS max_memory,
       MAX(gpu) AS max_gpu
FROM node_resources;
```

**Dispatcher（DRF 正規化）**: クラスタ全体のリソース合計を取得する。従来 ConfigMap で手動設定していた `CLUSTER_TOTAL_CPU_MILLICORES` / `CLUSTER_TOTAL_MEMORY_MIB` / `CLUSTER_TOTAL_GPUS` の代わりに使用する。

```sql
SELECT COALESCE(SUM(cpu_millicores), 0) AS total_cpu,
       COALESCE(SUM(memory_mib), 0) AS total_memory,
       COALESCE(SUM(gpu), 0) AS total_gpu
FROM node_resources;
```

**cjobctl（flavor 別 allocatable 合計）**: `set-quota` のバリデーションで、指定 flavor に対応するノード群の allocatable 合計を取得する。

```sql
SELECT COALESCE(SUM(cpu_millicores), 0) AS total_cpu,
       COALESCE(SUM(memory_mib), 0) AS total_memory,
       COALESCE(SUM(gpu), 0) AS total_gpu
FROM node_resources
WHERE flavor = :flavor;
```

### 6.3 設計判断

- **ノードごとに行を持つ理由**: Submit API のリジェクト判定には「単一ノードの最大 allocatable」が必要であり、クラスタ合計だけでは不十分。ノードごとのデータを保持することで、MAX() によるリジェクト判定と SUM() による DRF 正規化の両方を単一テーブルで実現する
- **updated_at**: cjobctl でノード情報の鮮度を確認するために使用する。Watcher が停止した場合に古いデータを検知可能にする
- **行数の見積もり**: 計算ノード数と同数。10〜50 ノード程度を想定しており、クエリのコストは無視できる
- **テーブルが空の場合のフォールバック**: Watcher 未起動時は `node_resources` が空となる。Submit API はバリデーションをスキップし、Dispatcher は DRF ソートを無効化して namespace 名順にフォールバックする。これにより Watcher 起動前でもシステムが動作する

## 7. 状態遷移

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

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
    flavor        TEXT NOT NULL DEFAULT 'cpu', -- ジョブ実行先の ResourceFlavor 名（例: 'cpu', 'gpu-a100'）
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
    node_name         TEXT,          -- ジョブ実行ノード名（Watcher が RUNNING 遷移時に記録。RUNNING スキップ時は完了遷移時に取得。sweep ジョブでは最初の Pod のノード名のみ記録される）
    cpu_millicores    INTEGER,       -- cpu 文字列のパース済み数値（ミリコア）。"500m" → 500, "2" → 2000。Dispatcher の in-flight CTE で使用
    memory_mib        INTEGER,       -- memory 文字列のパース済み数値（MiB）。"4Gi" → 4096, "500Mi" → 500。Dispatcher の in-flight CTE で使用
    PRIMARY KEY (namespace, job_id)
);

-- k8s_job_name による高速検索用インデックス（orphan Job 検出・API レスポンス等で使用）
-- ※ Watcher のジョブ特定には cjob.io/job-id ラベルを使用する（k8s_job_name 照合は使用しない）
CREATE INDEX idx_jobs_k8s_job_name ON jobs (k8s_job_name);

-- Dispatcher の dispatch budget 計算を効率化するためのインデックス
CREATE INDEX idx_jobs_namespace_status ON jobs (namespace, status);
```

`completions IS NULL` で通常ジョブと sweep ジョブを判別する。sweep ジョブの場合、`completed_indexes` / `failed_indexes` は K8s API の `status.completedIndexes` / `status.failedIndexes`（圧縮表記文字列）を Watcher が書き込む。`succeeded_count` / `failed_count` は `completed_indexes` のパースなしに集計値を参照するためのキャッシュカラムである。

`cpu_millicores` / `memory_mib` は `cpu` / `memory` 文字列カラムの非正規化数値表現であり、Submit API がジョブ作成時に `parse_cpu_millicores()` / `parse_memory_mib()` で設定する。Dispatcher の DRF クエリで DISPATCHING/DISPATCHED ジョブの予測消費量を SQL 内で集計するために使用する（[dispatcher.md](dispatcher.md) §1.2 参照）。

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
    flavor              TEXT NOT NULL DEFAULT 'cpu', -- ResourceFlavor 名（RESOURCE_FLAVORS 設定の name と一致）
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`flavor` 列は Watcher がノードの取得元セレクタに基づいて設定する。`RESOURCE_FLAVORS` 設定（[resources.md](resources.md) 参照）の各 flavor 定義の `label_selector` で取得したノードに、その flavor の `name` を設定する。`DEFAULT 'cpu'` により、既存データとの後方互換性を確保する。

### 6.1 同期処理

Watcher が `RESOURCE_FLAVORS` 設定（[resources.md](resources.md) 参照）の各 flavor 定義の `label_selector` に一致するノードの `status.allocatable` を取得し、ノードごとに UPSERT する。DB に存在するが K8s から消えたノード（撤去・ラベル除去）は DELETE する。

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

**Submit API（リソース超過リジェクト判定）**: 指定された flavor のノードに限定して各リソースの最大値を取得する。`flavor_quotas` テーブルの nominalQuota と合わせて、有効上限を `min(最大ノード allocatable, nominalQuota)` で決定する。要求リソースが有効上限を超える場合、400 でリジェクトする。

```sql
SELECT MAX(cpu_millicores) AS max_cpu,
       MAX(memory_mib) AS max_memory,
       MAX(gpu) AS max_gpu
FROM node_resources
WHERE flavor = :flavor;
```

**Dispatcher（DRF 正規化）**: クラスタ全体のリソース合計を取得する。従来 ConfigMap で手動設定していた `CLUSTER_TOTAL_CPU_MILLICORES` / `CLUSTER_TOTAL_MEMORY_MIB` / `CLUSTER_TOTAL_GPUS` の代わりに使用する。

```sql
SELECT COALESCE(SUM(cpu_millicores), 0) AS total_cpu,
       COALESCE(SUM(memory_mib), 0) AS total_memory,
       COALESCE(SUM(gpu), 0) AS total_gpu
FROM node_resources;
```

**cjobctl（flavor 別 allocatable 合計）**: `set-quota` のバリデーションで、指定 flavor に対応するノード群の allocatable 合計を取得する。flavor 名は Kueue ResourceFlavor 名と統一されているため、変換処理なしでそのままクエリに使用する。

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
- **flavor 名の統一**: `node_resources.flavor` と `jobs.flavor` の値は Kueue ResourceFlavor の `metadata.name` と一致させる。これにより DB クエリと Kueue API の間で名前変換が不要になる

## 7. `flavor_quotas` テーブル

ClusterQueue の各 ResourceFlavor に対する nominalQuota を記録する。Watcher が K8s API から ClusterQueue を定期取得（`node_resources` と同じサイクル）し、UPSERT で更新する。

```sql
CREATE TABLE IF NOT EXISTS flavor_quotas (
    flavor      TEXT PRIMARY KEY,
    cpu         TEXT NOT NULL,
    memory      TEXT NOT NULL,
    gpu         TEXT NOT NULL DEFAULT '0',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`cpu`・`memory`・`gpu` は nominalQuota の値を K8s リソース量文字列のまま保存する（例: `"256"`、`"1000Gi"`、`"4"`）。CLI でそのまま表示に使用するため、パース→復元による情報損失を避ける。

### 7.1 同期処理

Watcher が `CustomObjectsApi.get_cluster_custom_object()` で ClusterQueue を取得し、`spec.resourceGroups[0].flavors[]` の各 flavor について `resources[]` から nominalQuota を読み取り、UPSERT する。DB に存在するが ClusterQueue にない flavor は DELETE する。

```sql
-- UPSERT（flavor ごと）
INSERT INTO flavor_quotas (flavor, cpu, memory, gpu, updated_at)
VALUES (:flavor, :cpu, :memory, :gpu, NOW())
ON CONFLICT (flavor) DO UPDATE SET
    cpu = :cpu,
    memory = :memory,
    gpu = :gpu,
    updated_at = NOW();

-- ClusterQueue から消えた flavor の削除
DELETE FROM flavor_quotas WHERE flavor != ALL(:current_flavors);
```

### 7.2 参照パターン

**Submit API（リソース超過リジェクト判定）**: ジョブ投入時に指定 flavor の nominalQuota を取得し、`node_resources` の MAX 値と合わせて有効上限を `min(max_node_allocatable, nominalQuota)` で決定する。sweep ではクラスタ全体チェックの上限を `min(allocatable 合計, nominalQuota)` で決定する。

```sql
SELECT cpu, memory, gpu
FROM flavor_quotas
WHERE flavor = :flavor;
```

**Submit API（`GET /v1/flavors`）**: 各 flavor の nominalQuota を取得し、CLI に返す。CLI は `node_resources` の MAX 値と合わせて、タスクあたりのリソース上限（`min(max_node_allocatable, nominalQuota)`）を計算・表示する。

```sql
SELECT flavor, cpu, memory, gpu
FROM flavor_quotas;
```

### 7.3 設計判断

- **TEXT 保存**: nominalQuota を K8s リソース量文字列のまま保存する。CLI の表示で "1000Gi" をそのまま使用でき、数値パース→復元の情報損失（例: 1000Gi → 1024000 MiB → 復元不可）を回避する。DB 上でのリソース量演算は不要
- **テーブルが空の場合のフォールバック**: Watcher 未同期時は `flavor_quotas` が空となる。Submit API のリソースバリデーションは `node_resources` の allocatable のみで判定する。`GET /v1/flavors` は `quota: null` を返し、CLI は「リソース情報がまだ取得されていません」と表示する
- **行数の見積もり**: flavor 数と同数。2〜5 flavor 程度を想定しており、クエリのコストは無視できる

## 8. `namespace_resource_quotas` テーブル

各 user namespace の ResourceQuota 使用状況を記録する。Watcher が K8s API から ResourceQuota を定期取得（`node_resources` と同じサイクル）し、UPSERT で更新する。Dispatcher が dispatch 前に残リソースを確認し、不足時はジョブを QUEUED に留めるために使用する。

```sql
CREATE TABLE namespace_resource_quotas (
    namespace            TEXT PRIMARY KEY,
    hard_cpu_millicores  INTEGER NOT NULL,
    hard_memory_mib      INTEGER NOT NULL,
    hard_gpu             INTEGER NOT NULL DEFAULT 0,
    used_cpu_millicores  INTEGER NOT NULL,
    used_memory_mib      INTEGER NOT NULL,
    used_gpu             INTEGER NOT NULL DEFAULT 0,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`hard_*` は `spec.hard` の値、`used_*` は `status.used` の値をパース済み数値で保存する。CPU はミリコア、メモリは MiB、GPU は個数。`node_resources` と同じく数値で保存する理由は、Dispatcher が Python 側で残リソース（hard - used）を算出し、ジョブのリソース要求と比較するためである。

### 8.1 同期処理

Watcher が K8s API から `USER_NAMESPACE_LABEL` ラベルを持つ全ユーザー namespace を取得し、各 namespace の ResourceQuota を K8s API から読み取って UPSERT する。ジョブの有無に関わらず全ユーザー namespace を追跡対象とする（JupyterHub 等の User Pod によるリソース消費をジョブ投入前から把握するため）。ユーザー namespace でなくなった namespace の行は DELETE する。

```sql
-- UPSERT（namespace ごと）
INSERT INTO namespace_resource_quotas
(namespace, hard_cpu_millicores, hard_memory_mib, hard_gpu,
 used_cpu_millicores, used_memory_mib, used_gpu, updated_at)
VALUES (:ns, :h_cpu, :h_mem, :h_gpu, :u_cpu, :u_mem, :u_gpu, NOW())
ON CONFLICT (namespace) DO UPDATE SET
    hard_cpu_millicores = :h_cpu, hard_memory_mib = :h_mem, hard_gpu = :h_gpu,
    used_cpu_millicores = :u_cpu, used_memory_mib = :u_mem, used_gpu = :u_gpu,
    updated_at = NOW();

-- active でなくなった namespace の削除
DELETE FROM namespace_resource_quotas WHERE namespace NOT IN (:active_namespaces);
```

ResourceQuota が存在しない namespace（K8s API が 404 を返す場合）は行を DELETE する。これにより Dispatcher はその namespace に制限なしとして扱う。K8s API の一時的なエラー（500 等）の場合は既存データを保持し、次回サイクルで再試行する。

GPU の値は `RESOURCE_FLAVORS` 設定（[resources.md](resources.md) 参照）の各 flavor 定義の `gpu_resource_name` を使用して ResourceQuota から `requests.{gpu_resource_name}` を取得する。複数の GPU リソース名が設定されている場合は、最初に見つかった非ゼロの値を使用する。

### 8.2 参照パターン

**Dispatcher（ResourceQuota プレチェック）**: dispatch 候補の namespace に対して残リソースを取得し、ジョブのリソース要求と比較する。

```sql
SELECT namespace, hard_cpu_millicores, hard_memory_mib, hard_gpu,
       used_cpu_millicores, used_memory_mib, used_gpu
FROM namespace_resource_quotas
WHERE namespace IN (:candidate_namespaces);
```

テーブルに行がない namespace は ResourceQuota が存在しないか Watcher が未同期であり、制限なしとして dispatch する。

**Usage API（ResourceQuota 表示）**: `GET /v1/usage` で自 namespace の ResourceQuota 使用状況を返す。

```sql
SELECT hard_cpu_millicores, hard_memory_mib, hard_gpu,
       used_cpu_millicores, used_memory_mib, used_gpu
FROM namespace_resource_quotas
WHERE namespace = :namespace;
```

行がない場合はレスポンスの `resource_quota` を `null` とする。

**cjobctl（管理者向け ResourceQuota 一覧）**: `cjobctl usage quota` で全ユーザー namespace の ResourceQuota 使用状況を一覧表示する。K8s API からユーザー namespace 一覧を取得し、DB と突き合わせる。

```sql
SELECT namespace, hard_cpu_millicores, hard_memory_mib, hard_gpu,
       used_cpu_millicores, used_memory_mib, used_gpu, updated_at
FROM namespace_resource_quotas
ORDER BY namespace;
```

DB に行がない namespace は ResourceQuota 未設定として `-` 表示する。

### 8.3 設計判断

- **数値パース済み保存**: `node_resources` と同じ理由。Dispatcher が Python 側で hard - used の残リソースを算出し、ジョブの `cpu_millicores` / `memory_mib` / `gpu` と比較する
- **hard/used 両方を保持**: remaining（hard - used）だけでなく元の値を保持することで、cjobctl での表示やデバッグ時に使用状況を確認できる
- **行なし = 制限なし**: ResourceQuota が存在しない namespace や Watcher 未同期時はテーブルが空となる。Dispatcher はこれらの namespace に対して制限なしとして dispatch する。`node_resources` / `flavor_quotas` のフォールバックパターンと一貫する
- **行数の見積もり**: active な namespace 数と同数。20 namespace 程度を想定しており、クエリのコストは無視できる

## 9. 状態遷移

```text
QUEUED
  ├─ HELD（ユーザーが保留 → Dispatcher がスキップ。release で QUEUED に戻る）
  │    ├─ QUEUED（ユーザーが release で保留解除）
  │    └─ CANCELLED（ユーザーがキャンセル）
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

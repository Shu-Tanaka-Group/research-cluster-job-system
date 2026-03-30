# Dispatcher 設計

## 1. スケジューリング設計

### 1.1 スケジューリング方針

Dispatcher は PostgreSQL を定期的にスキャンし、以下の基準で dispatch するジョブを選択する。

1. **budget に余裕のある namespace のみ対象とする**（DISPATCHING + DISPATCHED + RUNNING < dispatch_limit）
2. **対象 namespace の QUEUED ジョブを `created_at` 昇順で取得する**
3. **1サイクルあたりの取得数を `DISPATCH_BATCH_SIZE`（デフォルト 50）で固定する**
4. **namespace 間を公平にラウンドロビンする**（各 namespace から `DISPATCH_ROUND_SIZE` 件ずつ交互に取得）
5. **累計リソース消費量の dominant share が小さい namespace を優先する**（DRF による公平性）

この方式により：
- budget を使い切ったユーザーのジョブが他ユーザーをブロックしない
- 同一ユーザーの投入順（`created_at` 昇順）は常に保証される
- 複数ユーザーが同時に QUEUED 状態でも公平に処理される
- namespace 数が `DISPATCH_BATCH_SIZE` を超えても累計消費量による優先で公平性が維持される
- 1サイクルあたりの dispatch 数が固定されるため K8s API への負荷が予測可能になる

**Fair sharing（DRF）：** namespace ごとの直近 `FAIR_SHARE_WINDOW_DAYS` 日分のリソース消費量（[database.md](database.md) §5 の `namespace_daily_usage` テーブル）を参照し、Dominant Resource Fairness（DRF）に基づいて dispatch 優先度を決定する。各リソース（CPU・メモリ・GPU）をクラスタ全体の容量で正規化し、最大値（dominant share）を namespace の weight（[database.md](database.md) §4）で割った値が小さい namespace を優先的に dispatch する。これにより、リソースを多く消費した namespace の優先度が下がり、消費の少ない namespace にリソースが行き渡る。weight が大きい namespace はより多くのリソースを消費するまで優先され続ける。日別の消費量をスライディングウィンドウで集計するため、一括リセットの断崖が生じない。

DRF 正規化に使用するクラスタ全体の容量は、`node_resources` テーブル（[database.md](database.md) §6）から `SUM()` で動的に取得する。Watcher がノードの `allocatable` を定期的に同期するため、ノードの追加・撤去が自動的に反映される。`node_resources` テーブルが空の場合（Watcher 未起動時）は DRF ソートを無効化し、namespace 名順にフォールバックする。

### 1.2 DB スキャンのクエリ方針

```sql
-- active CTE: namespace ごとの active ジョブ数を集計（budget 制御用）
WITH active AS (
  SELECT namespace, COUNT(*) AS active_count
  FROM jobs
  WHERE status IN ('DISPATCHING', 'DISPATCHED', 'RUNNING')
  GROUP BY namespace
),
-- queued CTE: QUEUED ジョブのみに namespace 内の投入順（rn）を付与
queued AS (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY namespace ORDER BY created_at ASC
  ) AS rn
  FROM jobs
  WHERE status = 'QUEUED'
    AND (retry_after IS NULL OR retry_after <= NOW())
)
SELECT q.* FROM queued q
  LEFT JOIN active a USING (namespace)
  LEFT JOIN (                              -- 直近 N 日のウィンドウ集計
    SELECT namespace,
           SUM(cpu_millicores_seconds) AS cpu_millicores_seconds,
           SUM(memory_mib_seconds) AS memory_mib_seconds,
           SUM(gpu_seconds) AS gpu_seconds
    FROM namespace_daily_usage
    WHERE usage_date > CURRENT_DATE - :window_days
    GROUP BY namespace
  ) u ON q.namespace = u.namespace
  LEFT JOIN namespace_weights w ON q.namespace = w.namespace
WHERE COALESCE(a.active_count, 0) < :dispatch_limit          -- budget に余裕がある namespace のみ
  AND q.rn <= :dispatch_limit - COALESCE(a.active_count, 0)  -- 残り budget 分だけ取得
  AND COALESCE(w.weight, 1) > 0                               -- weight=0 の namespace は dispatch 対象外
ORDER BY CEIL(q.rn * 1.0 / :round_size) ASC,  -- ラウンドロビン（各 namespace から round_size 件ずつ交互）
         GREATEST(                         -- DRF: dominant share / weight が小さい namespace を優先
           COALESCE(u.cpu_millicores_seconds, 0)::float / :cluster_cpu_millicores,
           COALESCE(u.memory_mib_seconds, 0)::float / :cluster_memory_mib,
           COALESCE(u.gpu_seconds, 0)::float / NULLIF(:cluster_gpus, 0)  -- GPU=0 のクラスタでは NULL → GREATEST が無視し CPU/mem のみで判定
         ) / COALESCE(w.weight, 1) ASC NULLS FIRST,  -- 消費量レコードなし(NULL)の namespace が最優先
         q.namespace ASC                   -- 同率の場合は namespace 名で決定的に順序付け
LIMIT :batch_size;                         -- 1サイクルの総取得数を固定
```

このクエリは `idx_jobs_namespace_status` インデックスにより効率化される。`namespace_daily_usage` のウィンドウ集計はサブクエリで行い、namespace ごとに 1 行に集約されるため JOIN のコストは無視できる（20 namespace × 7 日 = 140 行程度）。

**ラウンドロビンの仕組み：** `ROW_NUMBER()` を QUEUED ジョブのみに振り、`CEIL(rn / round_size)` でグループ化することで、各 namespace から `DISPATCH_ROUND_SIZE` 件ずつ交互に取得する。デフォルト（`round_size = 1`）では各 namespace から 1 件ずつ交互に並び、`round_size = 5` なら 5 件ずつまとめて並ぶ。`LIMIT :batch_size` で打ち切ることで、1 サイクルの dispatch 数が制限される。

**公平性の保証（DRF）：** 直近 `FAIR_SHARE_WINDOW_DAYS` 日分のリソース消費量をクラスタ容量で正規化し、`GREATEST` で最大値（dominant share）を求め、namespace の weight（`namespace_weights` テーブル、デフォルト 1）で割ってソートする。クラスタ容量は `node_resources` テーブルから `SUM()` で動的に取得する（[database.md](database.md) §6.2 参照）。これにより、支配的リソースの消費割合が小さい namespace が優先され、weight が大きい namespace はより多くのリソースを消費するまで優先され続ける。`namespace_daily_usage` にレコードがない namespace は消費量 0 として扱われ（`COALESCE` + `NULLS FIRST`）、最優先で dispatch される。GPU が 0 のクラスタでは `NULLIF(:cluster_gpus, 0)` により GPU の項が NULL となり、DRF の計算から除外される。`node_resources` テーブルが空の場合は DRF ソートを無効化し、namespace 名順にフォールバックする。

**古い行の削除：** `fetch_dispatchable_jobs()` の実行前に、ウィンドウ外の古い行を削除する（[database.md](database.md) §5.4 参照）。

### 1.3 再試行の管理

K8s API 一時障害時の再試行は `jobs.retry_after` タイムスタンプで管理する。
RabbitMQ の DLQ・TTL は不要。

```sql
-- 一時障害時: retry_after を設定して QUEUED に戻す
-- AND status = 'DISPATCHING' により CANCELLED を上書きしない
UPDATE jobs
SET retry_count = retry_count + 1,
    retry_after = NOW() + INTERVAL '30 seconds',  -- DISPATCH_RETRY_INTERVAL_SEC 秒後
    status = 'QUEUED'
WHERE namespace = :namespace
  AND job_id    = :job_id
  AND status    = 'DISPATCHING';   -- CANCELLED を上書きしない
```

`retry_after IS NULL OR retry_after <= NOW()` の条件で次回スキャン時に自動的に再試行される。

## 2. Dispatcher 詳細設計

### 2.1 役割

Dispatcher は PostgreSQL をスキャンして QUEUED ジョブを選択し、Kubernetes Job を作成する。

- DB を定期スキャンして dispatch 対象ジョブを選択する
- namespace 間の公平なスケジューリングを行う
- dispatch budget を確認して K8s Job を作成する
- 成功・失敗時に DB 状態を更新する
- 起動時の DISPATCHING 状態リセット

Dispatcher のメインループは各スキャンサイクル完了時に `/tmp/liveness` ファイルをタッチする。Kubernetes の Liveness probe がこのファイルの最終更新時刻を確認し、ループ停止を検知して再起動できるようにする（[deployment.md](../deployment.md) §13.4 参照）。

```text
dispatch_budget = namespace_dispatch_limit - active_jobs_in_db(namespace)

namespace_dispatch_limit = 256（ConfigMap: DISPATCH_BUDGET_PER_NAMESPACE で設定）
batch_size              = 50 （ConfigMap: DISPATCH_BATCH_SIZE で設定）

active_jobs_in_db(namespace) は PostgreSQL から取得する。
K8s API は参照しない。

対象ステータス:
  - DISPATCHING（Dispatcher が処理中）
  - DISPATCHED（K8s Job 作成済み・Kueue 待ち）
  - RUNNING（Pod 実行中）
```

**DB ベースを採用する理由：**

- Dispatcher が budget 計算のたびに K8s API を叩くと、K8s API の障害が Dispatcher 全体に波及するリスクがある
- Dispatcher 自身が DISPATCHING に更新してから Job を作るため、自分が投入したジョブは必ず DB に反映される
- Watcher の同期遅延により DB の状態が実態と数件ズレる場合があるが、研究計算の実行時間（数分〜数時間）に対して数秒〜10秒のズレは実用上無視できる
- ズレの方向は常に budget の過小評価（控えめに投入）であり、過大評価（投入しすぎ）にはならない

DB クエリは `idx_jobs_namespace_status` インデックスにより効率化される。

### 2.2 再試行ポリシー

失敗シナリオごとに対処を分ける。

| シナリオ | 対処 | 再試行間隔 | 上限 |
|---|---|---|---|
| K8s API 一時障害 | `retry_after` を設定して `QUEUED` に戻す | `DISPATCH_RETRY_INTERVAL_SEC` 秒後 | `DISPATCH_MAX_RETRIES` 回 |
| dispatch budget 不足 | 次回スキャンで再評価（自然に再試行） | `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` 秒ごと | なし（budget 回復まで） |
| バリデーションエラー | 即 FAILED | なし | なし |
| 永続的 K8s エラー | 即 FAILED | なし | なし |

#### K8s API 一時障害の処理

```python
# ※ 概念説明のための擬似コードである。

except TemporaryK8sError:
    # 現在の retry_count を取得して上限チェック（アトミック UPDATE の前に判断）
    # これにより FAILED 遷移が先行し、QUEUED を経由しなくなる
    current_count = db.get_retry_count(namespace, job_id)
    if current_count + 1 >= max_retries:
        # AND status='DISPATCHING' 条件により CANCELLED を上書きしない
        updated_rows = db.update_status(
            namespace, job_id, "FAILED",
            error="max retries exceeded", condition_status="DISPATCHING"
        )
        # updated_rows == 0 は cancel API が CANCELLED に更新済みのためスキップ
        return
    # 上限内なら retry_count・retry_after・status をアトミックに更新する（§1.3 参照）
    # AND status='DISPATCHING' 条件により CANCELLED を上書きしない
    updated_rows = db.increment_retry_and_set_queued(
        namespace, job_id,
        retry_after=now + int(os.environ["DISPATCH_RETRY_INTERVAL_SEC"])
    )
    if updated_rows == 0:
        return   # cancel API が CANCELLED に更新済み → スキップ
    db.record_event(namespace, job_id, "RETRY", {"count": current_count + 1})
```

`retry_after <= NOW()` になった時点で次回スキャン時に自動的に再 dispatch 対象となる。

### 2.3 dispatch ループ

```python
# ※ 概念説明のための擬似コードである。

class Dispatcher:
    def __init__(self):
        self.check_interval = int(os.environ["DISPATCH_BUDGET_CHECK_INTERVAL_SEC"])
        self.batch_size = int(os.environ["DISPATCH_BATCH_SIZE"])

    def run(self):
        while True:
            # §1.2 のクエリ（期間リセット → ラウンドロビン・DRF 優先・LIMIT batch_size）
            candidates = db.fetch_dispatchable_jobs()
            # §2.4 の隙間充填フィルタ（滞留ジョブがある namespace の候補を制限）
            candidates = apply_gap_filling(session, candidates, settings)
            for job in candidates:
                self.dispatch(job)
            time.sleep(self.check_interval)

    def dispatch(self, job):
        # WHERE status='QUEUED' 条件付き UPDATE で CAS（Compare And Swap）
        # スキャン後・UPDATE 前に cancel API が CANCELLED に更新していた場合、
        # WHERE status='QUEUED' にマッチしないため updated_rows=0 となりスキップできる
        updated_rows = db.execute("""
            UPDATE jobs SET status = 'DISPATCHING'
            WHERE namespace = :namespace
              AND job_id    = :job_id
              AND status    = 'QUEUED'
        """, namespace=job.namespace, job_id=job.job_id)

        if updated_rows == 0:
            # cancel API が先に CANCELLED に更新していた → スキップ
            return

        # CAS をコミットしてから K8s Job を作成する。
        # これにより、create_job() が例外を投げた際の rollback で
        # DISPATCHING が巻き戻らず、後続の mark_failed / increment_retry の
        # WHERE status='DISPATCHING' 条件が正しくマッチする。
        db.commit()

        # DISPATCHING への更新が確定したので続行
        try:
            k8s.create_job(job)  # job.time_limit_seconds を activeDeadlineSeconds に設定
            # AND status='DISPATCHING' 条件により CANCELLED を上書きしない
            # updated_rows == 0 の場合は status が CANCELLED のまま維持され、
            # Watcher が次のサイクルで CANCELLED ジョブの K8s Job を削除する（watcher.md §3 Step 5）
            db.update_status(
                job.namespace, job.job_id, "DISPATCHED", condition_status="DISPATCHING"
            )
        except TemporaryK8sError:
            # §2.2 の再試行処理
            ...
        except PermanentK8sError:
            # AND status='DISPATCHING' 条件により CANCELLED を上書きしない
            # updated_rows == 0 は cancel API が CANCELLED に更新済みのためスキップ
            db.update_status(
                job.namespace, job.job_id, "FAILED", condition_status="DISPATCHING"
            )
        except Exception:
            # build_k8s_job() の ValueError やネットワーク例外等の未捕捉例外でも
            # ジョブを FAILED に遷移させ、DISPATCHING のまま滞留することを防ぐ
            db.update_status(
                job.namespace, job.job_id, "FAILED", condition_status="DISPATCHING"
            )
```

### 2.4 隙間充填（Gap Filling）

#### 2.4.1 背景と目的

Kueue の `BestEffortFIFO` は、先頭のジョブが admit できない場合に後続の小さなジョブを先に admit する。`preemption: Never` の制約下では、小さなジョブが継続的に投入される環境でノード丸ごと使うような巨大ジョブが starvation される可能性がある。

この問題に対し、Dispatcher が time_limit を活用して時間方向の隙間充填を行う。空間方向のパッキング（ノード配置）は Kueue + K8s Scheduler に任せ、Dispatcher は「大きなジョブのためにリソースが空くまで、隙間に収まるジョブだけを dispatch する」制御に専念する。

#### 2.4.2 滞留ジョブの検知

DISPATCHED 状態のまま `GAP_FILLING_STALL_THRESHOLD_SEC`（デフォルト 300 秒 = 5 分）以上経過したジョブを「滞留ジョブ」とみなす。

```sql
SELECT namespace, job_id
FROM jobs
WHERE status = 'DISPATCHED'
  AND dispatched_at <= NOW() - MAKE_INTERVAL(secs => :threshold)
```

滞留ジョブは「Kueue に渡されたがリソース不足で admit されていないジョブ」を意味する。通常のジョブは DISPATCHED から数秒〜数十秒で RUNNING に遷移するため、閾値を超えた場合はリソース不足で待機していると判断できる。

閾値が短すぎると Kueue の通常処理中のジョブも滞留扱いになる。閾値が長すぎると対策の発動が遅れる。5 分はクラスタの通常動作を考慮した保守的な値である。

**前提:** 本検知はノードのリソース不足（Kueue の ClusterQueue レベル）による滞留を想定している。namespace の ResourceQuota 枠不足による滞留は検知対象外である。ResourceQuota は dispatch_budget および ClusterQueue nominalQuota より緩く設定する運用を前提としており、通常は ResourceQuota より先にこれらの制限が効く（[resources.md](../architecture/resources.md) §1 参照）。

#### 2.4.3 リソース空き推定

滞留ジョブが検知された場合、同一 namespace の RUNNING ジョブから「リソースが空くまでの推定残り時間」を計算する。

```sql
SELECT MIN(
  EXTRACT(EPOCH FROM
    (started_at + MAKE_INTERVAL(secs => time_limit_seconds)) - NOW()
  )
) AS min_remaining
FROM jobs
WHERE namespace = :namespace
  AND status = 'RUNNING'
  AND started_at IS NOT NULL
```

全 RUNNING ジョブの最遅終了時刻（started_at + time_limit_seconds）から現在時刻を引き、最小値を T とする。T が負の場合は 0 にクランプする。RUNNING ジョブが存在しない場合は NULL（None）を返す。

T は「少なくともあと T 秒後には、いずれかの RUNNING ジョブが終了する」ことを意味する。実際にはジョブが time_limit より早く完了する場合が多いため、T は保守的な（長めの）推定となる。

#### 2.4.4 隙間充填ロジック

滞留ジョブが存在する namespace について、QUEUED ジョブの dispatch 対象を制限する。

```
dispatch サイクル:
  1. 通常の fetch_dispatchable_jobs() で候補を取得する
  2. 各 namespace について滞留ジョブの有無を確認する
  3. 滞留ジョブが存在しない namespace → 候補をそのまま dispatch（現行動作）
  4. 滞留ジョブが存在する namespace → 候補をフィルタリング:
     a. RUNNING ジョブの最短残り時間 T を計算する
     b. T = None（RUNNING ジョブなし）→ 全候補を制限なしで dispatch（デッドロック防止）
     c. T が値を持つ場合 → 候補のうち time_limit_seconds ≤ T のジョブだけを dispatch 対象とする
     d. time_limit_seconds > T のジョブは dispatch を保留する（次サイクルで再評価）
```

**RUNNING ジョブが存在しない場合**（全て DISPATCHED で待機中）: T = None となり、全候補を制限なしで dispatch する。これにより、滞留ジョブのみが DISPATCHED にある状態で namespace 全体の dispatch が永久に停止するデッドロックを防止する。新しくdispatch されたジョブが RUNNING に遷移すれば、次のサイクルで T が計算可能になり、通常の隙間充填制御に戻る。

**設計判断: namespace 内スコープに限定する理由**

滞留ジョブの影響は同一 namespace 内のみに適用し、他 namespace の dispatch は制限しない。理由は以下の通り。

- 他ユーザーの dispatch を制限すると、巨大ジョブを投入したユーザーが他ユーザーの実行を妨げることになり、公平性に反する
- Kueue の ClusterQueue レベルのリソース管理は Kueue 自身に委ねる
- namespace 内であれば、同一ユーザーのジョブ同士の調整であり、妥当な制御範囲である

**設計判断: 既存の fetch_dispatchable_jobs を変更せず、後段でフィルタリングする理由**

`fetch_dispatchable_jobs` の SQL クエリを直接変更する方式は、隙間充填ロジックを SQL に組み込む必要があり複雑になる。代わりに、取得した候補リストを Python 側でフィルタリングする方式を採用する。これにより：

- 既存のラウンドロビン・budget 制御ロジックに影響しない
- 隙間充填のオン・オフが設定値で制御可能
- テストが容易（フィルタリング関数を独立してテストできる）

#### 2.4.5 設定値

| 設定 | ConfigMap キー | デフォルト値 | 説明 |
|---|---|---|---|
| 滞留検知閾値 | `GAP_FILLING_STALL_THRESHOLD_SEC` | 300 (5分) | DISPATCHED から経過した秒数がこの値を超えたジョブを滞留とみなす |
| 隙間充填の有効/無効 | `GAP_FILLING_ENABLED` | true | false にすると隙間充填ロジックをスキップする（従来動作） |

#### 2.4.6 擬似コード

```python
# ※ 概念説明のための擬似コードである。

def apply_gap_filling(
    session: Session,
    candidates: list[Job],
    settings: Settings,
) -> list[Job]:
    """滞留ジョブが存在する namespace の候補をフィルタリングする。"""
    if not settings.GAP_FILLING_ENABLED:
        return candidates

    # 滞留ジョブを namespace ごとに取得
    stalled = fetch_stalled_jobs(session, settings.GAP_FILLING_STALL_THRESHOLD_SEC)
    stalled_namespaces = {job.namespace for job in stalled}

    if not stalled_namespaces:
        return candidates

    # 滞留が発生していない namespace の候補はそのまま通す
    result = [c for c in candidates if c.namespace not in stalled_namespaces]

    # 滞留が発生している namespace の候補はフィルタリング
    for ns in stalled_namespaces:
        ns_candidates = [c for c in candidates if c.namespace == ns]
        if not ns_candidates:
            continue

        # RUNNING ジョブの最短残り時間を計算
        remaining = estimate_shortest_remaining(session, ns)

        # RUNNING ジョブがない場合は全候補を通す（デッドロック防止）
        if remaining is None:
            result.extend(ns_candidates)
            continue

        # time_limit_seconds が残り時間以内のジョブだけを通す
        for c in ns_candidates:
            if c.time_limit_seconds <= remaining:
                result.append(c)
            else:
                logger.debug(
                    "Gap filling: holding %s/%d (time_limit=%ds, remaining=%s)",
                    ns, c.job_id, c.time_limit_seconds, remaining,
                )

    return result
```

#### 2.4.7 制約と限界

- **推定精度**: DB ベースの推定であり、Kueue/K8s Scheduler が把握する実際のノード空き状況とは乖離する。ジョブが time_limit より早く完了した場合、推定より早くリソースが空くが、Dispatcher は次のサイクルで再評価する
- **空間方向のパッキングは行わない**: Dispatcher は CPU/メモリの合計値を追跡しない。「滞留ジョブがいるので隙間だけ埋める」という時間方向の制御のみを行い、空間方向は Kueue に委ねる
- **time_limit_seconds が実行時間と大きく乖離する場合**: ユーザーが time_limit を実際の実行時間より大幅に長く設定すると、T の推定が保守的になりすぎて隙間充填の効果が薄れる。ただしこれは制御が保守的な方向（dispatch を控える）にずれるだけで、starvation を悪化させることはない

### 2.5 起動時の初期化処理

Dispatcher 再起動時に `DISPATCHING` で止まっているジョブを `QUEUED` に戻す。

```python
def on_startup():
    db.reset_stale_dispatching_jobs()
    # UPDATE jobs SET status = 'QUEUED', retry_after = NULL WHERE status = 'DISPATCHING'
```

## 3. sweep ジョブの dispatch

### 3.1 dispatch_budget の消費単位

sweep 1 件 = budget 1 として消費する。`parallelism` の値に関わらず、DB 上の `jobs` テーブルの 1 行が budget 1 に対応する。

### 3.2 K8s Indexed Job の構築

sweep ジョブ（`jobs.completions IS NOT NULL`）の場合、`build_k8s_job` は以下のフィールドを追加した K8s Job マニフェストを生成する。

```yaml
spec:
  completionMode: Indexed
  completions: <completions>
  parallelism: <parallelism>
  backoffLimitPerIndex: 0
  activeDeadlineSeconds: <time_limit_seconds>
```

`backoffLimitPerIndex: 0` により、1 回失敗したタスクは再試行せず即座に `failedIndexes` に追加される。

### 3.3 コマンドラッパー

sweep ジョブのコマンドラッパーは `CJOB_INDEX` の export とインデックス付きログディレクトリを使用する。

```bash
export CJOB_INDEX=$JOB_COMPLETION_INDEX
LOG_DIR=/home/jovyan/.cjob/logs/{job_id}/$CJOB_INDEX
mkdir -p "$LOG_DIR"
exec > >(tee "$LOG_DIR/stdout.log") 2> >(tee "$LOG_DIR/stderr.log" >&2)
{user_command}
EXIT_CODE=$?
exec >&- 2>&-
wait
exit $EXIT_CODE
```

通常ジョブのラッパーとの違いは `export CJOB_INDEX=$JOB_COMPLETION_INDEX` 行の追加と `LOG_DIR` にインデックスが含まれる点のみ。

### 3.3.1 `_INDEX_` プレースホルダー

コマンドラッパー構築時に、ユーザーコマンド中の `_INDEX_` を `$CJOB_INDEX` に置換する。これにより、ユーザーは `cjob sweep -- python main.py --trial _INDEX_` のようにシェル変数を意識せずにインデックスを参照できる。スクリプトファイル内では `$CJOB_INDEX` 環境変数を直接参照することもできる（ファイル内容はユーザーのシェルによる展開を受けないため）。

### 3.4 隙間充填との関係

隙間充填ロジックは既存のまま動作する。sweep ジョブの `time_limit_seconds` はsweep 全体の実行時間上限であり、隙間充填の推定に使用される。

## 4. ResourceFlavor に基づくジョブスケジューリング

ジョブの flavor に基づくノード振り分けは、通常ジョブ・sweep ジョブ共通で以下の流れで行われる。

1. ユーザーが `--flavor` で flavor を指定する（省略時は `DEFAULT_FLAVOR`）
2. Submit API が `jobs.flavor` に記録する
3. Dispatcher が `build_k8s_job` で K8s Job を作成し、Kueue の LocalQueue に投入する
4. Kueue が ClusterQueue 内の flavor リストからジョブのリソース要求を満たせる flavor を選択し、その `nodeLabels` に基づいてノードにスケジュールする

Dispatcher 側で `nodeSelector` や追加の `tolerations` を設定する必要はない。ノードの振り分けは Kueue の ResourceFlavor が担う。

### 4.1 GPU リソースの設定

`job.gpu > 0` の場合、`build_k8s_job` は `RESOURCE_FLAVORS` 設定から `job.flavor` に一致する flavor 定義を検索し、その `gpu_resource_name`（例: `nvidia.com/gpu`、`amd.com/gpu`）をコンテナの `resources.requests` と `resources.limits` に追加する。`job.gpu == 0` の場合は CPU / メモリのみを設定する。

ResourceFlavor の定義と設定値については [resources.md](resources.md) §ResourceFlavor、[kueue.md](kueue.md) §1 を参照。

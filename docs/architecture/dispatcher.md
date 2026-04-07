# Dispatcher 設計

## 1. スケジューリング設計

### 1.1 スケジューリング方針

Dispatcher は PostgreSQL を定期的にスキャンし、以下の基準で dispatch するジョブを選択する。

1. **budget に余裕のある (namespace, flavor) のみ対象とする**（同一 (namespace, flavor) の DISPATCHING + DISPATCHED + RUNNING < dispatch_limit）
2. **対象 namespace の QUEUED ジョブを `created_at` 昇順で取得する**
3. **1サイクルあたりの取得数を `DISPATCH_BATCH_SIZE`（デフォルト 50）で固定する**
4. **namespace 間を公平にラウンドロビンする**（各 namespace から `DISPATCH_ROUND_SIZE` 件ずつ交互に取得）
5. **累計リソース消費量の dominant share が小さい namespace を優先する**（DRF による公平性）

この方式により：
- budget を使い切ったユーザーのジョブが他ユーザーをブロックしない
- 同一ユーザーの投入順（`created_at` 昇順）は常に保証される
- 複数ユーザーが同時に QUEUED 状態でも公平に処理される
- `DISPATCH_ROUND_SIZE` の設定により、ラウンドロビンによる均等配分から DRF による消費量ベースの優先制御まで調整できる（§1.2 調整指針参照）
- 1サイクルあたりの dispatch 数が固定されるため K8s API への負荷が予測可能になる

**Fair sharing（DRF）：** namespace ごとの直近 `FAIR_SHARE_WINDOW_DAYS` 日分のリソース消費量（[database.md](database.md) §5 の `namespace_daily_usage` テーブル）を参照し、Dominant Resource Fairness（DRF）に基づいて dispatch 優先度を決定する。各リソース（CPU・メモリ・GPU）をクラスタ全体の容量で正規化し、最大値（dominant share）を namespace の weight（[database.md](database.md) §4）で割った値が小さい namespace を優先的に dispatch する。これにより、リソースを多く消費した namespace の優先度が下がり、消費の少ない namespace にリソースが行き渡る。weight が大きい namespace はより多くのリソースを消費するまで優先され続ける。日別の消費量をスライディングウィンドウで集計するため、一括リセットの断崖が生じない。

**Flavor DRF weight：** `flavor_quotas` テーブルの `drf_weight`（[database.md](database.md) §7）を DRF の消費量と容量の両方に乗じることで、flavor ごとのリソースの「価値」の違いを反映する。GPU など貴重なリソースに大きい weight（例: 2.0）、低スペック flavor に小さい weight（例: 0.5）を設定することで、低スペック flavor の使用が DRF スコアを不当に押し上げることを防ぐ。デフォルトは 1.0（全 flavor 均一）。`cjobctl cluster set-drf-weight` で設定する。

DRF 正規化に使用するクラスタ全体の容量は、flavor ごとに `node_resources` テーブル（[database.md](database.md) §6）の allocatable 合計と `flavor_quotas` テーブル（[database.md](database.md) §7）の nominalQuota の小さい方を取り、`drf_weight` を乗じてから全 flavor で合算して算出する。これにより、nominalQuota が allocatable より小さい場合に、実際に使用可能なリソース量に対する正確なシェアが計算される。Watcher がノードの `allocatable` と ClusterQueue の nominalQuota を定期的に同期するため、ノードの追加・撤去や quota 変更が自動的に反映される。`flavor_quotas` テーブルが空の場合（Watcher 未同期時）は `node_resources` の allocatable 合計をそのまま使用し、`node_resources` テーブルも空の場合は DRF ソートを無効化して namespace 名順にフォールバックする。

**消費量データの保持：** `namespace_daily_usage` の古い行の削除は `USAGE_RETENTION_DAYS`（デフォルト 7）で制御する。DRF 計算ウィンドウ（`FAIR_SHARE_WINDOW_DAYS`）とは独立しており、将来 DRF 以外の用途で消費量データを参照する場合に、より長い期間のデータを保持できる。

### 1.2 DB スキャンのクエリ方針

```sql
-- active CTE: (namespace, flavor) ごとの active ジョブ数を集計（budget 制御用）
WITH active AS (
  SELECT namespace, flavor, COUNT(*) AS active_count
  FROM jobs
  WHERE status IN ('DISPATCHING', 'DISPATCHED', 'RUNNING')
  GROUP BY namespace, flavor
),
-- queued CTE: QUEUED ジョブに namespace 内の投入順（rn）と (namespace, flavor) 内の投入順（flavor_rn）を付与
queued AS (
  SELECT *,
    ROW_NUMBER() OVER (PARTITION BY namespace ORDER BY created_at ASC) AS rn,
    ROW_NUMBER() OVER (PARTITION BY namespace, flavor ORDER BY created_at ASC) AS flavor_rn
  FROM jobs
  WHERE status = 'QUEUED'            -- HELD ジョブはディスパッチ対象外
    AND (retry_after IS NULL OR retry_after <= NOW())
),
-- in_flight CTE: DISPATCHING/DISPATCHED ジョブの予測消費量を集計
in_flight AS (
  SELECT namespace,
    SUM(time_limit_seconds * cpu_millicores
        * CASE WHEN completions IS NOT NULL THEN parallelism ELSE 1 END) AS cpu_millicores_seconds,
    SUM(time_limit_seconds * memory_mib
        * CASE WHEN completions IS NOT NULL THEN parallelism ELSE 1 END) AS memory_mib_seconds,
    SUM(time_limit_seconds * gpu
        * CASE WHEN completions IS NOT NULL THEN parallelism ELSE 1 END) AS gpu_seconds
  FROM jobs
  WHERE status IN ('DISPATCHING', 'DISPATCHED')
  GROUP BY namespace
)
SELECT q.* FROM queued q
  LEFT JOIN active a ON q.namespace = a.namespace AND q.flavor = a.flavor
  LEFT JOIN (                              -- 直近 N 日のウィンドウ集計
    SELECT namespace,
           SUM(cpu_millicores_seconds) AS cpu_millicores_seconds,
           SUM(memory_mib_seconds) AS memory_mib_seconds,
           SUM(gpu_seconds) AS gpu_seconds
    FROM namespace_daily_usage
    WHERE usage_date > CURRENT_DATE - :window_days
    GROUP BY namespace
  ) u ON q.namespace = u.namespace
  LEFT JOIN in_flight inf ON q.namespace = inf.namespace
  LEFT JOIN namespace_weights w ON q.namespace = w.namespace
WHERE COALESCE(a.active_count, 0) < :dispatch_limit              -- budget に余裕がある (namespace, flavor) のみ
  AND q.flavor_rn <= :dispatch_limit - COALESCE(a.active_count, 0)  -- 残り budget 分だけ取得（flavor 単位）
  AND COALESCE(w.weight, 1) > 0                                   -- weight=0 の namespace は dispatch 対象外
ORDER BY CEIL(q.rn * 1.0 / :round_size) ASC,  -- ラウンドロビン（各 namespace から round_size 件ずつ交互）
         GREATEST(                         -- DRF: dominant share / weight が小さい namespace を優先
           (COALESCE(u.cpu_millicores_seconds, 0) + COALESCE(inf.cpu_millicores_seconds, 0))::float / :cluster_cpu_millicores,
           (COALESCE(u.memory_mib_seconds, 0) + COALESCE(inf.memory_mib_seconds, 0))::float / :cluster_memory_mib,
           (COALESCE(u.gpu_seconds, 0) + COALESCE(inf.gpu_seconds, 0))::float / NULLIF(:cluster_gpus, 0)
         ) / COALESCE(w.weight, 1) ASC NULLS FIRST,  -- 消費量レコードなし(NULL)の namespace が最優先
         q.namespace ASC                   -- 同率の場合は namespace 名で決定的に順序付け
LIMIT :fetch_limit;                        -- 候補を余剰取得（DISPATCH_BATCH_SIZE × DISPATCH_FETCH_MULTIPLIER）
```

このクエリは `idx_jobs_namespace_status` インデックスにより効率化される。`namespace_daily_usage` のウィンドウ集計はサブクエリで行い、namespace ごとに 1 行に集約されるため JOIN のコストは無視できる（20 namespace × 7 日 = 140 行程度）。`in_flight` CTE も `idx_jobs_namespace_status` インデックスを利用し、DISPATCHING/DISPATCHED ジョブ（通常 `DISPATCH_BUDGET_PER_NAMESPACE × namespace 数 × flavor 数` 以下）を効率的に集計する。

**budget の flavor 分離：** `active` CTE は `(namespace, flavor)` 単位で active ジョブ数を集計し、`queued` CTE には namespace 単位の `rn` に加えて `(namespace, flavor)` 単位の `flavor_rn` を付与する。`active` との JOIN は `(namespace, flavor)` でマッチし、WHERE 句の budget 条件には `flavor_rn` を使用する。これにより、ある flavor の active ジョブが budget を占有しても、同じ namespace の別 flavor のジョブは独立した budget で dispatch される。ラウンドロビン用の `rn` は namespace 単位のままであり、flavor 数が多い namespace がラウンドロビン枠を多く占有する問題は発生しない。

**ラウンドロビンの仕組み：** `ROW_NUMBER()` を QUEUED ジョブのみに振り、`CEIL(rn / round_size)` でグループ化することで、各 namespace から `DISPATCH_ROUND_SIZE` 件ずつ交互に取得する。`rn` は namespace 単位の連番であり、flavor ごとの分離は budget 条件（`flavor_rn`）のみで行う。デフォルト（`round_size = 1`）では各 namespace から 1 件ずつ交互に並び、`round_size = 5` なら 5 件ずつまとめて並ぶ。1 サイクルあたりの dispatch 数は後段のフィルタ通過後に `DISPATCH_BATCH_SIZE` 件へ絞り込まれる（後述の「候補の余剰取得」参照）。

**候補の余剰取得：** SQL の `LIMIT` は `DISPATCH_BATCH_SIZE × DISPATCH_FETCH_MULTIPLIER`（デフォルト 50 × 10 = 500）で取得する。取得した候補は §2.4 の隙間充填フィルタと §2.5 の ResourceQuota プレチェックを通過した後、Python 側で先頭 `DISPATCH_BATCH_SIZE` 件に絞り込んで dispatch する。余剰取得は、DRF 優先で取得される namespace のジョブが全滅（現在の残リソースで実行不可能）した場合に、後続の他 namespace の候補が dispatch されるようにするためである。これにより、リソースに空きがあるにもかかわらず 0 dispatch が継続して均衡が進まない事象を防ぐ。倍率は namespace 数・ジョブサイズの分布に応じて調整できる。候補数は WHERE 句の `q.flavor_rn <= :dispatch_limit - active_count` により `namespace数 × flavor数 × DISPATCH_BUDGET_PER_NAMESPACE` が上限となるため無制限にはならず、DB → Dispatcher のネットワーク転送量と Python 側のフィルタ iteration 回数のみが増加する。

**公平性の保証（DRF）：** 直近 `FAIR_SHARE_WINDOW_DAYS` 日分のリソース消費量（`namespace_daily_usage`）と DISPATCHING/DISPATCHED ジョブの予測消費量（`in_flight` CTE）を合算し、クラスタ容量で正規化し、`GREATEST` で最大値（dominant share）を求め、namespace の weight（`namespace_weights` テーブル、デフォルト 1）で割ってソートする。消費量と容量の両方に `flavor_quotas.drf_weight` を乗じることで、flavor ごとのリソースの「価値」の違いを反映する。クラスタ容量は flavor ごとに `node_resources` テーブルの allocatable 合計と `flavor_quotas` テーブルの nominalQuota の小さい方を取り、`drf_weight` を乗じてから全 flavor で合算して算出する（[database.md](database.md) §6.2・§7.2 参照）。これにより、支配的リソースの消費割合が小さい namespace が優先され、weight が大きい namespace はより多くのリソースを消費するまで優先され続ける。`namespace_daily_usage` にレコードがない namespace は消費量 0 として扱われ（`COALESCE` + `NULLS FIRST`）、最優先で dispatch される。GPU が 0 のクラスタでは `NULLIF(:cluster_gpus, 0)` により GPU の項が NULL となり、DRF の計算から除外される。`node_resources` テーブルが空の場合は DRF ソートを無効化し、namespace 名順にフォールバックする。

**in-flight CTE による予測消費量の反映：** DISPATCHING/DISPATCHED ジョブは `namespace_daily_usage` に未記録（Watcher が RUNNING 遷移時に記録するため）であるが、in_flight CTE により `time_limit_seconds × リソース量 × drf_weight` の予測消費量が DRF スコアに加算される。RUNNING に遷移したジョブは `namespace_daily_usage` に記録済みであり、かつ `status IN ('DISPATCHING', 'DISPATCHED')` の条件から除外されるため、二重計上は発生しない。PostgreSQL の MVCC（スナップショット分離）により、同一トランザクション内でのステータス遷移の一貫性が保証される。in_flight CTE は `jobs.cpu_millicores` / `jobs.memory_mib` カラム（Submit API が `parse_cpu_millicores()` / `parse_memory_mib()` で設定する数値カラム）を使用し、`flavor_quotas.drf_weight` を `jobs.flavor` で JOIN して乗じる（[database.md](database.md) §1 参照）。

**古い行の削除：** `fetch_dispatchable_jobs()` の実行前に、保持期間（`USAGE_RETENTION_DAYS`）外の古い行を削除する（[database.md](database.md) §5.4 参照）。

**`DISPATCH_ROUND_SIZE` の調整指針：** `DISPATCH_ROUND_SIZE` はラウンドロビン（primary sort）と DRF（secondary sort）のバランスを制御する。クエリの `ORDER BY` は以下の優先度で並べ替える。

1. `CEIL(rn / round_size)` — ラウンドロビングループ
2. DRF dominant share / weight — グループ内の namespace 優先度
3. namespace 名 — 同率タイブレイク

DRF が dispatch 結果を実質的に変えるのは、1 つのラウンドロビングループ内のジョブ数が `DISPATCH_BATCH_SIZE` を超え、`DISPATCH_BATCH_SIZE` への切り詰めが発生するときである。`round_size` が小さいと各グループの件数が少なくなり、DRF は順序の調整にとどまる。`round_size` が大きいと各グループの件数が多くなり、DRF が dispatch 枠の配分自体を左右する。

| 設定 | 挙動 | 特性 |
|---|---|---|
| `round_size = 1`（デフォルト） | 各 namespace から 1 件ずつ交互に取得。DRF はグループ内の順序のみ決定 | namespace 数が `DISPATCH_BATCH_SIZE` 以下の場合、全 namespace が均等に dispatch される。DRF の影響は namespace 数が `DISPATCH_BATCH_SIZE` を超えた場合にのみ現れる |
| `round_size = DISPATCH_BUDGET_PER_NAMESPACE` | 各 namespace の budget 内の全ジョブが同一グループに入り、DRF が namespace 間の配分を完全に決定 | リソース消費量の少ない namespace が優先的に dispatch され、消費量の多い namespace の dispatch が抑制される。dispatch が進むたびに in_flight CTE が更新され、特定 namespace への集中は数サイクル（数十秒）で均衡に戻る。DRF 優先の namespace のジョブがフィルタで全滅しても、候補の余剰取得により他 namespace の dispatch が継続するため、0 dispatch による均衡停止は発生しない |

中間値は `DISPATCH_BATCH_SIZE mod (namespace 数 × round_size)` の剰余に依存して DRF の影響度が変動し、namespace 数の増減で挙動が不安定になるため推奨しない。DRF による消費量ベースの優先制御を意図する場合は `round_size = DISPATCH_BUDGET_PER_NAMESPACE` を設定する。DRF を使用せずラウンドロビンのみで運用する場合はデフォルトの `round_size = 1` を維持する。

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
dispatch_budget(namespace, flavor) = dispatch_limit - active_jobs_in_db(namespace, flavor)

dispatch_limit   = 32 （ConfigMap: DISPATCH_BUDGET_PER_NAMESPACE で設定、flavor ごとに適用）
batch_size       = 50 （ConfigMap: DISPATCH_BATCH_SIZE で設定）
fetch_multiplier = 10 （ConfigMap: DISPATCH_FETCH_MULTIPLIER で設定）

active_jobs_in_db(namespace, flavor) は PostgreSQL から (namespace, flavor) 単位で取得する。
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
        self.fetch_multiplier = int(os.environ["DISPATCH_FETCH_MULTIPLIER"])

    def run(self):
        while True:
            # §1.2 のクエリ（期間リセット → ラウンドロビン・DRF 優先・LIMIT fetch_limit で余剰取得）
            candidates = db.fetch_dispatchable_jobs()
            # §2.4 の隙間充填フィルタ（滞留ジョブがある namespace の候補を制限）
            candidates = apply_gap_filling(session, candidates, settings)
            # §2.5 の ResourceQuota プレチェック（namespace の残リソースで候補を制限）
            candidates = filter_by_resource_quota(session, candidates)
            # フィルタ通過後の先頭 DISPATCH_BATCH_SIZE 件に絞り込む（1サイクルの dispatch 数上限）
            candidates = candidates[:self.batch_size]
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

**滞留のスコープ:** 滞留ジョブの影響は `(namespace, flavor)` 単位に限定する。GPU flavor のジョブが滞留しても、同一 namespace の CPU flavor のジョブの dispatch は制限しない。flavor ごとにリソースプールが独立しているため、ある flavor のリソース不足が他の flavor の dispatch を阻害する理由はない。

**前提:** 本検知はノードのリソース不足（Kueue の ClusterQueue レベル）による滞留を想定している。namespace の ResourceQuota 枠不足による DISPATCHED 滞留は §2.5 の ResourceQuota プレチェックで防止される。ResourceQuota は dispatch_budget および ClusterQueue nominalQuota より緩く設定する運用を前提としており、通常は ResourceQuota より先にこれらの制限が効く（[resources.md](../architecture/resources.md) §1 参照）。

#### 2.4.3 リソース空き推定

滞留ジョブが検知された場合、時間とリソースの2軸で空き状況を推定する。

##### 時間方向の推定

同一 `(namespace, flavor)` の RUNNING ジョブから「リソースが空くまでの推定残り時間」を計算する。

```sql
SELECT MIN(
  EXTRACT(EPOCH FROM
    (started_at + MAKE_INTERVAL(secs => time_limit_seconds)) - NOW()
  )
) AS min_remaining
FROM jobs
WHERE namespace = :namespace
  AND flavor = :flavor
  AND status = 'RUNNING'
  AND started_at IS NOT NULL
```

同一 `(namespace, flavor)` の全 RUNNING ジョブの最遅終了時刻（started_at + time_limit_seconds）から現在時刻を引き、最小値を T とする。T が負の場合は 0 にクランプする。RUNNING ジョブが存在しない場合は NULL（None）を返す。

T は「少なくともあと T 秒後には、同一 flavor のいずれかの RUNNING ジョブが終了する」ことを意味する。実際にはジョブが time_limit より早く完了する場合が多いため、T は保守的な（長めの）推定となる。

flavor 単位でスコープを限定する理由: CPU ジョブの終了は GPU リソースを解放しないため、異なる flavor の RUNNING ジョブの残り時間を参照すると不合理な時間推定になる。例えば GPU flavor が滞留し RUNNING が CPU ジョブのみ（残り 5 分）の場合、namespace 単位の推定では T = 5 分となり CPU の残り時間で GPU 候補をフィルタしてしまう。flavor 単位の推定では T = None となり時間条件が免除され、リソース条件のみが適用される。

##### リソース方向の推定

ClusterQueue の利用可能リソースを flavor ごとに推定する。

```
available[flavor] = flavor_quotas[flavor] - SUM(RUNNING jobs の resource[flavor])
```

- `flavor_quotas` テーブル（[database.md](database.md) §7 参照）から ClusterQueue の nominalQuota を取得する
- クラスタ全体の RUNNING ジョブを flavor 別に集計し、消費中のリソースを算出する
- DISPATCHED ジョブは集計に含めない。滞留ジョブを含む DISPATCHED ジョブは Kueue に admit されていない可能性があり、ClusterQueue のリソースを消費していないため
- sweep ジョブの場合は `parallelism` 倍のリソースを消費しているものとして計算する
- `flavor_quotas` テーブルに行がない flavor は制限なしとして扱う

#### 2.4.4 隙間充填ロジック

滞留ジョブが存在する `(namespace, flavor)` について、同一 `(namespace, flavor)` の QUEUED ジョブの dispatch 対象を時間とリソースの両面で制限する。

```
dispatch サイクル:
  1. 通常の fetch_dispatchable_jobs() で候補を取得する
  2. 各 (namespace, flavor) について滞留ジョブの有無を確認する
  3. 滞留が発生していない (namespace, flavor) → 候補をそのまま dispatch
  4. ClusterQueue の利用可能リソースを flavor ごとに推定する
  5. 滞留ジョブが存在する (namespace, flavor) → 同一 (namespace, flavor) の候補をフィルタリング:
     a. 同一 (namespace, flavor) の RUNNING ジョブの最短残り時間 T を計算する
     b. T = None（同一 (namespace, flavor) の RUNNING ジョブなし）→ 時間条件を免除（デッドロック防止）
     c. T が値を持つ場合 → time_limit_seconds ≤ T のジョブのみ通過（時間条件）
     d. 時間条件を通過したジョブに対し、リソース条件を適用:
        - 候補の flavor に対応する利用可能リソースと比較する
        - CPU・メモリ・GPU の全てが利用可能リソース以内であれば通過する
        - sweep ジョブは parallelism 倍のリソースとして計算する
        - 通過したジョブのリソースを利用可能リソースから差し引く（累積追跡）
     e. 両条件を満たさないジョブは dispatch を保留する（次サイクルで再評価）
```

**RUNNING ジョブが存在しない場合**（同一 `(namespace, flavor)` の全ジョブが DISPATCHED で待機中）: T = None となり、時間条件を免除する。リソース条件は引き続き適用する。ClusterQueue に空きがなければ dispatch しても Kueue に admit されないため、リソース条件の維持はデッドロックを悪化させない。ClusterQueue に空きがある場合は dispatch が許可され、ジョブが RUNNING に遷移すれば次のサイクルで T が計算可能になり通常の制御に戻る。

**設計判断: (namespace, flavor) スコープに限定する理由**

滞留ジョブの影響は同一 `(namespace, flavor)` 内のみに適用し、他の namespace や同一 namespace の他 flavor の dispatch は制限しない。理由は以下の通り。

- 他ユーザーの dispatch を制限すると、巨大ジョブを投入したユーザーが他ユーザーの実行を妨げることになり、公平性に反する
- flavor ごとにリソースプール（ClusterQueue の ResourceFlavor）が独立しているため、ある flavor のリソース不足は他の flavor に影響しない
- Kueue の ClusterQueue レベルのリソース管理は Kueue 自身に委ねる
- `(namespace, flavor)` 内であれば、同一ユーザーの同一リソースプールのジョブ同士の調整であり、妥当な制御範囲である

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
    """滞留ジョブが存在する (namespace, flavor) の候補をフィルタリングする。"""
    if not settings.GAP_FILLING_ENABLED:
        return candidates

    # 滞留ジョブを (namespace, flavor) ごとに取得
    stalled = fetch_stalled_jobs(session, settings.GAP_FILLING_STALL_THRESHOLD_SEC)
    stalled_keys = {(job.namespace, job.flavor) for job in stalled}

    if not stalled_keys:
        return candidates

    # ClusterQueue の利用可能リソースを flavor ごとに推定
    available = estimate_available_cluster_resources(session, settings)

    # 滞留が発生していない (namespace, flavor) の候補はそのまま通す
    result = [c for c in candidates if (c.namespace, c.flavor) not in stalled_keys]

    # 滞留が発生している (namespace, flavor) の候補はフィルタリング
    for ns, flv in stalled_keys:
        key_candidates = [c for c in candidates if c.namespace == ns and c.flavor == flv]
        if not key_candidates:
            continue

        # 同一 (namespace, flavor) の RUNNING ジョブの最短残り時間を計算
        remaining = estimate_shortest_remaining(session, ns, flv)

        for c in key_candidates:
            # 時間条件: remaining=None（同一 (namespace, flavor) の RUNNING なし）の場合は免除（デッドロック防止）
            if remaining is not None and c.time_limit_seconds > remaining:
                logger.debug(
                    "Gap filling: holding %s/%d (time_limit=%ds > remaining=%ds)",
                    ns, c.job_id, c.time_limit_seconds, remaining,
                )
                continue

            # リソース条件: flavor の利用可能リソースに収まるか
            multiplier = c.parallelism if c.completions is not None else 1
            job_cpu = c.cpu_millicores * multiplier
            job_mem = c.memory_mib * multiplier
            job_gpu = c.gpu * multiplier

            flavor_avail = available.get(c.flavor)
            if flavor_avail is not None:
                if (job_cpu > flavor_avail["cpu"]
                        or job_mem > flavor_avail["mem"]
                        or job_gpu > flavor_avail["gpu"]):
                    logger.debug(
                        "Gap filling: holding %s/%d (resource exceeds available for flavor=%s)",
                        ns, c.job_id, c.flavor,
                    )
                    continue
                # 累積追跡: 通過したジョブのリソースを差し引く
                flavor_avail["cpu"] -= job_cpu
                flavor_avail["mem"] -= job_mem
                flavor_avail["gpu"] -= job_gpu

            result.append(c)

    return result
```

#### 2.4.7 制約と限界

- **時間推定の精度**: DB ベースの推定であり、Kueue/K8s Scheduler が把握する実際のノード空き状況とは乖離する。ジョブが time_limit より早く完了した場合、推定より早くリソースが空くが、Dispatcher は次のサイクルで再評価する。時間推定は同一 `(namespace, flavor)` の RUNNING ジョブのみを参照するため、異なる flavor のジョブ終了は考慮しない（異なる flavor のリソースプールは独立しているため合理的）
- **リソース推定の精度**: RUNNING ジョブのみを集計するため、最近 DISPATCHED されて Kueue に admit 済みだがまだ RUNNING に遷移していないジョブのリソース消費は反映されない。結果として利用可能リソースを若干過大評価する場合がある。DRF スコアでは in-flight CTE により DISPATCHING/DISPATCHED ジョブの予測消費量を加算しているが（§1.2 参照）、隙間充填のリソース推定は ClusterQueue の実消費（RUNNING のみ）に基づくため、同様の補正は行わない。Kueue が最終的な admission を判断するため実害はない
- **ノード配置は考慮しない**: リソース推定は flavor ごとの合計値で行い、個々のノードの空き状況は考慮しない。合計では収まるが特定ノードに空きがない場合、Kueue が admit しない可能性がある
- **time_limit_seconds が実行時間と大きく乖離する場合**: ユーザーが time_limit を実際の実行時間より大幅に長く設定すると、T の推定が保守的になりすぎて隙間充填の効果が薄れる。ただしこれは制御が保守的な方向（dispatch を控える）にずれるだけで、starvation を悪化させることはない

### 2.5 ResourceQuota プレチェック

Dispatcher は dispatch 候補に対して namespace の ResourceQuota 残リソースを確認し、不足しているジョブを候補から除外する（QUEUED に留める）。これにより、JupyterHub 等の User Pod が ResourceQuota を圧迫している場合に、ジョブが DISPATCHED のまま滞留して最終的に時間切れで FAILED になることを防ぐ。

```python
# ※ 概念説明のための擬似コードである。

candidates = fetch_dispatchable_jobs(session, settings)
candidates = apply_gap_filling(session, candidates, settings)
candidates = filter_by_resource_quota(session, candidates)  # 追加
```

`filter_by_resource_quota()` は `namespace_resource_quotas` テーブル（[database.md](database.md) §8 参照）から候補 namespace の ResourceQuota 情報を読み取り、以下のロジックで候補をフィルタする:

1. テーブルに行がない namespace のジョブは制限なしとして通過させる
2. DRF 優先順序のまま候補を iterate し、残リソース（hard - used）がジョブのリソース要求以上であれば通過させる
3. sweep ジョブの場合は `parallelism` 倍のリソースを要求するものとして計算する
4. 同一サイクル内で通過させたジョブのリソースを累計し、後続ジョブの残リソース計算に反映する（同一サイクルでの過剰 dispatch を防止）
5. `hard_count` が NULL でない場合、残りジョブ数（hard_count - used_count - サイクル内累計 dispatch 数）が 1 以上であることを確認する。sweep ジョブも K8s Job 1 つとしてカウントする（parallelism による倍算なし）

**前提:** ResourceQuota の使用状況は Watcher が定期同期するため、`RESOURCE_QUOTA_SYNC_INTERVAL_SEC`（デフォルト 10 秒）分の遅延がある。このチェックは best-effort であり、チェック通過後に Kueue が admit するまでの間に ResourceQuota の使用状況が変わる可能性がある。ただし、チェックなしの場合（DISPATCHED 滞留 → 時間切れ FAILED）と比較して大幅に改善される。

**budget の flavor 分離との関係:** budget を `(namespace, flavor)` 単位に分離したことで、namespace あたりの最大 active ジョブ数が理論上 `DISPATCH_BUDGET_PER_NAMESPACE × flavor 数` に増加する。`count/jobs.batch` のプレチェック（ステップ 5）により、flavor-aware budget の合計が `count/jobs.batch` を超過する場合でも、Dispatcher が K8s API エラーを受ける前に dispatch を抑制できる。CPU/memory/GPU の ResourceQuota が単一 budget を前提にサイジングされている場合は、相対的に窮屈になり ResourceQuota で弾かれるジョブが増える可能性がある。これは保守的な方向（dispatch を控える）であり、過剰 dispatch にはならない。

### 2.6 起動時の初期化処理

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

`backoffLimitPerIndex: 0` により、1 回失敗したタスクは再試行せず即座に `failedIndexes` に追加される。sweep ジョブでは失敗タスクの再試行が parallelism 枠を占有し、他のタスクの実行を妨げるため、即座に枠を解放する必要がある。

通常ジョブでは `backoffLimit: 0` を明示設定し、失敗時にリトライせず即座に FAILED とする。研究計算ではエラーとなるジョブはリトライしても同じ結果になるケースが大半であり、リトライによる Error Pod の増加（デフォルトでは計 7 個）のデメリットの方が大きい。

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

ユーザーコマンド中の `_INDEX_` から `$CJOB_INDEX` への置換は、CLI クライアント側で Submit API に送信する前に行われる（[cli.md](cli.md) §3 参照）。Dispatcher は置換済みのコマンド文字列を受け取り、そのままコマンドラッパーに組み込む。これにより、ユーザーは `cjob sweep -- python main.py --trial _INDEX_` のようにシェル変数を意識せずにインデックスを参照できる。スクリプトファイル内では `$CJOB_INDEX` 環境変数を直接参照することもできる（ファイル内容はユーザーのシェルによる展開を受けないため）。

### 3.4 隙間充填との関係

隙間充填ロジックは既存のまま動作する。sweep ジョブの `time_limit_seconds` はsweep 全体の実行時間上限であり、隙間充填の推定に使用される。

## 4. ResourceFlavor に基づくジョブスケジューリング

ジョブの flavor に基づくノード振り分けは、通常ジョブ・sweep ジョブ共通で以下の流れで行われる。

1. ユーザーが `--flavor` で flavor を指定する（省略時は `DEFAULT_FLAVOR`）
2. Submit API が `jobs.flavor` に記録する
3. Dispatcher が `jobs.flavor` を参照して K8s Job を作成し、Kueue の LocalQueue に投入する。GPU ジョブの場合は対応する `gpu_resource_name` をリソース要求に追加する（§4.1 参照）。全ジョブ共通で、flavor の `label_selector`（`RESOURCE_FLAVORS` 設定で定義）を K8s Job の `nodeSelector` として設定する
4. Kueue が ClusterQueue 内の flavor リストから、`nodeSelector` にマッチする `nodeLabels` を持つ flavor を選択し、ノードにスケジュールする

### 4.1 GPU リソースの設定

`job.gpu > 0` の場合、`build_k8s_job` は `RESOURCE_FLAVORS` 設定から `job.flavor` に一致する flavor 定義を検索し、その `gpu_resource_name`（例: `nvidia.com/gpu`、`amd.com/gpu`）をコンテナの `resources.requests` と `resources.limits` に追加する。`job.gpu == 0` の場合は CPU / メモリのみを設定する。

ResourceFlavor の定義と設定値については [resources.md](resources.md) §ResourceFlavor、[kueue.md](kueue.md) §1 を参照。

### 4.2 CPU limit バッファ

`CPU_LIMIT_BUFFER_MULTIPLIER`（デフォルト `1.0`）が `1.0` より大きい場合、`build_k8s_job` は CPU **limit のみ**に乗数を適用する。request は変更しない。

```yaml
# CPU_LIMIT_BUFFER_MULTIPLIER=1.05、--cpu 2 の場合
resources:
  requests:
    cpu: "2"       # 変更なし（Kueue クォータはこちらで計算）
  limits:
    cpu: "2100m"   # 2000m × 1.05 = 2100m
```

コンテナ内のシステムプロセス（PID 1、bash、ログ出力等）のわずかな CPU 消費により、ユーザープログラムが request 分のみ使用していても CFS throttling が発生する場合がある。limit にバッファを持たせることでこれを軽減する。

request を変更しないため、Kueue のクォータ消費・DRF スケジューリングへの影響はない。乗数 `1.0` のときは request == limit となり、従来の動作（Guaranteed QoS）と同一である。乗数が `1.0` を超える場合は QoS クラスが Burstable に変わるが、Kueue 管理のバッチジョブでは実質的な影響は小さい。

# Dispatcher 設計

## 1. スケジューリング設計

### 1.1 スケジューリング方針

Dispatcher は PostgreSQL を定期的にスキャンし、以下の基準で dispatch するジョブを選択する。

1. **budget に余裕のある namespace のみ対象とする**（DISPATCHING + DISPATCHED + RUNNING < dispatch_limit）
2. **対象 namespace の QUEUED ジョブを `created_at` 昇順で取得する**
3. **1サイクルあたりの取得数を `DISPATCH_BATCH_SIZE`（デフォルト 50）で固定する**
4. **namespace 間を公平にラウンドロビンする**（各 namespace から1件ずつ交互に取得）
5. **active ジョブ数が少ない namespace を優先する**（リソース消費の公平性）

この方式により：
- budget を使い切ったユーザーのジョブが他ユーザーをブロックしない
- 同一ユーザーの投入順（`created_at` 昇順）は常に保証される
- 複数ユーザーが同時に QUEUED 状態でも公平に処理される
- namespace 数が `DISPATCH_BATCH_SIZE` を超えても active ジョブ数による優先で公平性が維持される
- 1サイクルあたりの dispatch 数が固定されるため K8s API への負荷が予測可能になる

### 1.2 DB スキャンのクエリ方針

```sql
-- active CTE: namespace ごとの active ジョブ数を集計
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
WHERE COALESCE(a.active_count, 0) < :dispatch_limit          -- budget に余裕がある namespace のみ
  AND q.rn <= :dispatch_limit - COALESCE(a.active_count, 0)  -- 残り budget 分だけ取得
ORDER BY q.rn ASC,                        -- ラウンドロビン（各 namespace から1件ずつ交互）
         COALESCE(a.active_count, 0) ASC,  -- active が少ない namespace を優先
         q.namespace ASC                   -- 同率の場合は namespace 名で決定的に順序付け
LIMIT :batch_size;                         -- 1サイクルの総取得数を固定
```

このクエリは `idx_jobs_namespace_status` インデックスにより効率化される。

**ラウンドロビンの仕組み：** `ROW_NUMBER()` を QUEUED ジョブのみに振り、`ORDER BY rn ASC` とすることで、各 namespace の rn=1（最古の QUEUED ジョブ）が全 namespace 分先に並び、次に rn=2 が全 namespace 分並ぶ。`LIMIT :batch_size` で打ち切ることで、namespace 間で均等に dispatch される。

**公平性の保証：** `active_count ASC` でソートすることで、現在の active ジョブ数が少ない namespace が優先される。これにより namespace 数が `DISPATCH_BATCH_SIZE` を超えても、リソース消費の少ないユーザーから先に dispatch され、公平性が維持される。

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
            # §1.2 のクエリ（ラウンドロビン・active_count 優先・LIMIT batch_size）
            candidates = db.fetch_dispatchable_jobs()
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

        # DISPATCHING への更新が確定したので続行
        try:
            k8s.create_job(job)
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
```

### 2.4 起動時の初期化処理

Dispatcher 再起動時に `DISPATCHING` で止まっているジョブを `QUEUED` に戻す。

```python
def on_startup():
    db.reset_stale_dispatching_jobs()
    # UPDATE jobs SET status = 'QUEUED', retry_after = NULL WHERE status = 'DISPATCHING'
```

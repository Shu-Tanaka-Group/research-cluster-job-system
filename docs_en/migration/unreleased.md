> *This document was auto-translated from the [Japanese original](../../docs/migration/unreleased.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Unreleased Migration Procedures

This file is a working document describing migration procedures for the **next release**. At release time, rename it to the version name (e.g., `v1.16.0.md`) and create a new `unreleased.md` (see [versioning.md](../versioning.md)).

If there are migration procedures specific to the next release in addition to the [standard migration procedures](../migration.md), append them below.

## Deployment Order (Watcher Before Dispatcher)

> Related: issue #207 / PR #211

Time limit enforcement moved from the Dispatcher (setting `activeDeadlineSeconds`) to the Watcher (enforcement measured from `started_at`), so **the Watcher must be deployed before the Dispatcher**.

In the reverse order, the Dispatcher would create Jobs without `activeDeadlineSeconds` while the Watcher-side enforcement is not yet active, leaving jobs submitted during that window with no time limit at all.

Following the deployment order in the [standard migration procedures](../migration.md) (`watcher` → `dispatcher` → `submit-api`) is sufficient.

## Old Behavior Remaining for Jobs Running at Rollout Time

> Related: issue #207 / PR #211

The time limit enforcement mechanism changed from the K8s Job's `activeDeadlineSeconds` to Watcher-side enforcement measured from `started_at` (see [watcher.md](../architecture/watcher.md) §3 Step 9).

K8s Jobs created by the Dispatcher before this version still carry `activeDeadlineSeconds` and will continue to be terminated by K8s based on that value after the rollout. Therefore, jobs that are RUNNING or DISPATCHED at rollout time retain the old behavior (measurement from the Kueue admission point, i.e. including the time the Pod spent Pending).

- Such jobs are also watched by the new logic, but the `started_at`-based decision always comes after termination by `activeDeadlineSeconds`, so there is no actual harm (K8s terminates first, and the existing path that maps `DeadlineExceeded` to `time limit exceeded` marks the job FAILED)
- If the old behavior is unacceptable, either wait for the running jobs to finish before rolling out, or `cjob cancel` the affected jobs and resubmit them

Newly created K8s Jobs are not given `activeDeadlineSeconds`, so jobs submitted after the rollout use the new behavior.

## Adding the DISPATCHED Stall Guard Settings to `cjob-config`

> Related: issue #208 / PR #212

Two new standard keys are added to the `cjob-config` ConfigMap.

| Key | Default | Purpose |
|---|---|---|
| `WATCHER_DISPATCH_TIMEOUT_SEC` | `"1800"` | Seconds after which a job that has not reached RUNNING while DISPATCHED is treated as unschedulable, its K8s Job deleted, and the job requeued to QUEUED |
| `WATCHER_DISPATCH_BACKOFF_MAX_SEC` | `"7200"` | Ceiling in seconds for the exponential backoff written to `retry_after` on requeue |

After the base ConfigMap has been applied with `kubectl apply -k overlays/<env>`, do one of the following.

- If you use the base ConfigMap as-is: no additional work is needed
- If you patch the contents of `cjob-config` in your own overlay: add the two keys above to the overlay's ConfigMap patch before applying. Even without explicit values the Python-side defaults apply, but adding them to the ConfigMap is recommended so the output of `cjobctl config show` matches

Set `WATCHER_DISPATCH_TIMEOUT_SEC` well above the gap filling stall threshold (`GAP_FILLING_STALL_THRESHOLD_SEC`, default 300 seconds). If it is shorter, the stall guard requeues a large job before gap filling has a chance to start it.

## Run the DB Schema Update (`jobs.unschedulable_count`) Before Step 4

> Related: issue #208 / PR #212

`unschedulable_count INTEGER NOT NULL DEFAULT 0` is added to the `jobs` table. It is applied idempotently by Step 5 (`cjobctl db migrate`) of the [standard migration procedures](../migration.md), and existing rows are filled with the default 0, so no additional data migration is required.

For this version, however, **run Step 5 before Step 4 (applying K8s resources)**. The new Watcher writes to this column and the new Dispatcher reads it in its stalled-job detection query, so if the new components start while the column is missing, the reconcile and dispatch cycles keep failing with SQL errors.

From the old code's point of view `ADD COLUMN ... DEFAULT 0` merely adds an unreferenced column, so applying it early does not affect components running the previous version.

```bash
# Run after building cjobctl in Step 3, before Step 4
cjobctl db migrate
```

## Re-import the Grafana Dashboard

> Related: issue #208 / PR #212

An "Awaiting Placement (Backoff)" panel was added to Row 3 of `k8s/base/grafana/dashboard-user.json`, and the width of the "Queue Usage by Flavor" table was changed from 24 to 18. The new panel reads `jobs.unschedulable_count`, so re-import it **after the DB schema update above**.

1. Upload the updated JSON from `Dashboards > Import` in the Grafana UI
2. Overwrite the existing dashboard (same UID)
3. Select the data source variables (`${DS_PROMETHEUS}` / `${DS_CJOB_DB}`) to match your environment


> *This document was auto-translated from the [Japanese original](../../docs/migration/unreleased.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Unreleased Migration Procedures

This file is a working document describing migration procedures for the **next release**. At release time, rename it to the version name (e.g., `v1.16.0.md`) and create a new `unreleased.md` (see [versioning.md](../versioning.md)).

If there are migration procedures specific to the next release in addition to the [standard migration procedures](../migration.md), append them below.

## Old Behavior Remaining for Jobs Running at Rollout Time

The time limit enforcement mechanism changed from the K8s Job's `activeDeadlineSeconds` to Watcher-side enforcement measured from `started_at` (see [watcher.md](../architecture/watcher.md) §3 Step 9).

K8s Jobs created by the Dispatcher before this version still carry `activeDeadlineSeconds` and will continue to be terminated by K8s based on that value after the rollout. Therefore, jobs that are RUNNING or DISPATCHED at rollout time retain the old behavior (measurement from the Kueue admission point, i.e. including the time the Pod spent Pending).

- Such jobs are also watched by the new logic, but the `started_at`-based decision always comes after termination by `activeDeadlineSeconds`, so there is no actual harm (K8s terminates first, and the existing path that maps `DeadlineExceeded` to `time limit exceeded` marks the job FAILED)
- If the old behavior is unacceptable, either wait for the running jobs to finish before rolling out, or `cjob cancel` the affected jobs and resubmit them

Newly created K8s Jobs are not given `activeDeadlineSeconds`, so jobs submitted after the rollout use the new behavior.

> *This document was auto-translated from the [Japanese original](../../docs/migration/unreleased.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Unreleased Migration Procedures

This file is a working document describing migration procedures for the **next release**. At release time, rename it to the version name (e.g., `v1.14.0.md`) and create a new `unreleased.md` (see [versioning.md](../versioning.md)).

If there are migration procedures specific to the next release in addition to the [standard migration procedures](../migration.md), append them below.

## Re-importing the Grafana Dashboard

The user-facing dashboard at `k8s/base/grafana/dashboard-user.json` has been revised with the following panel wording and SQL changes. Re-import from the Grafana UI is required (see [deployment.md](../deployment.md) §17.5).

- Renamed "Waiting Jobs" to "Jobs Awaiting Resource Allocation". The aggregation target was changed from `QUEUED + DISPATCHING + DISPATCHED` to `DISPATCHED` only.
- "Queue Usage by Flavor": the "Waiting" column was renamed to "Awaiting Resource Allocation" and its aggregation target narrowed to `DISPATCHED` only. A new "Submitted" (`QUEUED`) column was added so that the per-flavor queue state is visible across all active states (QUEUED / DISPATCHED / RUNNING / HELD).
- "Queue Job Count Over Time": the "Waiting" legend was renamed to "Awaiting Resource Allocation".
- "Job Status Breakdown" pie chart: `DISPATCHING` is now excluded from the display. `QUEUED` was relabeled to "Submitted" and `DISPATCHED` to "Allocation Wait".
- "Resource Allocation Wait (P50)" and "Resource Allocation Wait Time Over Time (P50 / P95)": the Japanese okurigana "て" was removed from the title (unifying the term as "リソース割当待ち").

## Adding `NODE_BIN_PACKING_ENABLED` to the ConfigMap

A per-node bin-packing precheck has been added to the Dispatcher (see [dispatcher.md](../architecture/dispatcher.md) §2.6). Add a new key `NODE_BIN_PACKING_ENABLED` (default `"true"`) to the `cjob-config` ConfigMap. The Dispatcher Deployment references this key with `optional: true`, so the Pydantic default of `true` will be applied even if the key is not set; however, setting it explicitly is recommended for operational clarity.

Set the value to `"false"` only if disabling it (reverting to the previous behavior) is required. When disabled, admission is performed using only Kueue's per-flavor `nominalQuota`, which may cause a recurrence of the issue where jobs fit within the aggregate quota but cannot be placed on any individual node, leaving them stuck in the `DISPATCHED` state.

## Kubernetes Minimum Version Requirement

The Watcher's RUNNING determination now consults the `status.ready` field of K8s Jobs (the `JobReadyPods` feature). The minimum cluster requirement is now stated explicitly as **Kubernetes v1.26 or later**, the version where `JobReadyPods` reaches GA (see [prerequisites.md](../architecture/prerequisites.md) §1).

If deployed onto a cluster older than v1.26, `status.ready` will always be unset (None) and the Watcher will never transition jobs to RUNNING. Verify the cluster's K8s version before upgrading.

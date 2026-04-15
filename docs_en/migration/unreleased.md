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

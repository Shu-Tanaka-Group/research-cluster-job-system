> *This document was auto-translated from the [Japanese original](../../docs/migration/unreleased.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Unreleased Migration Procedures

This file is a working document describing migration procedures for the **next release**. At release time, rename it to the version name (e.g., `v1.13.0.md`) and create a new `unreleased.md` (see [versioning.md](../versioning.md)).

If there are migration procedures specific to the next release in addition to the [standard migration procedures](../migration.md), append them below.

## cjobctl CLI Command Changes

`cjobctl counters list` has been migrated to `cjobctl jobs counters`. If existing scripts or procedures use the old command, update them accordingly.

## Remove `cohortName` / `lendingLimit` from ClusterQueue

The ClusterQueue design has been revised: `cohortName: cjob-cohort` and the `lendingLimit: "0"` previously set on GPU flavors have been removed. Because the Dispatcher always sets a per-flavor `nodeSelector` on Job Pods, Kueue's flavor matching structurally prevents any job from consuming another flavor's quota, and these settings were effectively no-ops.

In existing environments, update the ClusterQueue resource with the following steps:

```bash
kubectl edit clusterqueue cjob-cluster-queue
```

Apply these changes:

1. Remove the `spec.cohortName` line
2. Remove all `spec.resourceGroups[0].flavors[*].resources[*].lendingLimit` lines

After the change, confirm that the `(lendingLimit: 0)` annotations disappear from the output of `cjobctl cluster show-quota`. Kueue's admission behavior does not change, so there is no impact on running or pending jobs.

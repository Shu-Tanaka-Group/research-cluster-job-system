> *This document was auto-translated from the [Japanese original](../../docs/migration/unreleased.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Unreleased Migration Procedures

This file is a working document describing migration procedures for the **next release**. At release time, rename it to the version name (e.g., `v1.14.0.md`) and create a new `unreleased.md` (see [versioning.md](../versioning.md)).

If there are migration procedures specific to the next release in addition to the [standard migration procedures](../migration.md), append them below.

## Watcher memory limit raised

Along with pagination and lightweight dataclass conversion of Watcher's K8s API calls, `k8s/base/watcher/deployment.yaml` has been updated to change `resources.limits.memory` from `256Mi` to `1Gi` (request stays at 128Mi). Apply the overlay with `kubectl apply -k` and rollout the watcher Deployment to activate the new memory limit.

```bash
kubectl apply -k overlays/<env>
kubectl rollout restart deployment watcher -n cjob-system
```

If your custom overlay overrides the watcher `resources` block, update the overlay-side memory limit to an equivalent or higher value (1Gi recommended).

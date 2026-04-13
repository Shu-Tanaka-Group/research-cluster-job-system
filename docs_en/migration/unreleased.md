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

## `WATCHER_K8S_LIST_PAGE_SIZE` added to `cjob-config`

A new standard key `WATCHER_K8S_LIST_PAGE_SIZE` (default `"500"`) has been added to the `cjob-config` ConfigMap. After the base ConfigMap is applied with `kubectl apply -k overlays/<env>`, run one of the following:

- Standard case: `cjobctl system restart watcher` (env is read from the ConfigMap)
- If your custom overlay patches `cjob-config`: add `WATCHER_K8S_LIST_PAGE_SIZE: "500"` to the overlay-side ConfigMap patch and apply. Even if the value is not set explicitly, the Python-side default (500) still applies, but listing it in the ConfigMap is recommended so it appears in `cjobctl config show` output

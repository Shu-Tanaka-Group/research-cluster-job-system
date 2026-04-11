> *This document was auto-translated from the [Japanese original](../docs/migration.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Version Migration Procedures

Standard procedures for updating a running CJob system to a new version. Skip unnecessary steps depending on the nature of the changes.

> **Step ordering principle**: Each step is ordered based on dependencies. When one step's output is required by another step, prepare the output first.
>
> - Build and push images → Update and apply Kustomize tags (do not reference tags that do not exist yet)
> - Build cjobctl → Run DB migration (execute with the new migration logic)

## Prerequisites

- Repository has been cloned
- Overlay has been created (see [deployment.md](deployment.md) §17)
- Cluster is accessible via `kubectl`
- Docker is available for building and pushing images

## Step 1: Update Repository and Review Diff

```bash
cd /path/to/stg-cluster-job-system
git fetch && git checkout <VERSION>
```

Check whether any keys have been added to the base ConfigMap. If there are additions, decide whether to reflect tuned values in the overlay's `configmap-cjob-config.yaml` or leave them at their default values.

```bash
git diff <old-tag>...<new-tag> -- k8s/base/configmap-cjob-config.yaml
```

## Step 2: Build and Push Images

Run this step if there are changes to server components (Python). You can check for changes with `git diff <old-tag>...<new-tag> -- server/src/`.

```bash
read -r VERSION < VERSION

# Build and push only the components that have changed
docker build -t your-registry/cjob-submit-api:${VERSION} -f server/Dockerfile.api server/
docker build -t your-registry/cjob-dispatcher:${VERSION} -f server/Dockerfile.dispatcher server/
docker build -t your-registry/cjob-watcher:${VERSION} -f server/Dockerfile.watcher server/

docker push your-registry/cjob-submit-api:${VERSION}
docker push your-registry/cjob-dispatcher:${VERSION}
docker push your-registry/cjob-watcher:${VERSION}
```

If there are no changes to server components, retag and push the existing images.

```bash
read -r VERSION < VERSION

# Example for Submit API. Do the same for other components.
docker tag your-registry/cjob-submit-api:${OLD_VERSION} your-registry/cjob-submit-api:${VERSION}
docker push your-registry/cjob-submit-api:${VERSION}
```

## Step 3: Build CLI / Admin Tools

### 3.1 cjobctl (Admin PC)

Build if there are changes in `ctl/`, **or if you are running a DB schema update (Step 5)**.

```bash
cd ctl/
cargo build --release
```

### 3.2 cjob CLI (for user distribution)

Build if there are changes in `cli/`. See [build.md](build.md) for cross-compilation details.

```bash
cd cli/
cargo build --release --target x86_64-unknown-linux-musl
```

## Step 4: Apply K8s Resources

Update `newTag` in the overlay's `kustomization.yaml` to the new version (confirm that the image has been pushed in Step 2 before doing this).

```bash
kubectl apply -k /path/to/my-overlay
```

This updates ConfigMaps, RBAC, Deployments, and other resources all at once. If the Deployment's image tag has changed, a rolling update is performed automatically.

> The `postgres-schema` ConfigMap is only executed on the initial PostgreSQL startup. For existing databases, apply changes using `cjobctl db migrate` in Step 5.

If there are changes to Kyverno policies, apply them individually since they are not managed by Kustomize.

**Note on deployment order**: If there are data dependencies between components, deploy the data-producing side first. For example, if the Watcher writes data to the DB and the Dispatcher or Submit API reads that data, deploy the Watcher first. If there are no dependencies, order does not matter.

```bash
kubectl rollout status deployment/watcher -n cjob-system
kubectl rollout status deployment/dispatcher -n cjob-system
kubectl rollout status deployment/submit-api -n cjob-system
```

## Step 5: Update DB Schema

Run this step if there are additions to tables or columns. This can be executed idempotently via `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS`. Use the new `cjobctl` built in Step 3.

```bash
cjobctl db migrate
```

## Step 6: Distribute cjob CLI

If you built the CLI in Step 3.2, deploy the binary to the PVC using `cjobctl cli deploy`. Users can self-update using `cjob update`.

```bash
read -r VERSION < VERSION
cjobctl cli deploy --binary ./target/x86_64-unknown-linux-musl/release/cjob --version ${VERSION}
```

## Step 7: Verify Operation

```bash
# Component status
cjobctl system status

# Job submission test
cjob add --cpu 1 --memory 1Gi -- echo "upgrade test"
cjob list
```

If there are version-specific verification items, refer to the PR's Test plan or the version-specific migration procedure.

## Version-Specific Migration Procedures

For versions that require additional work beyond the standard procedure, version-specific migration procedures are provided in the `docs_en/migration/` directory. When updating to such a version, refer to these alongside the standard procedures. Files may not exist for versions with minor changes.

- [v1.13.0](migration/v1.13.0.md) — Retire `cjobctl counters list`, remove ClusterQueue cohortName/lendingLimit, add `DISPATCHER_METRICS_PORT`
- [v1.12.0](migration/v1.12.0.md) — Per-flavor DRF weight, namespace_daily_usage flavor column addition, weight Float type conversion, USAGE_RETENTION_DAYS setting addition
- [v1.11.0](migration/v1.11.0.md) — Dispatcher metrics, flavor-aware budget, count/jobs.batch pre-check, effective allocatable
- [v1.10.0](migration/v1.10.0.md) — Enable Prometheus metrics, unified flavor node labels

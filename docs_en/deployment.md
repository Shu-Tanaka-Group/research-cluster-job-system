> *This document was auto-translated from the [Japanese original](../docs/deployment.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# CJob Deployment Design

## 1. Overview

This design document covers the deployment and configuration management of the CJob system on Kubernetes.

### Manifest Management

K8s manifests are managed using Kustomize base / overlay structure.

```
In the repository:
  k8s/
    base/              # Environment-independent manifests (including default values)
    overlay-example/   # Sample overlay (copy and use)

Outside the repository (created by administrators):
  my-overlay/
    kustomization.yaml              # Reference to base, image, StorageClass, ConfigMap patches
    configmap-cjob-config.yaml      # Tuned ConfigMap values
```

The base contains all manifests with default values, and environment-specific values are overridden by **overlays placed outside the repository**. See `k8s/overlay-example/` for a sample overlay.

Deployment is done by cloning the repository and specifying the overlay.

```bash
kubectl apply -k /path/to/my-overlay
```

Secrets (`postgres-secret`) are not managed by Kustomize and must be created manually by the administrator. See `k8s/base/secret-postgres.yaml` for a template.

The following environment-dependent values are managed in overlays:

| Setting | How to Set in Overlay |
|---|---|
| Image name/tag | `images[].newName` / `images[].newTag` |
| StorageClass | `patches[]` (JSON Patch) |
| ConfigMap `cjob-config` values | `patches[]` (overridden via `configmap-cjob-config.yaml`) |

---

## 2. Namespace Structure

```
cjob-system        : All system components (Submit API / Dispatcher / Watcher / PostgreSQL)
<user-namespace>   : Per-user execution environment (User Pod / Job Pod / LocalQueue / ResourceQuota / PVC)
```

User namespaces can use any name (e.g., `alice`, `user-alice`, `lab-physics`).
Identification is done via the label `cjob.io/user-namespace=true`, and the username is obtained from the namespace annotation `cjob.io/username`.

---

## 3. Component Placement

| Component | Kind | Replicas | Namespace |
|---|---|---|---|
| Submit API | Deployment | 2 or more recommended | cjob-system |
| Dispatcher | Deployment | 1 | cjob-system |
| Watcher / Reconciler | Deployment | 1 | cjob-system |
| PostgreSQL | StatefulSet | 1 | cjob-system |

Reason for fixing Dispatcher and Watcher at 1 replica: Multiple replicas would cause duplicate dispatching and duplicate DB updates.
Submit API is stateless, so it is safe to increase replicas. 2 or more replicas are recommended for improved availability.

---

## 4. PVC Configuration

PostgreSQL has a PVC. StorageClass uses NFS subdir external provisioner.

| PVC Name | Target | Purpose |
|---|---|---|
| `postgres-data` | PostgreSQL | Persistence of DB files |
| `cli-binary` | Submit API | Storage for CLI binary distribution |

### 4.1 `cli-binary` PVC

Stores binaries distributed by the CLI self-update feature (`cjob update`). Mounted read-only by the Submit API Pod and served via the `/v1/cli/version` and `/v1/cli/download` endpoints.

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: cli-binary
  namespace: cjob-system
spec:
  accessModes: ["ReadWriteMany"]
  storageClassName: managed-nfs-storage
  resources:
    requests:
      storage: 1Gi
```

Directory structure:

```
/cli-binary/
  latest          # Text file containing the latest version number (e.g., "1.2.0")
  1.1.0/
    cjob          # linux/amd64 binary
  1.2.0/
    cjob
```

Binary deployment is done with `cjobctl cli deploy` (see [cjobctl.md](architecture/cjobctl.md) §5.6).

```bash
# After building CLI, deploy to PVC (see build.md §3 for build instructions)
cjobctl cli deploy --binary ./cli/target/x86_64-unknown-linux-musl/release/cjob --version <version>
```

For the complete operational procedure, see [operations.md](operations.md) §8.

---

## 5. Secret Design

All created in the `cjob-system` namespace.

### 5.1 `postgres-secret`

Source: [`secret-postgres.yaml`](../k8s/base/secret-postgres.yaml)

Key design points:
- Not managed by Kustomize; the administrator creates it manually with `kubectl create secret` during initial setup
- Creation instructions are documented in comments within the template file

---

## 6. ConfigMap Design

### 6.1 `cjob-config` (Common Settings)

Referenced by Submit API, Dispatcher, and Watcher in common.

Source: [`configmap-cjob-config.yaml`](../k8s/base/configmap-cjob-config.yaml)

Key design points:
- Default values are defined in base, and environment-specific values are overridden via overlay patches (see `k8s/overlay-example/`)
- `USER_NAMESPACE_LABEL` is not injected into Submit API / Dispatcher env among server components. Watcher uses it for ResourceQuota sync ([watcher.md](architecture/watcher.md) §1.3) so it is injected into env. NetworkPolicy's `namespaceSelector` and cjobctl's `weight exclusive` command also reference it
- See [resources.md](architecture/resources.md) for the meaning and design rationale of each setting value
- The default value of `LOG_BASE_DIR` (`/home/jovyan/.cjob/logs`) is a path under `WORKSPACE_MOUNT_PATH` (default `/home/jovyan`). If you change `WORKSPACE_MOUNT_PATH`, also change `LOG_BASE_DIR` accordingly. If `LOG_BASE_DIR` points outside `WORKSPACE_MOUNT_PATH`, logs will not be written to the PVC and will be lost

### 6.2 Injection Pattern for Each Component

A common pattern is used across all components.

```yaml
# Example env section of a Deployment
env:
  - name: POSTGRES_HOST
    valueFrom:
      configMapKeyRef:
        name: cjob-config
        key: POSTGRES_HOST
  - name: POSTGRES_PASSWORD
    valueFrom:
      secretKeyRef:
        name: postgres-secret
        key: POSTGRES_PASSWORD
```

### 6.3 Resources Referenced by Each Component

| Component | ConfigMap | Secret |
|---|---|---|
| Submit API | `cjob-config` | `postgres-secret` |
| Dispatcher | `cjob-config` | `postgres-secret` |
| Watcher | `cjob-config` | `postgres-secret` |
| PostgreSQL | - | `postgres-secret` |
| CLI (distributed via `cjob update`) | - (log path obtained from API) | - |
| cjobctl | `cjob-config` (referenced via `config show`) | - |
| NetworkPolicy | - (`USER_NAMESPACE_LABEL` / `PROMETHEUS_NAMESPACE_LABEL` values hardcoded in YAML) | - |

`USER_NAMESPACE_LABEL` is not injected into Submit API / Dispatcher env. Watcher uses it for ResourceQuota sync so it is injected into env. NetworkPolicy's `namespaceSelector` and cjobctl's `weight exclusive` command also reference it. `PROMETHEUS_NAMESPACE_LABEL` is similarly hardcoded in NetworkPolicy and overridden via overlay.

---

## 7. Runtime Image Design

### 7.1 Image Role

The same image (obtained from the User Pod's environment variable `CJOB_IMAGE` or `JUPYTER_IMAGE`) is used for two purposes. The `cjob` CLI is not included in the image; users install it individually.

| Purpose | Pod | Notes |
|---|---|---|
| User work environment | User Pod (JupyterHub) | User installs cjob CLI separately |
| Job execution environment | Job Pod (Kubernetes Job) | CLI not required |

### 7.2 Image Contents

| Category | Packages / Settings | Reason |
|---|---|---|
| Base OS | Any (e.g., Ubuntu 24.04) | `/bin/bash` must be available |
| Python | python3.12 python3.12-venv python3-pip | Base for virtual environments |
| Build tools | gcc g++ make | Building C extension libraries |
| Scientific computing libraries | libopenblas-dev liblapack-dev | Dependencies for numpy, etc. |
| HPC tools | openmpi-bin | MPI job support |
| Basic tools | git curl wget vim | Utility |

Not included: `cjob` CLI (distributed via Submit API through `cjob update`), user Python packages (each user manages venvs under `/home/jovyan`), CUDA / GPU drivers (outside initial scope), Jupyter itself (managed by JupyterHub).

### 7.3 Dockerfile

```dockerfile
FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    python3.12 \
    python3.12-venv \
    python3-pip \
    gcc \
    g++ \
    make \
    libopenblas-dev \
    liblapack-dev \
    openmpi-bin \
    git \
    curl \
    wget \
    vim \
    && rm -rf /var/lib/apt/lists/*
```

### 7.4 cjob CLI Distribution

The `cjob` CLI is distributed as a Rust single binary via the Submit API.
Pre-built binaries are placed on a PVC (`cli-binary`) in the `cjob-system` namespace, and the Submit API serves them through endpoints (`/v1/cli/version`, `/v1/cli/download`).
Users can self-update with the `cjob update` command.

For initial installation, since the binary doesn't exist yet, the administrator either distributes it directly or the user fetches it from the Submit API as follows:

```bash
mkdir -p /home/jovyan/.local/bin
curl -L http://submit-api.cjob-system.svc.cluster.local:8080/v1/cli/download \
  -o /home/jovyan/.local/bin/cjob
chmod +x /home/jovyan/.local/bin/cjob
```

#### CLI Environment Variables

The Submit API endpoint is configured via the `CJOB_API_URL` environment variable.
When not set, the default value (`http://submit-api.cjob-system.svc.cluster.local:8080`) is used.

No configuration is needed on the CLI side for the log directory path. Since the CLI obtains the log path from the API, changing the server-side ConfigMap (`LOG_BASE_DIR`) is automatically reflected in the CLI.

---

## 8. Submit API ServiceAccount and RBAC

Source: [`rbac-submit-api.yaml`](../k8s/base/rbac-submit-api.yaml)

Key design points:
- ClusterRole `token-reviewer` is granted so that Submit API can call the TokenReview API
- The `get` permission on namespaces is needed to obtain the username from the `cjob.io/username` annotation of user namespaces

---

## 9. Dispatcher / Watcher ServiceAccount and RBAC

Source: [`rbac-dispatcher.yaml`](../k8s/base/rbac-dispatcher.yaml)

Key design points:
- Watcher shares `dispatcher-sa` with Dispatcher for management simplicity. Watcher's Deployment specifies `serviceAccountName: dispatcher-sa`
- ClusterRole `cjob-job-controller` permits Job CRUD, Pod read, Node read, Namespace listing, ResourceQuota listing, and ClusterQueue read
- Node `get`/`list` permissions are needed for the Watcher to sync node information to the `node_resources` table
- Namespace `list` permission is needed for the Watcher to enumerate user namespaces with the `USER_NAMESPACE_LABEL` label
- ResourceQuota `list` permission is needed for the Watcher to batch-sync ResourceQuota usage for all user namespaces to the `namespace_resource_quotas` table
- ClusterQueue `get` permission (`kueue.x-k8s.io` API group) is needed for the Watcher to sync nominalQuota to the `flavor_quotas` table

---

## 10. NetworkPolicy

Sources:
- [`networkpolicy-allow-submit-api.yaml`](../k8s/base/networkpolicy-allow-submit-api.yaml)
- [`networkpolicy-allow-metrics-scrape.yaml`](../k8s/base/networkpolicy-allow-metrics-scrape.yaml)

Key design points:
- This cluster has no default-deny NetworkPolicy, so Pod-to-Pod communication within the `cjob-system` namespace (e.g., Submit API ↔ PostgreSQL) is not restricted
- `allow-submit-api`: Allows communication from user namespaces (labeled `cjob.io/user-namespace: "true"`) to Submit API (TCP 8080). Restricts access from non-user namespaces to ensure security
- `allow-metrics-scrape`: Allows metrics scraping from the Prometheus namespace to Submit API (TCP 8080). The base default is `kubernetes.io/metadata.name: monitoring`. For different namespaces, patch `namespaceSelector.matchLabels` in the overlay (see `overlay-example/kustomization.yaml`)

---

## 11. Namespace Creation Script (Complete Version)

Script to run when creating a namespace for a new user.

```bash
#!/bin/bash
set -euo pipefail

NS_NAME=$1
USERNAME=$2

if [ -z "${NS_NAME}" ] || [ -z "${USERNAME}" ]; then
  echo "Usage: $0 <namespace-name> <username>"
  exit 1
fi

echo "Creating namespace and resources: ns=${NS_NAME}, user=${USERNAME}"

# Create namespace
kubectl create namespace ${NS_NAME}

# Apply user namespace identification label and username annotation
kubectl label namespace ${NS_NAME} type=user
kubectl label namespace ${NS_NAME} cjob.io/user-namespace=true
kubectl annotate namespace ${NS_NAME} cjob.io/username=${USERNAME}

# Create ServiceAccount for User Pod
kubectl create serviceaccount computing-user -n ${NS_NAME}

# JupyterHub KubeSpawner configuration (config.yaml)
# Must have service_account: computing-user configured

# Create Kueue LocalQueue
# The LocalQueue name ("default" below) must match KUEUE_LOCAL_QUEUE_NAME in the ConfigMap.
# If changing KUEUE_LOCAL_QUEUE_NAME, also update the LocalQueue name in this script.
kubectl apply -f - <<EOF
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: default   # Must match the value of KUEUE_LOCAL_QUEUE_NAME
  namespace: ${NS_NAME}
spec:
  clusterQueue: cjob-cluster-queue
EOF

# Create ResourceQuota
kubectl apply -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: computing-quota
  namespace: ${NS_NAME}
spec:
  hard:
    count/jobs.batch: "50"
    requests.cpu: "300"
    requests.memory: "1250Gi"
    limits.cpu: "300"
    limits.memory: "1250Gi"
    requests.nvidia.com/gpu: "4"
    limits.nvidia.com/gpu: "4"
EOF

echo "Done: ns=${NS_NAME}, user=${USERNAME}"
```

---

## 12. JupyterHub Configuration

KubeSpawner configuration to assign the `computing-user` ServiceAccount to User Pods.

```yaml
# JupyterHub config.yaml
hub:
  config:
    KubeSpawner:
      service_account: computing-user
```

### Job Pod Image Environment Variable

The `cjob` CLI obtains the image name for Job Pods from User Pod environment variables in the following priority order:

1. `CJOB_IMAGE` (preferred)
2. `JUPYTER_IMAGE` (fallback)

In JupyterHub environments, `JUPYTER_IMAGE` is automatically injected when the User Pod starts, so no additional configuration changes are needed.

When using in non-JupyterHub environments, set the `CJOB_IMAGE` environment variable on the User Pod with the image name as its value.

```
CJOB_IMAGE=my-registry/my-image:1.0
```

---

## 13. Deployment / StatefulSet YAML

### 13.1 PostgreSQL ConfigMap (Schema Definition)

Source: [`configmap-postgres-schema.yaml`](../k8s/base/configmap-postgres-schema.yaml)

Key design points:
- Schema SQL is stored in a ConfigMap and applied via the PostgreSQL official image's initdb auto-execution mechanism (`/docker-entrypoint-initdb.d/`)
- Uses `IF NOT EXISTS` for safe re-execution during redeployment (idempotency guaranteed)
- For table design details, see [database.md](architecture/database.md)

### 13.2 PostgreSQL StatefulSet

Source: [`postgres/statefulset.yaml`](../k8s/base/postgres/statefulset.yaml)

Key design points:
- Uses Headless Service (`clusterIP: None`) (required for StatefulSet DNS resolution)
- Mounts the `postgres-schema` ConfigMap to `/docker-entrypoint-initdb.d/` for automatic schema initialization
- StorageClass uses a placeholder value (`STORAGE_CLASS`) in base, overridden to match the environment via overlay
- Replica is fixed at 1 (single instance configuration)

### 13.3 Submit API Deployment

Source: [`submit-api/deployment.yaml`](../k8s/base/submit-api/deployment.yaml)

Key design points:
- Stateless, so 2 or more replicas recommended (improved availability)
- Mounts `cli-binary` PVC as ReadOnly, used by CLI binary distribution endpoints (`/v1/cli/*`)
- Liveness / Readiness probes use the `/healthz` endpoint
- Image name is overridden from overlay via Kustomize's `images[]` (base uses short name only)

### 13.4 Dispatcher Deployment

Source: [`dispatcher/deployment.yaml`](../k8s/base/dispatcher/deployment.yaml)

Key design points:
- Replica fixed at 1 (multiple replicas would cause duplicate dispatching)
- Liveness probe uses file timestamp method: the main loop touches `/tmp/liveness` every `DISPATCH_BUDGET_CHECK_INTERVAL_SEC`, and if the last update was more than 120 seconds ago, the loop is considered stopped and a restart is triggered
- Uses `dispatcher-sa` (shared with Watcher, see §9)

### 13.5 Watcher Deployment

Source: [`watcher/deployment.yaml`](../k8s/base/watcher/deployment.yaml)

Key design points:
- Replica fixed at 1 (multiple replicas would cause duplicate DB updates)
- Liveness probe uses file timestamp method: the main loop periodically touches `/tmp/liveness`, and if the last update was more than 120 seconds ago, the loop is considered stopped and a restart is triggered
- Specifies `serviceAccountName: dispatcher-sa` to share the ServiceAccount with Dispatcher (see §9)

---

## 14. Image Restriction via Kyverno

Restricts images usable by Job Pods and prevents users from overriding images.
Uses Kyverno ClusterPolicy to reject images not on the allowlist for Jobs in user namespaces.

### 14.1 Kyverno Installation

```bash
helm repo add kyverno https://kyverno.github.io/kyverno/
helm repo update
helm upgrade kyverno kyverno/kyverno -n kyverno --install --create-namespace --version 3.7.1
```

### 14.2 Applying the ClusterPolicy

Only allows images starting with `your-registry/cjob-*`.
Only Jobs in user namespaces (labeled `cjob.io/user-namespace: "true"`) are targeted; system components in the `cjob-system` namespace are unaffected.

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: restrict-job-image
spec:
  validationFailureAction: Enforce
  rules:
    - name: allowed-images
      match:
        resources:
          kinds: ["Job"]
          namespaceSelector:
            matchLabels:
              cjob.io/user-namespace: "true"
      validate:
        message: "Unauthorized image. Only images matching your-registry/cjob-* are allowed."
        pattern:
          spec:
            template:
              spec:
                containers:
                  - image: "your-registry/cjob-*"
```

```bash
kubectl apply -f policies/restrict-job-image.yaml
```

---

## 15. Kueue Installation

Download the manifests:

```bash
curl -L -o kueue-manifests.yaml https://github.com/kubernetes-sigs/kueue/releases/download/v0.16.4/manifests.yaml
```

Find the kueue-manager-config ConfigMap in the file and modify the integrations section of controller_manager_config.yaml. Without this change, all resources in namespaces where Kueue Queues are applied will be managed by Kueue. For example, if JupyterHub user namespaces are targeted by Kueue, Notebook Pods will also be subject to Kueue management and will fail to start.

```yaml
integrations:
  frameworks:
    - "batch/job"
    # - Pos # ← Remove Pod, Deployment, etc. from scope
```

### 15.1 Prometheus Metrics Scrape Configuration

#### CJob Application Metrics

Submit API and Watcher have `prometheus.io/scrape` annotations in their Pod templates. In Prometheus environments using annotation-based discovery, this alone enables automatic scraping.

In environments using Prometheus Operator, add the `base/prometheus-operator` directory to the overlay resources (see `overlay-example/kustomization.yaml`).

| Resource | File | Target | Port | Path |
|---|---|---|---|---|
| ServiceMonitor `submit-api` | `prometheus-operator/servicemonitor-submit-api.yaml` | Submit API Service | `http` (8080) | `/metrics` |
| PodMonitor `watcher` | `prometheus-operator/podmonitor-watcher.yaml` | Watcher Pod | `metrics` (9090) | `/metrics` |

Verify that Prometheus Operator's `serviceMonitorNamespaceSelector` / `podMonitorNamespaceSelector` includes the `cjob-system` namespace in its monitoring targets.

After applying, search for `cjob_jobs_submitted_total` in Grafana's Explore view to verify that metrics are displayed.

#### Kueue Metrics

Create a ServiceMonitor for collecting Kueue metrics with Prometheus. Without this, Kueue-related panels in the Grafana dashboard will not be displayed.

```bash
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.16.4/prometheus.yaml
```

After applying, search for `kueue_pending_workloads` in Grafana's Explore view to verify that metrics are displayed.

### 15.2 Enabling ClusterQueue Resource Metrics

Enable ClusterQueue resource metrics to display CPU/GPU usage gauges in the Grafana dashboard. This setting exposes `kueue_cluster_queue_resource_usage` / `kueue_cluster_queue_nominal_quota` metrics to Prometheus.

```bash
kubectl edit configmap kueue-manager-config -n kueue-system
```

Add `enableClusterQueueResources: true` to the `metrics` section of `controller_manager_config.yaml`:

```yaml
metrics:
  enableClusterQueueResources: true
```

After the change, restart kueue-controller-manager:

```bash
kubectl rollout restart deployment kueue-controller-manager -n kueue-system
```

Verify enablement by searching for `kueue_cluster_queue_resource_usage` in Grafana's Explore view and confirming that metrics are displayed.

### 15.3 Creating Kueue Resources

```bash
kubectl apply -f kueue/resource-flavor.yaml
kubectl apply -f kueue/cluster-queue.yaml
```

---

## 16. Preparing Compute Nodes

Apply the common key `cjob.io/flavor` label to compute nodes with the flavor name as the value. This label must match the Kueue ResourceFlavor's `nodeLabels` and the `label_selector` in ConfigMap `RESOURCE_FLAVORS`. Using the same key across all flavors allows Kueue to detect cross-flavor conflicts and prevent scheduling to the wrong flavor. The taint value is configured via `JOB_NODE_TAINT` in ConfigMap `cjob-config` (default: `role=computing:NoSchedule`).

**Important:** The ConfigMap `JOB_NODE_TAINT`, Kueue ResourceFlavor `nodeTaints`, and node Taint must all have the same value. Mismatches will prevent Job Pods from being scheduled.

**Operations without taints (shared nodes):** In environments without dedicated nodes, set `JOB_NODE_TAINT` to an empty string, omit `nodeTaints` from the Kueue ResourceFlavor, and do not apply taints to nodes.

### maxPods Adjustment

The kubelet default `maxPods` is 110, and this limit may be reached when launching many Job Pods simultaneously. Adjust the kubelet configuration on each compute node as needed.

1. **Edit the kubelet configuration file** (usually `/var/lib/kubelet/config.yaml`)

   ```yaml
   maxPods: 135
   ```

2. **Restart kubelet**

   ```bash
   sudo systemctl restart kubelet
   ```

3. **Verify the change**

   ```bash
   kubectl get node <node-name> -o jsonpath='{.status.capacity.pods}'
   ```

**Notes:**

- Even when attempting to allocate all CPU cores on a node to Job Pods, the actual usable core count is reduced by the CPU requests of DaemonSet Pods (calico-node, kube-proxy, etc.). It is recommended to reserve approximately 2-4 cores for system use for stable operation.
- Verify that the Pod CIDR size covers maxPods or more (`/24` = 256 IPs is sufficient).
- In kubeadm environments, local settings may be overwritten during `kubeadm upgrade`, so also set `maxPods` in the `kubelet-config` ConfigMap in `kube-system`. However, ConfigMap changes affect all nodes. To apply only to specific compute nodes, do not modify the ConfigMap; instead, edit only the local configuration on target nodes and reconfigure after each `kubeadm upgrade`.

### 16.1 CPU Nodes

```bash
# Apply label and taint to CPU compute nodes
kubectl label node <node-name> cjob.io/flavor=cpu
kubectl taint node <node-name> role=computing:NoSchedule

# Verify
kubectl get nodes -l cjob.io/flavor=cpu
```

### 16.2 GPU Nodes

GPU nodes use the same key `cjob.io/flavor` as CPU nodes, with the GPU flavor name as the value. Taints use the same value as CPU nodes.

```bash
# Apply label and taint to GPU nodes
kubectl label node <gpu-node-name> cjob.io/flavor=gpu
kubectl taint node <gpu-node-name> role=computing:NoSchedule

# Verify
kubectl get nodes -l cjob.io/flavor=gpu
```

Node routing is controlled by the label value of the common key `cjob.io/flavor`. The Dispatcher sets the flavor's `label_selector` as the `nodeSelector` of the K8s Job, and Kueue schedules it to nodes based on the matching ResourceFlavor's `nodeLabels`.

When compute nodes are added or removed, the Watcher automatically syncs the `node_resources` table, so no configuration changes to the Dispatcher or Submit API are needed.

### 16.3 Adding a New ResourceFlavor

When adding nodes with different CPU architecture or different GPU model, create a new flavor following these steps.

#### 1. Apply Labels and Taints to Nodes

```bash
kubectl label node <node-name> cjob.io/flavor=<flavor-name>    # e.g., cjob.io/flavor=gpu-h100
kubectl taint node <node-name> role=computing:NoSchedule
```

#### 2. Create Kueue ResourceFlavor

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: <flavor-name>         # e.g., gpu-h100 (must match the flavor value in DB)
spec:
  nodeLabels:
    cjob.io/flavor: "<flavor-name>"    # Must match the label applied in step 1
  nodeTaints:
    - key: "role"
      value: "computing"
      effect: "NoSchedule"
  tolerations:
    - key: "role"
      operator: "Equal"
      value: "computing"
      effect: "NoSchedule"
```

#### 3. Add Flavor to ClusterQueue

```bash
kubectl edit clusterqueue cjob-cluster-queue
```

Add a new flavor entry to `spec.resourceGroups[0].flavors`. For flavors without GPU resources, set the `nvidia.com/gpu` nominalQuota to `"0"`. To protect other flavors' resources, set `lendingLimit: "0"`.

#### 4. Add Definition to ConfigMap `RESOURCE_FLAVORS`

```bash
kubectl edit configmap cjob-config -n cjob-system
```

Add the new flavor definition to the `RESOURCE_FLAVORS` JSON array. For flavors with GPU, specify `gpu_resource_name`.

```json
{"name": "gpu-h100", "label_selector": "cjob.io/flavor=gpu-h100", "gpu_resource_name": "nvidia.com/gpu"}
```

#### 5. Restart Components

```bash
kubectl rollout restart deployment submit-api dispatcher watcher -n cjob-system
```

#### 6. Verification

```bash
# Verify node sync (reflected in the next sync cycle)
cjobctl cluster resources

# Verify nominalQuota
cjobctl cluster show-quota

# Verify job submission
cjob add --flavor <flavor-name> -- echo hello
```

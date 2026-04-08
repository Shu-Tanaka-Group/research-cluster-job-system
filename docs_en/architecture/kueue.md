> *This document was auto-translated from the [Japanese original](../../docs/architecture/kueue.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Kueue Design

## 1. ResourceFlavor

### 1.1 Naming Convention

The `metadata.name` of a ResourceFlavor must match the value of the `node_resources.flavor` / `jobs.flavor` columns in the DB and the `name` field of the ConfigMap `RESOURCE_FLAVORS` setting. This eliminates the need for name conversion between the Kueue API and DB queries.

### 1.2 ResourceFlavor Definition

Each ResourceFlavor identifies the target node group using node label selectors. To add a flavor, follow these steps:

1. Assign a label with the common key `cjob.io/flavor` to the target nodes (e.g., `cjob.io/flavor=gpu-a100`)
2. Create the Kueue ResourceFlavor object
3. Add it to the `resourceGroups[0].flavors` list in ClusterQueue
4. Add the flavor definition to the ConfigMap `RESOURCE_FLAVORS`

**ResourceFlavor template:**

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: <flavor-name>        # Must match the flavor value in DB
spec:
  nodeLabels:
    cjob.io/flavor: "<flavor-name>"    # Label to identify target nodes (value must match flavor name)
  nodeTaints:               # Only when using taints (must match JOB_NODE_TAINT)
    - key: "role"
      value: "computing"
      effect: "NoSchedule"
  tolerations:
    - key: "role"
      operator: "Equal"
      value: "computing"
      effect: "NoSchedule"
```

**Note:** The values of `nodeTaints` and `tolerations` must match the ConfigMap `cjob-config` setting `JOB_NODE_TAINT` (default: `role=computing:NoSchedule`) and the taints assigned to nodes. If these three locations are inconsistent, Job Pods will not be scheduled. If `JOB_NODE_TAINT` is set to an empty string, omit `nodeTaints` and `tolerations` from the ResourceFlavor.

### 1.3 Configuration Examples

#### ResourceFlavor for CPU Nodes

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: cpu
spec:
  nodeLabels:
    cjob.io/flavor: "cpu"
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

#### ResourceFlavor for GPU Nodes (e.g., A100)

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: gpu-a100
spec:
  nodeLabels:
    cjob.io/flavor: "gpu-a100"
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

GPU nodes are assigned the same taint as CPU nodes (`role=computing:NoSchedule`). No additional taints are needed. The toleration configuration on the Dispatcher side does not need to be changed; node assignment is handled by the Kueue ResourceFlavor `nodeLabels`. Since all flavors use the common key `cjob.io/flavor`, Kueue can detect conflicts where the same key has different values and prevent cross-flavor admission.

## 2. ClusterQueue

All ResourceFlavors are placed in a single ClusterQueue. Jobs from all flavors are managed in the same queue, and Kueue automatically assigns nodes based on the ResourceFlavor `nodeLabels`.

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: cjob-cluster-queue
spec:
  cohortName: cjob-cohort
  namespaceSelector: {}
  resourceGroups:
    - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
      flavors:
        - name: cpu
          resources:
            - name: cpu
              nominalQuota: "256"
            - name: memory
              nominalQuota: "1000Gi"
            - name: nvidia.com/gpu
              nominalQuota: "0"
        - name: gpu-a100
          resources:
            - name: cpu
              nominalQuota: "64"
              lendingLimit: "0"
            - name: memory
              nominalQuota: "500Gi"
              lendingLimit: "0"
            - name: nvidia.com/gpu
              nominalQuota: "4"
              lendingLimit: "0"
  queueingStrategy: BestEffortFIFO
  preemption:
    withinClusterQueue: Never   # Prohibits forceful termination of running jobs
```

Since Kueue does not allow the same resource name to appear in multiple `resourceGroups`, cpu / memory / nvidia.com/gpu are consolidated into a single `resourceGroups` with all flavors placed there. Flavors without GPUs set the `nominalQuota` for `nvidia.com/gpu` to `"0"`.

### Adding a Flavor

To add a new flavor, add a new entry to the `resourceGroups[0].flavors` list. Each flavor must declare the `nominalQuota` for all resources listed in `coveredResources` (set to `"0"` for resources the flavor does not have).

```yaml
# Example: Adding a flavor for H100 GPU nodes
- name: gpu-h100
  resources:
    - name: cpu
      nominalQuota: "128"
      lendingLimit: "0"
    - name: memory
      nominalQuota: "1000Gi"
      lendingLimit: "0"
    - name: nvidia.com/gpu
      nominalQuota: "8"
      lendingLimit: "0"
```

**For different GPU vendors:** If using GPUs with a different resource name such as AMD GPU (`amd.com/gpu`), that resource name must be added to `coveredResources`. In this case, all existing flavors must also have the `nominalQuota` (`"0"`) for that resource added.

### Resource Protection via lendingLimit

Set `lendingLimit: "0"` on all resources of GPU flavors. This prevents CPU jobs from borrowing cpu / memory from GPU flavor quotas under `BestEffortFIFO`, ensuring GPU jobs can always be admitted. Without `lendingLimit`, CPU jobs that exceed the CPU flavor's `nominalQuota` can consume GPU flavor quota, making it impossible for GPU jobs to be admitted.

`lendingLimit` can only be used in ClusterQueues that belong to a cohort, so `cohortName: cjob-cohort` is set. Even if there are no other ClusterQueues in the cohort, `lendingLimit` functions effectively.

### Design Decisions

The `nominalQuota` for each flavor is set to match the allocatable resources of nodes belonging to that flavor. It can be updated with `cjobctl cluster set-quota`.

Reason for adopting `BestEffortFIFO`: When there are available resources, other users' idle quota can be utilized (one user can use all cores), and `StrictFIFO` could cause one user's large number of submissions to stall the entire system. Resource sharing between users within a single ClusterQueue is handled by this `queueingStrategy`. `cohortName` is set to enable `lendingLimit`; it is not intended for resource sharing within the cohort.

Reason for prohibiting preemption: In research computing, forceful termination of jobs mid-run often results in lost results.

With the above configuration: `BestEffortFIFO` allows other users to utilize idle resources. `preemption.withinClusterQueue: Never` prevents running jobs from being forcefully terminated. Jobs match the ResourceFlavor for the user-specified flavor, and Kueue automatically assigns nodes based on `nodeLabels`. GPU flavor `lendingLimit: "0"` reserves GPU node resources exclusively for GPU jobs.

## 3. LocalQueue

Created in each user namespace.

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: default
  namespace: user-alice
spec:
  clusterQueue: cjob-cluster-queue
```

For ResourceQuota and resource limit configuration, see [resources.md](resources.md).

## 4. Kubernetes Job Template

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  namespace: user-alice
  name: cjob-alice-1    # Format: cjob-<username>-<job_id>
  labels:
    kueue.x-k8s.io/queue-name: default   # Dispatcher dynamically sets the value of KUEUE_LOCAL_QUEUE_NAME
    cjob.io/job-id: "1"          # job_id (dynamically set by Dispatcher)
    cjob.io/namespace: user-alice  # namespace (dynamically set by Dispatcher)
spec:
  activeDeadlineSeconds: 86400      # Sets DB's time_limit_seconds as-is (dynamically set by Dispatcher)
  ttlSecondsAfterFinished: 300      # Deletes Job / Pod 5 minutes after completion
  template:
    spec:
      restartPolicy: Never
      nodeSelector:                           # Dynamically set by Dispatcher from RESOURCE_FLAVORS label_selector
        cjob.io/flavor: "cpu"
      tolerations:                            # Dynamically generated by Dispatcher from JOB_NODE_TAINT value (omitted if empty)
        - key: "role"
          operator: "Equal"
          value: "computing"
          effect: "NoSchedule"
      containers:
        - name: worker
          image: your-registry/cjob-jupyter:2.1.0   # Dispatcher dynamically sets image retrieved from DB
          workingDir: /home/jovyan/project-a/exp1
          command: ["/bin/bash", "-lc"]
          args:
            - |
              # Regular job: LOG_DIR=/home/jovyan/.cjob/logs/<job_id>
              # Sweep job: LOG_DIR=/home/jovyan/.cjob/logs/<job_id>/$CJOB_INDEX
              LOG_DIR=/home/jovyan/.cjob/logs/1
              mkdir -p "${LOG_DIR}"
              exec > >(tee "${LOG_DIR}/stdout.log") \
                   2> >(tee "${LOG_DIR}/stderr.log" >&2)
              python main.py --alpha 0.1 --beta 16
              EXIT_CODE=$?
              exec >&- 2>&-
              wait
              exit $EXIT_CODE
          env:
            - name: PYTHONUNBUFFERED
              value: "1"
            - name: OMP_NUM_THREADS
              value: "4"
            - name: PYTHONPATH
              value: "/home/jovyan/project-a"
            - name: VIRTUAL_ENV
              value: "/home/jovyan/myenv"
            - name: PATH
              value: "/home/jovyan/myenv/bin:/usr/local/bin:/usr/bin"
          volumeMounts:
            - name: workspace
              mountPath: /home/jovyan
          resources:
            requests:
              cpu: "2"
              memory: "4Gi"
              # nvidia.com/gpu: "1"   # Only added dynamically by Dispatcher for GPU jobs
            limits:
              cpu: "2"
              memory: "4Gi"
              # nvidia.com/gpu: "1"   # Only added dynamically by Dispatcher for GPU jobs
      volumes:
        - name: workspace
          persistentVolumeClaim:
            claimName: alice   # Dispatcher dynamically fills in the user retrieved from DB
```

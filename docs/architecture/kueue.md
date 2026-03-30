# Kueue 設計

## 1. ResourceFlavor

### 1.1 CPU ノード用 ResourceFlavor

CPU ジョブ専用ノード（`cluster-job=true`）を対象とする。

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: cpu-flavor
spec:
  nodeLabels:
    cluster-job: "true"
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

**注意:** `nodeTaints` と `tolerations` の値は、ConfigMap `cjob-config` の `JOB_NODE_TAINT`（デフォルト: `role=computing:NoSchedule`）およびノードに付与する Taint と一致している必要がある。3 箇所が不一致の場合、Job Pod がスケジュールされない。

**Taint を使わない場合:** `JOB_NODE_TAINT` を空文字列に設定した場合は、ResourceFlavor の `nodeTaints` と `tolerations` を省略する。

```yaml
# Taint を使わない場合の ResourceFlavor
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: cpu-flavor
spec:
  nodeLabels:
    cluster-job: "true"
```

### 1.2 GPU ノード用 ResourceFlavor

GPU ジョブ用ノード（`cluster-gpu-job=true`）を対象とする。CPU ノードとは異なるラベルで識別し、Kueue が `nodeLabels` に基づいて GPU ジョブを GPU ノードに自動的に振り分ける。

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: gpu-flavor
spec:
  nodeLabels:
    cluster-gpu-job: "true"
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

GPU ノードには CPU ノードと同じ taint（`role=computing:NoSchedule`）を付与する。追加の taint は不要。Dispatcher 側の toleration 設定も変更不要であり、ノードの振り分けは Kueue の ResourceFlavor `nodeLabels` が担う。

## 2. ClusterQueue

単一の ClusterQueue に CPU 用と GPU 用の 2 つの ResourceFlavor を配置する。GPU ジョブと CPU ジョブは同じキューで管理され、Kueue が ResourceFlavor の `nodeLabels` に基づいてノードを自動的に振り分ける。

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
        - name: cpu-flavor
          resources:
            - name: cpu
              nominalQuota: "256"
            - name: memory
              nominalQuota: "1000Gi"
            - name: nvidia.com/gpu
              nominalQuota: "0"
        - name: gpu-flavor
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
    withinClusterQueue: Never   # 実行中ジョブの強制終了を禁止
```

Kueue は同じリソース名を複数の `resourceGroups` に含めることを許可しないため、cpu / memory / nvidia.com/gpu を 1 つの `resourceGroups` にまとめ、cpu-flavor と gpu-flavor の 2 つの flavor を配置する。cpu-flavor の `nvidia.com/gpu` を `"0"` にすることで、GPU を要求しないジョブは cpu-flavor にマッチし、GPU を要求するジョブは gpu-flavor にマッチする。

### lendingLimit による GPU リソースの保護

gpu-flavor の全リソースに `lendingLimit: "0"` を設定する。これにより、CPU ジョブが `BestEffortFIFO` の下で gpu-flavor の cpu / memory を借用することを禁止し、GPU ジョブが常に admit 可能な状態を維持する。`lendingLimit` を設定しない場合、cpu-flavor の nominalQuota を超える CPU ジョブが gpu-flavor の quota を消費し、GPU ジョブが admit できなくなる。

`lendingLimit` は cohort に所属する ClusterQueue でのみ使用可能であるため、`cohortName: cjob-cohort` を設定する。cohort 内に他の ClusterQueue がなくても `lendingLimit` は有効に機能する。将来 GPU の種類が増えた場合（例: A100, H100）は、ResourceFlavor の追加と ClusterQueue の flavors リストへの追加のみで対応でき、LocalQueue や Dispatcher の変更は不要である。

### 設計判断

GPU 用の `nominalQuota`（cpu / memory / nvidia.com/gpu）は GPU ノードの allocatable に合わせて設定する。`cjobctl cluster set-quota` で更新できる。

`BestEffortFIFO` を採用する理由：空きリソースがあれば他ユーザーの idle quota を利用できる（1ユーザーが全コアを使える）ため、かつ `StrictFIFO` では1ユーザーの大量投入が全体を止める可能性があるため。単一 ClusterQueue 内でのユーザー間リソース共有はこの `queueingStrategy` が担う。`cohortName` は `lendingLimit` の有効化のために設定しており、cohort 内のリソース共有が目的ではない。

preemption を禁止する理由：研究計算ではジョブが途中で強制終了されると結果が失われるケースが多いため。

以上の設定により：`BestEffortFIFO` により空きリソースは他ユーザーが利用できる。`preemption.withinClusterQueue: Never` により実行中のジョブは強制終了されない。GPU リソースを要求しない CPU ジョブは `cpu-flavor` にマッチし、`nvidia.com/gpu` を要求する GPU ジョブは `gpu-flavor` にマッチする。gpu-flavor の `lendingLimit: "0"` により GPU ノードのリソースは GPU ジョブ専用に確保される。

## 3. LocalQueue

各 user namespace に作成する。

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: default
  namespace: user-alice
spec:
  clusterQueue: cjob-cluster-queue
```

ResourceQuota およびリソース制限の設定については [resources.md](resources.md) を参照。

## 4. Kubernetes Job テンプレート

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  namespace: user-alice
  name: cjob-alice-1    # cjob-<username>-<job_id> 形式
  labels:
    kueue.x-k8s.io/queue-name: default   # Dispatcher が KUEUE_LOCAL_QUEUE_NAME の値を動的に設定
    cjob.io/job-id: "1"          # job_id（Dispatcher が動的に設定）
    cjob.io/namespace: user-alice  # namespace（Dispatcher が動的に設定）
spec:
  activeDeadlineSeconds: 86400      # DB の time_limit_seconds をそのまま設定（Dispatcher が動的に設定）
  ttlSecondsAfterFinished: 300      # 完了後 5分で Job / Pod を削除
  template:
    spec:
      restartPolicy: Never
      tolerations:                            # JOB_NODE_TAINT の値から Dispatcher が動的に生成（空の場合は省略）
        - key: "role"
          operator: "Equal"
          value: "computing"
          effect: "NoSchedule"
      containers:
        - name: worker
          image: yusekiya/stg-jupyter:2.1.0   # Dispatcher が DB から取得した image を動的に設定
          workingDir: /home/jovyan/project-a/exp1
          command: ["/bin/bash", "-lc"]
          args:
            - |
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
              # nvidia.com/gpu: "1"   # GPU ジョブの場合のみ Dispatcher が動的に追加
            limits:
              cpu: "2"
              memory: "4Gi"
              # nvidia.com/gpu: "1"   # GPU ジョブの場合のみ Dispatcher が動的に追加
      volumes:
        - name: workspace
          persistentVolumeClaim:
            claimName: alice   # Dispatcher が DB から取得した user を動的に埋め込む
```

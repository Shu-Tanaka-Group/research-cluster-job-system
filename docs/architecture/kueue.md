# Kueue 設計

## 1. ResourceFlavor

ジョブキューシステム専用ノード（`cluster-job=true`）を対象とする。

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: cluster-job-flavor
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

## 2. ClusterQueue

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: cjob-cluster-queue
spec:
  namespaceSelector: {}
  resourceGroups:
    - coveredResources: ["cpu", "memory"]
      flavors:
        - name: cluster-job-flavor
          resources:
            - name: cpu
              nominalQuota: "256"
            - name: memory
              nominalQuota: "1000Gi"
  queueingStrategy: BestEffortFIFO
  preemption:
    withinClusterQueue: Never   # 実行中ジョブの強制終了を禁止
```

`BestEffortFIFO` を採用する理由：空きリソースがあれば他ユーザーの idle quota を利用できる（1ユーザーが全コアを使える）ため、かつ `StrictFIFO` では1ユーザーの大量投入が全体を止める可能性があるため。単一 ClusterQueue 内でのユーザー間リソース共有は `cohort` ではなくこの `queueingStrategy` が担う。

`cohort` を設定しない理由：`cohort` は複数 ClusterQueue 間のリソース共有に使う仕組みであり、本設計の単一 ClusterQueue 構成では意味を持たないため削除する。将来 GPU 専用キューなど複数 ClusterQueue 構成に拡張する際に追加すること。

preemption を禁止する理由：研究計算ではジョブが途中で強制終了されると結果が失われるケースが多いため。

以上の設定により：`BestEffortFIFO` により空きリソースは他ユーザーが利用できる。`preemption.withinClusterQueue: Never` により実行中のジョブは強制終了されない。

## 3. LocalQueue

各 user namespace に作成する。

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
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
  ttlSecondsAfterFinished: 10800    # 完了後 3時間で Job / Pod を削除
  template:
    spec:
      restartPolicy: Never
      tolerations:
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
            limits:
              cpu: "2"
              memory: "4Gi"
      volumes:
        - name: workspace
          persistentVolumeClaim:
            claimName: alice   # Dispatcher が DB から取得した user を動的に埋め込む
```

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

## 4. ResourceQuota

各 user namespace に作成する安全網。

空きリソースがあれば1ユーザーが全コアを使える方針とし、Kueue に公平性の調整を委ねる。
ResourceQuota はリソースを均等分配するためではなく、バグ等による意図しない無制限消費を防ぐための安全網として機能する。

設定根拠：
- CPU / memory：クラスタ総量より少し大きめに設定し、Kueue の admission 制御に任せる。Job Pod（最大 dispatch_limit 分）に加えてユーザーが使用している他の計算リソース（ジョブ投入用Podやデータ解析用Podなど）の分も余裕として含める
- Job 数：dispatch_limit(256) と `ttlSecondsAfterFinished`(10800秒=3時間) を考慮して設定する。SUCCEEDED/FAILED の K8s Job は Watcher が明示的に削除せず TTL 経過まで残るため、実行中ジョブ(最大256) と TTL ウィンドウ内の完了済みジョブの合計が ResourceQuota を超えないよう余裕を持たせて dispatch_limit の2倍以上に設定 → 600

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: computing-quota
  namespace: user-alice
spec:
  hard:
    count/jobs.batch: "600"
    requests.cpu: "300"
    requests.memory: "1250Gi"
    limits.cpu: "300"
    limits.memory: "1250Gi"
```

## 5. リソース制限まとめ

### ジョブ数に関する制限

| 制限 | 設定箇所 | 値 | 管理主体 | 適用単位 | 制限対象 |
|---|---|---|---|---|---|
| `MAX_QUEUED_JOBS_PER_NAMESPACE` | ConfigMap | 2000 | Submit API | ユーザーごと | PostgreSQL の `jobs` テーブルへの登録数（QUEUED / DISPATCHING / DISPATCHED / RUNNING / CANCELLED の合計） |
| `DISPATCH_BUDGET_PER_NAMESPACE` | ConfigMap | 256 | Dispatcher | ユーザーごと | DB 上の active ジョブ数（DISPATCHING + DISPATCHED + RUNNING の合計）。上限に達すると Dispatcher が新規 dispatch を停止する |
| `count/jobs.batch` | ResourceQuota | 600 | Kubernetes | ユーザーごと | K8s 上に存在する `batch/v1 Job` オブジェクトの総数。実行中 + TTL 待ち完了済みジョブの合計が対象 |

3つの制限は独立したレイヤーで機能する。

```
cjob add → DB 登録（MAX_QUEUED_JOBS_PER_NAMESPACE: 2000件上限）
              ↓
Dispatcher がスキャン → dispatch_budget チェック（DISPATCH_BUDGET_PER_NAMESPACE: 256件上限）
              ↓
K8s Job を作成 → count/jobs.batch チェック（600件上限）
```

`count/jobs.batch` を 600 に設定する理由：dispatch_budget の上限（256件）で動作していても、SUCCEEDED/FAILED になった K8s Job が TTL（3時間）が切れるまで K8s 上に残り続けるため、実行中 + TTL 待ち完了済みジョブの合計を吸収できるよう dispatch_limit の2倍以上に設定している。

### CPU・メモリに関する制限

| 制限 | 設定箇所 | 値 | 管理主体 | 適用単位 | 制限対象 |
|---|---|---|---|---|---|
| ResourceQuota `requests.cpu` / `limits.cpu` | ResourceQuota（user namespace） | 300 | Kubernetes | ユーザーごと | namespace 内の全 Pod（Job Pod + User Pod）が要求・使用できる CPU の合計上限 |
| ResourceQuota `requests.memory` / `limits.memory` | ResourceQuota（user namespace） | 1250Gi | Kubernetes | ユーザーごと | namespace 内の全 Pod が要求・使用できるメモリの合計上限 |
| ClusterQueue `nominalQuota` CPU | ClusterQueue | 256 | Kueue | クラスタ全体 | Kueue が Job Pod に割り当てるクラスタ全体の CPU 上限。ユーザー間で共有される |
| ClusterQueue `nominalQuota` memory | ClusterQueue | 1000Gi | Kueue | クラスタ全体 | Kueue が Job Pod に割り当てるクラスタ全体のメモリ上限。ユーザー間で共有される |

ResourceQuota と ClusterQueue nominalQuota の違い：ResourceQuota は User Pod を含む namespace 内の全 Pod を対象とした上限（バグ等による無制限消費を防ぐ安全網）。ClusterQueue nominalQuota は Kueue が Job Pod の admission を判断するための上限であり、実際の実行スケジューリングを制御する。User Pod は Kueue を経由しないため ClusterQueue の制御対象外である。

## 6. Kubernetes Job テンプレート

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

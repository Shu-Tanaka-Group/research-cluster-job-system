# CJob Deployment 設計書

## 1. 概要

本設計書は、CJob システムの Kubernetes 上への配置・構成管理に関する設計をまとめたものである。

---

## 2. namespace 構成

```
cjob-system      : システムコンポーネント全体（Submit API / Dispatcher / Watcher / RabbitMQ / PostgreSQL）
user-<username>    : ユーザーごとの実行環境（User Pod / Job Pod / LocalQueue / ResourceQuota / PVC）
```

---

## 3. コンポーネント配置

| コンポーネント | 種類 | Replica | namespace |
|---|---|---|---|
| Submit API | Deployment | 1 | cjob-system |
| Dispatcher | Deployment | 1 | cjob-system |
| Watcher / Reconciler | Deployment | 1 | cjob-system |
| RabbitMQ | StatefulSet | 1 | cjob-system |
| PostgreSQL | StatefulSet | 1 | cjob-system |

Dispatcher・Watcher を Replica 1 に固定する理由：Replica 複数にすると二重 dispatch・二重 DB 更新が発生するため。

---

## 4. PVC 構成

RabbitMQ・PostgreSQL はそれぞれ PVC を持つ。StorageClass は NFS subdir external provisioner を使用する。

| PVC 名 | 対象 | 用途 |
|---|---|---|
| `postgres-data` | PostgreSQL | DB ファイルの永続化 |
| `rabbitmq-data` | RabbitMQ | キューの永続化データ |

---

## 5. Secret 設計

全て `cjob-system` namespace に作成する。

### 5.1 `postgres-secret`

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: postgres-secret
  namespace: cjob-system
type: Opaque
stringData:
  POSTGRES_USER: cjob
  POSTGRES_PASSWORD: <password>
  POSTGRES_DB: cjob
```

### 5.2 `rabbitmq-secret`

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: rabbitmq-secret
  namespace: cjob-system
type: Opaque
stringData:
  RABBITMQ_DEFAULT_USER: cjob
  RABBITMQ_DEFAULT_PASS: <password>
```

---

## 6. ConfigMap 設計

### 6.1 `cjob-config`（共通設定）

Submit API・Dispatcher・Watcher が共通で参照する。

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: cjob-config
  namespace: cjob-system
data:
  POSTGRES_HOST: postgres.cjob-system.svc.cluster.local
  POSTGRES_PORT: "5432"
  POSTGRES_DB: cjob
  RABBITMQ_HOST: rabbitmq.cjob-system.svc.cluster.local
  RABBITMQ_PORT: "5672"
  RABBITMQ_VHOST: "/"
  RABBITMQ_EXCHANGE: cjob
  RABBITMQ_RETRY_EXCHANGE: cjob.retry
  RABBITMQ_QUEUE: cjob.submit
  RABBITMQ_RETRY_QUEUE: cjob.retry
  RABBITMQ_ROUTING_KEY: submit
  RABBITMQ_RETRY_ROUTING_KEY: retry
  RABBITMQ_RETRY_TTL_MS: "30000"
  RABBITMQ_MAX_RETRIES: "5"
  SUBMIT_API_HOST: submit-api.cjob-system.svc.cluster.local
  SUBMIT_API_PORT: "8080"
  DISPATCH_BUDGET_PER_NAMESPACE: "256"
  DISPATCH_BUDGET_CHECK_INTERVAL_SEC: "10"
  MAX_QUEUED_JOBS_PER_NAMESPACE: "2000"
  KUEUE_LOCAL_QUEUE_NAME: default
  JOB_NAMESPACE_PREFIX: user-
  WORKSPACE_MOUNT_PATH: /home/jovyan
  LOG_BASE_DIR: /home/jovyan/.cjob/logs
```

### 6.2 各コンポーネントへの注入パターン

全コンポーネントで共通のパターンを使う。

```yaml
# Deployment の env セクション例
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
  - name: RABBITMQ_HOST
    valueFrom:
      configMapKeyRef:
        name: cjob-config
        key: RABBITMQ_HOST
  - name: RABBITMQ_PASS
    valueFrom:
      secretKeyRef:
        name: rabbitmq-secret
        key: RABBITMQ_DEFAULT_PASS
```

### 6.3 各コンポーネントが参照するリソース

| コンポーネント | ConfigMap | Secret |
|---|---|---|
| Submit API | `cjob-config` | `postgres-secret` `rabbitmq-secret` |
| Dispatcher | `cjob-config` | `postgres-secret` `rabbitmq-secret` |
| Watcher | `cjob-config` | `postgres-secret` |
| PostgreSQL | - | `postgres-secret` |
| RabbitMQ | - | `rabbitmq-secret` |
| CLI（GitHub Releases で配布） | - | - |

---

## 7. Fixed Image 設計

### 7.1 image の役割

同一の fixed image が2つの用途で使われる。`cjob` CLI は image には含めず、ユーザーが各自でインストールする。

| 用途 | Pod | 備考 |
|---|---|---|
| ユーザー作業環境 | User Pod（JupyterHub） | ユーザーが cjob CLI を別途インストール |
| ジョブ実行環境 | Job Pod（Kubernetes Job） | CLI は不要 |

### 7.2 image の内容

| カテゴリ | パッケージ / 設定 | 理由 |
|---|---|---|
| ベース OS | Ubuntu 24.04 | 安定性・パッケージの豊富さ |
| Python | python3.12 python3.12-venv python3-pip | 仮想環境のベース |
| ビルドツール | gcc g++ make | C 拡張ライブラリのビルド |
| 科学計算系ライブラリ | libopenblas-dev liblapack-dev | numpy 等の依存 |
| HPC 系ツール | openmpi-bin | MPI ジョブへの対応 |
| 基本ツール | git curl wget vim | 作業用 |

含めないもの：`cjob` CLI（GitHub Releases で個別配布）・ユーザーの Python パッケージ（各自が `/home/jovyan` 配下で venv を管理）・CUDA / GPU ドライバ（初期スコープ外）・Jupyter 本体（JupyterHub 側が管理）。

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

### 7.4 cjob CLI の配布

`cjob` CLI は Rust 製シングルバイナリとして GitHub Releases で配布する。
ユーザーは User Pod 内で以下のようにインストールする。

```bash
# GitHub Releases から最新バイナリをダウンロードして配置する例
mkdir -p /home/jovyan/.local/bin
curl -L https://github.com/<org>/cjob/releases/latest/download/cjob-x86_64-unknown-linux-gnu \
  -o /home/jovyan/.local/bin/cjob
chmod +x /home/jovyan/.local/bin/cjob
```

Submit API のエンドポイントは環境変数 `CJOB_API_URL` で設定する。
未設定時はデフォルト値（`http://submit-api.cjob-system.svc.cluster.local:8080`）を使用する。

---

## 8. Submit API の ServiceAccount と RBAC

Submit API が TokenReview API を呼ぶために必要な RBAC リソース。

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: submit-api-sa
  namespace: cjob-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: token-reviewer
rules:
  - apiGroups: ["authentication.k8s.io"]
    resources: ["tokenreviews"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: submit-api-token-reviewer
subjects:
  - kind: ServiceAccount
    name: submit-api-sa
    namespace: cjob-system
roleRef:
  kind: ClusterRole
  name: token-reviewer
  apiGroup: rbac.authorization.k8s.io
```

---

## 9. Dispatcher / Watcher の ServiceAccount と RBAC

Dispatcher と Watcher が Kubernetes Job / Pod を操作するために必要な RBAC リソース。
Watcher は Dispatcher と同一の権限を必要とするため、`dispatcher-sa` を共用する。
Watcher の Deployment には `serviceAccountName: dispatcher-sa` を指定する。

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: dispatcher-sa
  namespace: cjob-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cjob-job-controller
rules:
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["create", "get", "list", "watch", "delete"]
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: cjob-job-controller-binding
subjects:
  - kind: ServiceAccount
    name: dispatcher-sa
    namespace: cjob-system
roleRef:
  kind: ClusterRole
  name: cjob-job-controller
  apiGroup: rbac.authorization.k8s.io
```

---

## 10. NetworkPolicy

前提：このクラスタには default-deny NetworkPolicy は存在しない。
`cjob-system` namespace 内の Pod 間通信（Submit API ↔ PostgreSQL / RabbitMQ など）は制限しない。
User namespace 以外からの Submit API へのアクセスのみを NetworkPolicy で制限する。

User namespace から Submit API への通信のみを許可する。

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-submit-api
  namespace: cjob-system
spec:
  podSelector:
    matchLabels:
      app: submit-api
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              cjob.io/user-namespace: "true"
      ports:
        - protocol: TCP
          port: 8080
```

---

## 11. namespace 作成スクリプト（完成版）

新規ユーザーの namespace を作成する際に実行するスクリプト。

```bash
#!/bin/bash
set -euo pipefail

USERNAME=$1

if [ -z "${USERNAME}" ]; then
  echo "Usage: $0 <username>"
  exit 1
fi

echo "Creating namespace and resources for user: ${USERNAME}"

# namespace 作成
kubectl create namespace user-${USERNAME}

# NetworkPolicy 用ラベルを付与
kubectl label namespace user-${USERNAME} cjob.io/user-namespace=true

# User Pod 用 ServiceAccount 作成
kubectl create serviceaccount cjob-user -n user-${USERNAME}

# JupyterHub KubeSpawner 設定（config.yaml）
# service_account: cjob-user を設定済みであること

# Kueue LocalQueue 作成
kubectl apply -f - <<EOF
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  name: default
  namespace: user-${USERNAME}
spec:
  clusterQueue: cjob-cluster-queue
EOF

# ResourceQuota 作成
kubectl apply -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: cjob-quota
  namespace: user-${USERNAME}
spec:
  hard:
    count/jobs.batch: "300"
    requests.cpu: "256"
    requests.memory: "1000Gi"
    limits.cpu: "256"
    limits.memory: "1000Gi"
EOF

echo "Done: user-${USERNAME}"
```

---

## 12. JupyterHub 設定

User Pod に `cjob-user` ServiceAccount を付与するための KubeSpawner 設定。

```yaml
# JupyterHub config.yaml
hub:
  config:
    KubeSpawner:
      service_account: cjob-user
```

### `JUPYTER_IMAGE` 環境変数について

`cjob` CLI は Job Pod に使用する image 名を User Pod の環境変数 `JUPYTER_IMAGE` から取得する。
この環境変数は既存の JupyterHub 環境においてすでに設定済みであり、User Pod 起動時に
現在のコンテナイメージ名（例: `yusekiya/stg-jupyter:2.1.0`）が自動的に注入される。
追加の設定変更は不要である。

---

## 13. Deployment / StatefulSet YAML

### 13.1 PostgreSQL ConfigMap（スキーマ定義）

スキーマ SQL を ConfigMap に定義する。PostgreSQL 公式 image は初回起動時に
`/docker-entrypoint-initdb.d/` 内の `.sql` ファイルを自動実行する。
`IF NOT EXISTS` を使用しているため再デプロイ時も安全に再実行できる。

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: postgres-schema
  namespace: cjob-system
data:
  schema.sql: |
    CREATE TABLE IF NOT EXISTS jobs (
        job_id        INTEGER NOT NULL,
        "user"        TEXT NOT NULL,
        namespace     TEXT NOT NULL,
        command       TEXT NOT NULL,
        cwd           TEXT NOT NULL,
        env_json      JSONB NOT NULL DEFAULT '{}',
        cpu           TEXT NOT NULL,
        memory        TEXT NOT NULL,
        gpu           INTEGER NOT NULL DEFAULT 0,
        status        TEXT NOT NULL,
        retry_count   INTEGER NOT NULL DEFAULT 0,
        k8s_job_name  TEXT,
        log_dir       TEXT,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        dispatched_at TIMESTAMPTZ,
        finished_at   TIMESTAMPTZ,
        last_error    TEXT,
        PRIMARY KEY (namespace, job_id)
    );
    CREATE TABLE IF NOT EXISTS user_job_counters (
        namespace   TEXT PRIMARY KEY,
        next_id     INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS job_events (
        id           BIGSERIAL PRIMARY KEY,
        namespace    TEXT NOT NULL,
        job_id       INTEGER NOT NULL,
        event_type   TEXT NOT NULL,
        payload_json JSONB NOT NULL DEFAULT '{}',
        created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        FOREIGN KEY (namespace, job_id) REFERENCES jobs(namespace, job_id)
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_k8s_job_name ON jobs (k8s_job_name);
    CREATE INDEX IF NOT EXISTS idx_jobs_namespace_status ON jobs (namespace, status);
```

### 13.2 PostgreSQL StatefulSet

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
  namespace: cjob-system
spec:
  serviceName: postgres
  replicas: 1
  selector:
    matchLabels:
      app: postgres
  template:
    metadata:
      labels:
        app: postgres
    spec:
      containers:
        - name: postgres
          image: postgres:16
          env:
            - name: POSTGRES_USER
              valueFrom:
                secretKeyRef:
                  name: postgres-secret
                  key: POSTGRES_USER
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: postgres-secret
                  key: POSTGRES_PASSWORD
            - name: POSTGRES_DB
              valueFrom:
                secretKeyRef:
                  name: postgres-secret
                  key: POSTGRES_DB
          ports:
            - containerPort: 5432
          volumeMounts:
            - name: postgres-data
              mountPath: /var/lib/postgresql/data
            - name: initdb
              mountPath: /docker-entrypoint-initdb.d/
          resources:
            requests:
              cpu: "250m"
              memory: "256Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          livenessProbe:
            exec:
              command: ["pg_isready", "-U", "cjob"]
            initialDelaySeconds: 30
            periodSeconds: 10
      volumes:
        - name: initdb
          configMap:
            name: postgres-schema
  volumeClaimTemplates:
    - metadata:
        name: postgres-data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: managed-nfs-storage
        resources:
          requests:
            storage: 20Gi
---
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: cjob-system
spec:
  selector:
    app: postgres
  ports:
    - port: 5432
      targetPort: 5432
  clusterIP: None   # Headless Service（StatefulSet 用）
```

### 13.3 RabbitMQ StatefulSet

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: rabbitmq
  namespace: cjob-system
spec:
  serviceName: rabbitmq
  replicas: 1
  selector:
    matchLabels:
      app: rabbitmq
  template:
    metadata:
      labels:
        app: rabbitmq
    spec:
      containers:
        - name: rabbitmq
          image: rabbitmq:3.13-management
          env:
            - name: RABBITMQ_DEFAULT_USER
              valueFrom:
                secretKeyRef:
                  name: rabbitmq-secret
                  key: RABBITMQ_DEFAULT_USER
            - name: RABBITMQ_DEFAULT_PASS
              valueFrom:
                secretKeyRef:
                  name: rabbitmq-secret
                  key: RABBITMQ_DEFAULT_PASS
          ports:
            - containerPort: 5672   # AMQP
            - containerPort: 15672  # Management UI
          volumeMounts:
            - name: rabbitmq-data
              mountPath: /var/lib/rabbitmq
          resources:
            requests:
              cpu: "250m"
              memory: "256Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          livenessProbe:
            exec:
              command: ["rabbitmq-diagnostics", "ping"]
            initialDelaySeconds: 30
            periodSeconds: 10
  volumeClaimTemplates:
    - metadata:
        name: rabbitmq-data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: managed-nfs-storage
        resources:
          requests:
            storage: 5Gi
---
apiVersion: v1
kind: Service
metadata:
  name: rabbitmq
  namespace: cjob-system
spec:
  selector:
    app: rabbitmq
  ports:
    - name: amqp
      port: 5672
      targetPort: 5672
    - name: management
      port: 15672
      targetPort: 15672
  clusterIP: None
```

### 13.4 Submit API Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: submit-api
  namespace: cjob-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: submit-api
  template:
    metadata:
      labels:
        app: submit-api
    spec:
      serviceAccountName: submit-api-sa
      containers:
        - name: submit-api
          image: yusekiya/cjob-submit-api:latest
          ports:
            - containerPort: 8080
          env:
            - name: POSTGRES_HOST
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: POSTGRES_HOST
            - name: POSTGRES_PORT
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: POSTGRES_PORT
            - name: POSTGRES_DB
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: POSTGRES_DB
            - name: POSTGRES_USER
              valueFrom:
                secretKeyRef:
                  name: postgres-secret
                  key: POSTGRES_USER
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: postgres-secret
                  key: POSTGRES_PASSWORD
            - name: RABBITMQ_HOST
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: RABBITMQ_HOST
            - name: RABBITMQ_PORT
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: RABBITMQ_PORT
            - name: RABBITMQ_USER
              valueFrom:
                secretKeyRef:
                  name: rabbitmq-secret
                  key: RABBITMQ_DEFAULT_USER
            - name: RABBITMQ_PASS
              valueFrom:
                secretKeyRef:
                  name: rabbitmq-secret
                  key: RABBITMQ_DEFAULT_PASS
            - name: MAX_QUEUED_JOBS_PER_NAMESPACE
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: MAX_QUEUED_JOBS_PER_NAMESPACE
          resources:
            requests:
              cpu: "100m"
              memory: "256Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          livenessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 10
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: submit-api
  namespace: cjob-system
spec:
  selector:
    app: submit-api
  ports:
    - port: 8080
      targetPort: 8080
```

### 13.5 Dispatcher Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dispatcher
  namespace: cjob-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: dispatcher
  template:
    metadata:
      labels:
        app: dispatcher
    spec:
      serviceAccountName: dispatcher-sa
      containers:
        - name: dispatcher
          image: yusekiya/cjob-dispatcher:latest
          env:
            - name: POSTGRES_HOST
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: POSTGRES_HOST
            - name: POSTGRES_PORT
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: POSTGRES_PORT
            - name: POSTGRES_DB
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: POSTGRES_DB
            - name: POSTGRES_USER
              valueFrom:
                secretKeyRef:
                  name: postgres-secret
                  key: POSTGRES_USER
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: postgres-secret
                  key: POSTGRES_PASSWORD
            - name: RABBITMQ_HOST
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: RABBITMQ_HOST
            - name: RABBITMQ_PORT
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: RABBITMQ_PORT
            - name: RABBITMQ_USER
              valueFrom:
                secretKeyRef:
                  name: rabbitmq-secret
                  key: RABBITMQ_DEFAULT_USER
            - name: RABBITMQ_PASS
              valueFrom:
                secretKeyRef:
                  name: rabbitmq-secret
                  key: RABBITMQ_DEFAULT_PASS
            - name: DISPATCH_BUDGET_PER_NAMESPACE
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: DISPATCH_BUDGET_PER_NAMESPACE
          resources:
            requests:
              cpu: "100m"
              memory: "256Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          livenessProbe:
            exec:
              command: ["python", "-c", "import os; os.kill(1, 0)"]
            initialDelaySeconds: 10
            periodSeconds: 30
```

### 13.6 Watcher Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: watcher
  namespace: cjob-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: watcher
  template:
    metadata:
      labels:
        app: watcher
    spec:
      serviceAccountName: dispatcher-sa   # dispatcher-sa を共用
      containers:
        - name: watcher
          image: yusekiya/cjob-watcher:latest
          env:
            - name: POSTGRES_HOST
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: POSTGRES_HOST
            - name: POSTGRES_PORT
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: POSTGRES_PORT
            - name: POSTGRES_DB
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: POSTGRES_DB
            - name: POSTGRES_USER
              valueFrom:
                secretKeyRef:
                  name: postgres-secret
                  key: POSTGRES_USER
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: postgres-secret
                  key: POSTGRES_PASSWORD
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "300m"
              memory: "256Mi"
          livenessProbe:
            exec:
              command: ["python", "-c", "import os; os.kill(1, 0)"]
            initialDelaySeconds: 10
            periodSeconds: 30
```

---

## 14. 初期セットアップ手順

新規クラスタへの初回セットアップ手順。

```bash
# 1. cjob-system namespace の作成
kubectl create namespace cjob-system

# 2. Secret の作成
kubectl apply -f secrets/postgres-secret.yaml
kubectl apply -f secrets/rabbitmq-secret.yaml

# 3. ConfigMap の作成
kubectl apply -f configmaps/cjob-config.yaml
kubectl apply -f configmaps/postgres-schema.yaml

# 4. RBAC の作成
kubectl apply -f rbac/submit-api-sa.yaml
kubectl apply -f rbac/dispatcher-sa.yaml

# 5. Kueue のインストール
kubectl apply -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.16.4/manifests.yaml

# 6. Kueue リソースの作成
kubectl apply -f kueue/resource-flavor.yaml
kubectl apply -f kueue/cluster-queue.yaml

# 7. PostgreSQL のデプロイ
kubectl apply -f deployments/postgres.yaml

# 8. DB スキーマの初期化
# postgres-schema ConfigMap の schema.sql が /docker-entrypoint-initdb.d/ にマウントされ、
# PostgreSQL 初回起動時に自動実行される。
# IF NOT EXISTS を使用しているため再デプロイ時も安全に再実行できる。

# 9. システムコンポーネント image のビルドと push
docker build -t yusekiya/cjob-submit-api:latest -f Dockerfile.submit-api .
docker push yusekiya/cjob-submit-api:latest

docker build -t yusekiya/cjob-dispatcher:latest -f Dockerfile.dispatcher .
docker push yusekiya/cjob-dispatcher:latest

docker build -t yusekiya/cjob-watcher:latest -f Dockerfile.watcher .
docker push yusekiya/cjob-watcher:latest

# Job Pod（runtime image）は yusekiya/stg-jupyter:2.1.0 を使用する（別途管理）

# 10. RabbitMQ のデプロイ
kubectl apply -f deployments/rabbitmq.yaml

# 11. Submit API のデプロイ
kubectl apply -f deployments/submit-api.yaml

# 12. Dispatcher のデプロイ
kubectl apply -f deployments/dispatcher.yaml

# 13. Watcher のデプロイ
kubectl apply -f deployments/watcher.yaml

# 14. NetworkPolicy の適用
kubectl apply -f networkpolicies/allow-submit-api.yaml

# 15. 各ユーザーの namespace 作成
./scripts/create-user-namespace.sh alice
./scripts/create-user-namespace.sh bob
```

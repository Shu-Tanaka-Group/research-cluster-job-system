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
  DISPATCH_BUDGET_PER_NAMESPACE: "30"
  DISPATCH_BUDGET_CHECK_INTERVAL_SEC: "10"
  KUEUE_LOCAL_QUEUE_NAME: default
  JOB_NAMESPACE_PREFIX: user-
  RUNTIME_IMAGE: <dockerhub-repo>/lab-runtime:latest
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
| CLI（fixed image 埋め込み） | image 内の設定ファイル | - |

---

## 7. Fixed Image 設計

### 7.1 image の役割

同一の fixed image が2つの用途で使われる。

| 用途 | Pod | cjob CLI の使用 |
|---|---|---|
| ユーザー作業環境 | User Pod（JupyterHub） | 使う |
| ジョブ実行環境 | Job Pod（Kubernetes Job） | 使わない |

### 7.2 image の内容

| カテゴリ | パッケージ / 設定 | 理由 |
|---|---|---|
| ベース OS | Ubuntu 24.04 | 安定性・パッケージの豊富さ |
| Python | python3.12 python3.12-venv python3-pip | 仮想環境のベース |
| ビルドツール | gcc g++ make | C 拡張ライブラリのビルド |
| 科学計算系ライブラリ | libopenblas-dev liblapack-dev | numpy 等の依存 |
| HPC 系ツール | openmpi-bin | MPI ジョブへの対応 |
| 基本ツール | git curl wget vim | 作業用 |
| cjob CLI | `/usr/local/bin/cjob` | User Pod での使用 |
| CLI 設定ファイル | `/etc/cjob/config.yaml` | Submit API エンドポイント等 |

含めないもの：ユーザーの Python パッケージ（各自が `/home/jovyan` 配下で venv を管理）・CUDA / GPU ドライバ（初期スコープ外）・Jupyter 本体（JupyterHub 側が管理）。

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

# cjob CLI のインストール
COPY cjob /usr/local/bin/cjob
RUN chmod +x /usr/local/bin/cjob

# CLI のデフォルト設定
COPY cjob-config.yaml /etc/cjob/config.yaml
```

### 7.4 CLI 設定ファイル（image 埋め込み）

```yaml
# /etc/cjob/config.yaml
submit_api_url: http://submit-api.cjob-system.svc.cluster.local:8080
```

環境変数 `CJOB_API_URL` でオーバーライド可能。

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
    count/jobs.batch: "50"
    requests.cpu: "12"
    requests.memory: "50Gi"
    limits.cpu: "12"
    limits.memory: "50Gi"
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

---

## 13. 初期セットアップ手順

新規クラスタへの初回セットアップ手順。

```bash
# 1. cjob-system namespace の作成
kubectl create namespace cjob-system

# 2. Secret の作成
kubectl apply -f secrets/postgres-secret.yaml
kubectl apply -f secrets/rabbitmq-secret.yaml

# 3. ConfigMap の作成
kubectl apply -f configmaps/cjob-config.yaml

# 4. RBAC の作成
kubectl apply -f rbac/submit-api-sa.yaml
kubectl apply -f rbac/dispatcher-sa.yaml

# 5. Kueue のインストール
kubectl apply -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.x.x/manifests.yaml

# 6. Kueue リソースの作成
kubectl apply -f kueue/resource-flavor.yaml
kubectl apply -f kueue/cluster-queue.yaml

# 7. PostgreSQL のデプロイ
kubectl apply -f deployments/postgres.yaml

# 8. RabbitMQ のデプロイ
kubectl apply -f deployments/rabbitmq.yaml

# 9. Submit API のデプロイ
kubectl apply -f deployments/submit-api.yaml

# 10. Dispatcher のデプロイ
kubectl apply -f deployments/dispatcher.yaml

# 11. Watcher のデプロイ
kubectl apply -f deployments/watcher.yaml

# 12. NetworkPolicy の適用
kubectl apply -f networkpolicies/allow-submit-api.yaml

# 13. fixed image のビルドと push
docker build -t <dockerhub-repo>/lab-runtime:latest .
docker push <dockerhub-repo>/lab-runtime:latest

# 14. 各ユーザーの namespace 作成
./scripts/create-user-namespace.sh alice
./scripts/create-user-namespace.sh bob
```

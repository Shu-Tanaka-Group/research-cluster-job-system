# CJob Deployment 設計書

## 1. 概要

本設計書は、CJob システムの Kubernetes 上への配置・構成管理に関する設計をまとめたものである。

### マニフェスト管理

K8s マニフェストは Kustomize の base / overlay 構成で管理する。

```
リポジトリ内:
  k8s/
    base/              # 環境非依存のマニフェスト（デフォルト値を含む）
    overlay-example/   # overlay のサンプル（コピーして使用する）

リポジトリ外（管理者が作成）:
  my-overlay/
    kustomization.yaml              # base への参照、image、StorageClass、ConfigMap パッチ
    configmap-cjob-config.yaml      # チューニングした ConfigMap 値
```

base にはデフォルト値を含む全マニフェストが入っており、環境固有の値は**リポジトリ外に配置した overlay** で上書きする。overlay のサンプルは `k8s/overlay-example/` を参照。

デプロイはリポジトリを clone し、overlay を指定して行う。

```bash
kubectl apply -k /path/to/my-overlay
```

Secret（`postgres-secret`）は Kustomize の管理対象外とし、管理者が手動で作成する。テンプレートは `k8s/base/secret-postgres.yaml` を参照。

以下の環境依存値は overlay で管理する。

| 設定項目 | overlay での設定方法 |
|---|---|
| image 名・タグ | `images[].newName` / `images[].newTag` |
| StorageClass | `patches[]`（JSON Patch） |
| ConfigMap `cjob-config` の値 | `patches[]`（`configmap-cjob-config.yaml` で上書き） |

---

## 2. namespace 構成

```
cjob-system        : システムコンポーネント全体（Submit API / Dispatcher / Watcher / PostgreSQL）
<user-namespace>   : ユーザーごとの実行環境（User Pod / Job Pod / LocalQueue / ResourceQuota / PVC）
```

ユーザー namespace は任意の名前を使用できる（例: `alice`, `user-alice`, `lab-physics`）。
識別はラベル `cjob.io/user-namespace=true` で行い、ユーザー名は namespace のアノテーション `cjob.io/username` から取得する。

---

## 3. コンポーネント配置

| コンポーネント | 種類 | Replica | namespace |
|---|---|---|---|
| Submit API | Deployment | 2以上推奨 | cjob-system |
| Dispatcher | Deployment | 1 | cjob-system |
| Watcher / Reconciler | Deployment | 1 | cjob-system |
| PostgreSQL | StatefulSet | 1 | cjob-system |

Dispatcher・Watcher を Replica 1 に固定する理由：Replica 複数にすると二重 dispatch・二重 DB 更新が発生するため。
Submit API は stateless であるため Replica を増やしても安全。可用性向上のため Replica 2 以上を推奨する。

---

## 4. PVC 構成

PostgreSQL は PVC を持つ。StorageClass は NFS subdir external provisioner を使用する。

| PVC 名 | 対象 | 用途 |
|---|---|---|
| `postgres-data` | PostgreSQL | DB ファイルの永続化 |
| `cli-binary` | Submit API | CLI バイナリの配布用ストレージ |

### 4.1 `cli-binary` PVC

CLI セルフアップデート機能（`cjob update`）で配布するバイナリを格納する。Submit API Pod から読み取り専用でマウントし、`/v1/cli/version` および `/v1/cli/download` エンドポイントで配信する。

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

ディレクトリ構成:

```
/cli-binary/
  latest          # 最新バージョン番号を記載したテキストファイル（例: "1.2.0"）
  1.1.0/
    cjob          # linux/amd64 バイナリ
  1.2.0/
    cjob
```

バイナリの配置は `cjobctl cli deploy` で行う（[cjobctl.md](architecture/cjobctl.md) §5.6 参照）。

```bash
# CLI をビルド後、PVC に配置する（ビルド手順は build.md §3 を参照）
cjobctl cli deploy --binary ./cli/target/x86_64-unknown-linux-musl/release/cjob --version <version>
```

運用時の一連の手順は [operations.md](operations.md) §8 を参照。

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
  DISPATCH_BUDGET_PER_NAMESPACE: "32"
  DISPATCH_BATCH_SIZE: "50"
  DISPATCH_ROUND_SIZE: "1"
  DISPATCH_BUDGET_CHECK_INTERVAL_SEC: "10"
  DISPATCH_RETRY_INTERVAL_SEC: "30"
  DISPATCH_MAX_RETRIES: "5"
  GAP_FILLING_ENABLED: "true"
  GAP_FILLING_STALL_THRESHOLD_SEC: "300"
  FAIR_SHARE_WINDOW_DAYS: "7"
  NODE_LABEL_SELECTOR: "cluster-job=true"
  NODE_RESOURCE_SYNC_INTERVAL_SEC: "300"
  MAX_QUEUED_JOBS_PER_NAMESPACE: "500"
  DEFAULT_TIME_LIMIT_SECONDS: "86400"
  MAX_TIME_LIMIT_SECONDS: "604800"
  MAX_SWEEP_COMPLETIONS: "1000"
  KUEUE_LOCAL_QUEUE_NAME: default
  USER_NAMESPACE_LABEL: cjob.io/user-namespace=true   # NetworkPolicy・cjobctl が参照。サーバーコンポーネントの env には注入不要
  WORKSPACE_MOUNT_PATH: /home/jovyan
  LOG_BASE_DIR: /home/jovyan/.cjob/logs
  JOB_NODE_TAINT: "role=computing:NoSchedule"
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
```

### 6.3 各コンポーネントが参照するリソース

| コンポーネント | ConfigMap | Secret |
|---|---|---|
| Submit API | `cjob-config` | `postgres-secret` |
| Dispatcher | `cjob-config` | `postgres-secret` |
| Watcher | `cjob-config` | `postgres-secret` |
| PostgreSQL | - | `postgres-secret` |
| CLI（`cjob update` で配布） | -（ログパスは API から取得） | - |
| cjobctl | `cjob-config`（`config show` で参照） | - |
| NetworkPolicy | -（`USER_NAMESPACE_LABEL` の値を YAML にハードコード） | - |

`USER_NAMESPACE_LABEL` はサーバーコンポーネント（Submit API / Dispatcher / Watcher）の env には注入しない。NetworkPolicy の `namespaceSelector` と cjobctl の `weight exclusive` コマンドが参照する。

---

## 7. Runtime Image 設計

### 7.1 image の役割

同一の image（User Pod の環境変数 `CJOB_IMAGE` または `JUPYTER_IMAGE` から取得したもの）が2つの用途で使われる。`cjob` CLI は image には含めず、ユーザーが各自でインストールする。

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

含めないもの：`cjob` CLI（Submit API 経由で `cjob update` により配布）・ユーザーの Python パッケージ（各自が `/home/jovyan` 配下で venv を管理）・CUDA / GPU ドライバ（初期スコープ外）・Jupyter 本体（JupyterHub 側が管理）。

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

`cjob` CLI は Rust 製シングルバイナリとして Submit API 経由で配布する。
ビルド済みバイナリは `cjob-system` namespace の PVC（`cli-binary`）に配置し、Submit API がエンドポイント（`/v1/cli/version`、`/v1/cli/download`）として配信する。
ユーザーは `cjob update` コマンドでセルフアップデートできる。

初回インストール時はバイナリが存在しないため、管理者が直接配布するか、以下のように Submit API から取得する。

```bash
mkdir -p /home/jovyan/.local/bin
curl -L http://submit-api.cjob-system.svc.cluster.local:8080/v1/cli/download \
  -o /home/jovyan/.local/bin/cjob
chmod +x /home/jovyan/.local/bin/cjob
```

#### CLI の環境変数

Submit API のエンドポイントは環境変数 `CJOB_API_URL` で設定する。
未設定時はデフォルト値（`http://submit-api.cjob-system.svc.cluster.local:8080`）を使用する。

ログディレクトリのパスは CLI 側で設定不要である。CLI は API からログパスを取得するため、サーバー側の ConfigMap（`LOG_BASE_DIR`）を変更するだけで CLI にも反映される。

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
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["get"]
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
Watcher は管理の簡略化のため Dispatcher と `dispatcher-sa` を共用する。
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
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list"]
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
`cjob-system` namespace 内の Pod 間通信（Submit API ↔ PostgreSQL など）は制限しない。
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

NS_NAME=$1
USERNAME=$2

if [ -z "${NS_NAME}" ] || [ -z "${USERNAME}" ]; then
  echo "Usage: $0 <namespace-name> <username>"
  exit 1
fi

echo "Creating namespace and resources: ns=${NS_NAME}, user=${USERNAME}"

# namespace 作成
kubectl create namespace ${NS_NAME}

# ユーザー namespace 識別ラベルとユーザー名アノテーションを付与
kubectl label namespace ${NS_NAME} cjob.io/user-namespace=true
kubectl annotate namespace ${NS_NAME} cjob.io/username=${USERNAME}

# User Pod 用 ServiceAccount 作成
kubectl create serviceaccount computing-user -n ${NS_NAME}

# JupyterHub KubeSpawner 設定（config.yaml）
# service_account: computing-user を設定済みであること

# Kueue LocalQueue 作成
# LocalQueue 名（下記 "default"）は ConfigMap の KUEUE_LOCAL_QUEUE_NAME と一致させること。
# KUEUE_LOCAL_QUEUE_NAME を変更する場合はこのスクリプトの LocalQueue 名も同時に修正が必要。
kubectl apply -f - <<EOF
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: default   # KUEUE_LOCAL_QUEUE_NAME の値と一致させること
  namespace: ${NS_NAME}
spec:
  clusterQueue: cjob-cluster-queue
EOF

# ResourceQuota 作成
kubectl apply -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: computing-quota
  namespace: ${NS_NAME}
spec:
  hard:
    count/jobs.batch: "100"
    requests.cpu: "300"
    requests.memory: "1250Gi"
    limits.cpu: "300"
    limits.memory: "1250Gi"
EOF

echo "Done: ns=${NS_NAME}, user=${USERNAME}"
```

---

## 12. JupyterHub 設定

User Pod に `computing-user` ServiceAccount を付与するための KubeSpawner 設定。

```yaml
# JupyterHub config.yaml
hub:
  config:
    KubeSpawner:
      service_account: computing-user
```

### Job Pod イメージの環境変数について

`cjob` CLI は Job Pod に使用する image 名を User Pod の環境変数から以下の優先順位で取得する。

1. `CJOB_IMAGE`（優先）
2. `JUPYTER_IMAGE`（フォールバック）

JupyterHub 環境では `JUPYTER_IMAGE` が User Pod 起動時に自動的に注入されるため、
追加の設定変更は不要である。

JupyterHub 以外の環境で使用する場合は、User Pod に `CJOB_IMAGE` 環境変数を設定し、
使用するイメージ名を値として指定する。

```
CJOB_IMAGE=my-registry/my-image:1.0
```

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
        image         TEXT NOT NULL,
        command       TEXT NOT NULL,
        cwd           TEXT NOT NULL,
        env_json      JSONB NOT NULL DEFAULT '{}',
        cpu           TEXT NOT NULL,
        memory        TEXT NOT NULL,
        gpu           INTEGER NOT NULL DEFAULT 0,
        time_limit_seconds INTEGER NOT NULL,
        status        TEXT NOT NULL,
        retry_count   INTEGER NOT NULL DEFAULT 0,
        retry_after   TIMESTAMPTZ,
        k8s_job_name  TEXT,
        log_dir       TEXT,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        dispatched_at TIMESTAMPTZ,
        started_at    TIMESTAMPTZ,
        finished_at   TIMESTAMPTZ,
        last_error    TEXT,
        completions       INTEGER,
        parallelism       INTEGER,
        completed_indexes TEXT,
        failed_indexes    TEXT,
        succeeded_count   INTEGER,
        failed_count      INTEGER,
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
            ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS namespace_weights (
        namespace TEXT PRIMARY KEY,
        weight    INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS namespace_daily_usage (
        namespace              TEXT NOT NULL,
        usage_date             DATE NOT NULL,
        cpu_millicores_seconds BIGINT NOT NULL DEFAULT 0,
        memory_mib_seconds     BIGINT NOT NULL DEFAULT 0,
        gpu_seconds            BIGINT NOT NULL DEFAULT 0,
        PRIMARY KEY (namespace, usage_date)
    );
    CREATE TABLE IF NOT EXISTS node_resources (
        node_name           TEXT PRIMARY KEY,
        cpu_millicores      INTEGER NOT NULL,
        memory_mib          INTEGER NOT NULL,
        gpu                 INTEGER NOT NULL DEFAULT 0,
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
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

### 13.3 Submit API Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: submit-api
  namespace: cjob-system
spec:
  replicas: 2   # stateless のため複数 Replica 可。可用性向上のため 2 以上を推奨
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
            - name: MAX_QUEUED_JOBS_PER_NAMESPACE
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: MAX_QUEUED_JOBS_PER_NAMESPACE
            - name: LOG_BASE_DIR
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: LOG_BASE_DIR
            - name: DEFAULT_TIME_LIMIT_SECONDS
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: DEFAULT_TIME_LIMIT_SECONDS
            - name: MAX_TIME_LIMIT_SECONDS
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: MAX_TIME_LIMIT_SECONDS
            - name: MAX_SWEEP_COMPLETIONS
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: MAX_SWEEP_COMPLETIONS
          resources:
            requests:
              cpu: "100m"
              memory: "256Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          volumeMounts:
            - name: cli-binary
              mountPath: /cli-binary
              readOnly: true
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
      volumes:
        - name: cli-binary
          persistentVolumeClaim:
            claimName: cli-binary
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

### 13.4 Dispatcher Deployment

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
            - name: DISPATCH_BUDGET_PER_NAMESPACE
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: DISPATCH_BUDGET_PER_NAMESPACE
            - name: DISPATCH_BATCH_SIZE
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: DISPATCH_BATCH_SIZE
            - name: DISPATCH_ROUND_SIZE
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: DISPATCH_ROUND_SIZE
            - name: DISPATCH_BUDGET_CHECK_INTERVAL_SEC
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: DISPATCH_BUDGET_CHECK_INTERVAL_SEC
            - name: DISPATCH_RETRY_INTERVAL_SEC
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: DISPATCH_RETRY_INTERVAL_SEC
            - name: DISPATCH_MAX_RETRIES
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: DISPATCH_MAX_RETRIES
            - name: GAP_FILLING_ENABLED
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: GAP_FILLING_ENABLED
            - name: GAP_FILLING_STALL_THRESHOLD_SEC
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: GAP_FILLING_STALL_THRESHOLD_SEC
            - name: FAIR_SHARE_WINDOW_DAYS
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: FAIR_SHARE_WINDOW_DAYS
            - name: KUEUE_LOCAL_QUEUE_NAME
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: KUEUE_LOCAL_QUEUE_NAME
            - name: WORKSPACE_MOUNT_PATH
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: WORKSPACE_MOUNT_PATH
            - name: LOG_BASE_DIR
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: LOG_BASE_DIR
            - name: JOB_NODE_TAINT
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: JOB_NODE_TAINT
          resources:
            requests:
              cpu: "100m"
              memory: "256Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
          livenessProbe:
            exec:
              # メインループが DISPATCH_BUDGET_CHECK_INTERVAL_SEC ごとに /tmp/liveness をタッチする
              # 最終更新から 120 秒以上経過した場合はループ停止とみなして再起動
              command: ["sh", "-c", "test $(( $(date +%s) - $(stat -c %Y /tmp/liveness) )) -lt 120"]
            initialDelaySeconds: 30
            periodSeconds: 30
```

### 13.5 Watcher Deployment

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
            - name: DISPATCH_BUDGET_CHECK_INTERVAL_SEC
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: DISPATCH_BUDGET_CHECK_INTERVAL_SEC
            - name: NODE_LABEL_SELECTOR
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: NODE_LABEL_SELECTOR
            - name: NODE_RESOURCE_SYNC_INTERVAL_SEC
              valueFrom:
                configMapKeyRef:
                  name: cjob-config
                  key: NODE_RESOURCE_SYNC_INTERVAL_SEC
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "300m"
              memory: "256Mi"
          livenessProbe:
            exec:
              # メインループが定期的に /tmp/liveness をタッチする
              # 最終更新から 120 秒以上経過した場合はループ停止とみなして再起動
              command: ["sh", "-c", "test $(( $(date +%s) - $(stat -c %Y /tmp/liveness) )) -lt 120"]
            initialDelaySeconds: 30
            periodSeconds: 30
```

---

## 14. Kyverno によるイメージ制限

Job Pod で使用可能なイメージを制限し、ユーザーによるイメージ書き換えを防止する。
Kyverno の ClusterPolicy を使い、ユーザー namespace 内の Job に対して許可リスト外のイメージを拒否する。

### 14.1 Kyverno インストール

```bash
helm repo add kyverno https://kyverno.github.io/kyverno/
helm repo update
helm upgrade kyverno kyverno/kyverno -n kyverno --install --create-namespace --version 3.7.1
```

### 14.2 ClusterPolicy の適用

`yusekiya/stg-*` から始まるイメージのみを許可する。
ユーザー namespace（`cjob.io/user-namespace: "true"` ラベル付き）内の Job だけが対象であり、
`cjob-system` namespace のシステムコンポーネントには影響しない。

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
        message: "許可されていないイメージです。yusekiya/stg-* のイメージのみ使用できます。"
        pattern:
          spec:
            template:
              spec:
                containers:
                  - image: "yusekiya/stg-*"
```

```bash
kubectl apply -f policies/restrict-job-image.yaml
```

---

## 15. Kueue インストール


マニフェストをダウンロード

```bash
curl -L -o kueue-manifests.yaml https://github.com/kubernetes-sigs/kueue/releases/download/v0.16.4/manifests.yaml
```

ファイル内で kueue-manager-config ConfigMapを探し，controller_manager_config.yaml の integrations セクションを変更．これを行わないと，KueueのQueueを適用するnamespaceの全てのリソースがKueueの管理対象となる．例えば，JupyterHubのユーザーnamespaceをKueueの適用対象とした場合，Notebook PodもKueueの対象となるため，起動に失敗する．

```yaml
integrations:
  frameworks:
    - "batch/job"
    # - Pos # ←PodやDeploymentなどを範囲対象から外す
```

Kueue リソースの作成

```bash
kubectl apply -f kueue/resource-flavor.yaml
kubectl apply -f kueue/cluster-queue.yaml
```

---

## 16. 計算ノードの準備

ジョブを実行するノードに `cluster-job=true` ラベルと Taint を付与する。Taint の値は ConfigMap `cjob-config` の `JOB_NODE_TAINT` で設定する（デフォルト: `role=computing:NoSchedule`）。

| 設定 | 参照先 | 用途 |
|---|---|---|
| `cluster-job=true` ラベル | Kueue ResourceFlavor の `nodeLabels` | Kueue が Job Pod をスケジュールするノードの選定 |
| `cluster-job=true` ラベル | ConfigMap `NODE_LABEL_SELECTOR` | Watcher がノードの allocatable リソースを取得する対象の選定 |
| `JOB_NODE_TAINT` の値 | Kueue ResourceFlavor の `nodeTaints` / Job Pod の `tolerations` | 一般の Pod が計算ノードにスケジュールされることを防止 |

**重要:** ConfigMap `JOB_NODE_TAINT`・Kueue ResourceFlavor の `nodeTaints`・ノードの Taint の 3 箇所は同じ値に統一する必要がある。不一致の場合、Job Pod がスケジュールされない。

```bash
# 計算ノードにラベルと Taint を付与する
# <node-name> はクラスタ内の計算ノード名に置き換える
kubectl label node <node-name> cluster-job=true
kubectl taint node <node-name> role=computing:NoSchedule

# 確認
kubectl get nodes -l cluster-job=true
kubectl describe node <node-name> | grep -A5 Taints
```

**Taint を使わない運用（共用ノード）:** 専用ノードを持たない環境では `JOB_NODE_TAINT` を空文字列に設定し、Kueue ResourceFlavor の `nodeTaints` を省略し、ノードへの Taint 付与を行わない。

計算ノードを追加・撤去した場合、Watcher が `node_resources` テーブルを自動的に同期するため、Dispatcher や Submit API の設定変更は不要である。

---

## 17. 初期セットアップ手順

新規クラスタへの初回セットアップ手順。§16 の計算ノード準備が完了していることが前提。

```bash
# 1. Secret の作成（Kustomize 管理対象外のため手動で作成する）
kubectl create namespace cjob-system
kubectl create secret generic postgres-secret -n cjob-system \
  --from-literal=POSTGRES_USER=cjob \
  --from-literal=POSTGRES_PASSWORD='<password>' \
  --from-literal=POSTGRES_DB=cjob

# 2. overlay の準備
# k8s/overlay-example/ をリポジトリ外にコピーし、環境に合わせて編集する
# - kustomization.yaml: resources の base パス、image 名・タグ、StorageClass
# - configmap-cjob-config.yaml: チューニングしたい ConfigMap の値
cp -r k8s/overlay-example /path/to/my-overlay
# kustomization.yaml の resources パスを編集する
# 例: resources: [../stg-cluster-job-system/k8s/base]

# 3. システムコンポーネント image のビルドと push
read -r VERSION < VERSION
docker build -t yusekiya/cjob-submit-api:${VERSION} -f server/Dockerfile.api server/
docker build -t yusekiya/cjob-dispatcher:${VERSION} -f server/Dockerfile.dispatcher server/
docker build -t yusekiya/cjob-watcher:${VERSION} -f server/Dockerfile.watcher server/
docker push yusekiya/cjob-submit-api:${VERSION}
docker push yusekiya/cjob-dispatcher:${VERSION}
docker push yusekiya/cjob-watcher:${VERSION}
# Job Pod（runtime image）は yusekiya/stg-jupyter:2.1.0 を使用する（別途管理）

# 4. Kustomize で全リソースをデプロイ
kubectl apply -k /path/to/my-overlay

# DB スキーマの初期化:
# postgres-schema ConfigMap の schema.sql が /docker-entrypoint-initdb.d/ にマウントされ、
# PostgreSQL 初回起動時に自動実行される。
# IF NOT EXISTS を使用しているため再デプロイ時も安全に再実行できる。

# 5. Kueue リソースの作成（kueue.md 参照）
kubectl apply -f kueue/resource-flavor.yaml
kubectl apply -f kueue/cluster-queue.yaml

# 6. Kyverno のインストールとイメージ制限ポリシーの適用
helm repo add kyverno https://kyverno.github.io/kyverno/
helm repo update
helm upgrade kyverno kyverno/kyverno -n kyverno --install --create-namespace --version 3.7.1
kubectl apply -f policies/restrict-job-image.yaml

# 7. 各ユーザーの namespace 作成（引数: <namespace名> <ユーザー名>）
./scripts/create-user-namespace.sh user-alice alice
./scripts/create-user-namespace.sh user-bob bob

# 8. CLI バイナリの配置（§4.1 参照）
# cjobctl のビルドは build.md「管理 CLI（cjobctl）のビルド」を参照
cargo build --release --target x86_64-unknown-linux-musl --manifest-path cli/Cargo.toml
cjobctl cli deploy --binary ./cli/target/x86_64-unknown-linux-musl/release/cjob --version ${VERSION}
```

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

ソース: [`secret-postgres.yaml`](../k8s/base/secret-postgres.yaml)

設計上の要点:
- Kustomize の管理対象外とし、管理者が初回セットアップ時に `kubectl create secret` で手動作成する
- テンプレートファイルのコメントに作成手順を記載

---

## 6. ConfigMap 設計

### 6.1 `cjob-config`（共通設定）

Submit API・Dispatcher・Watcher が共通で参照する。

ソース: [`configmap-cjob-config.yaml`](../k8s/base/configmap-cjob-config.yaml)

設計上の要点:
- base にデフォルト値を定義し、環境固有の値は overlay の patches で上書きする（`k8s/overlay-example/` 参照）
- `USER_NAMESPACE_LABEL` はサーバーコンポーネントのうち Submit API / Dispatcher の env には注入しない。Watcher は ResourceQuota 同期（[watcher.md](architecture/watcher.md) §1.3）で使用するため env に注入する。NetworkPolicy の `namespaceSelector` と cjobctl の `weight exclusive` コマンドも参照する
- 各設定値の意味と設計根拠は [resources.md](architecture/resources.md) を参照

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
| NetworkPolicy | -（`USER_NAMESPACE_LABEL` / `PROMETHEUS_NAMESPACE_LABEL` の値を YAML にハードコード） | - |

`USER_NAMESPACE_LABEL` は Submit API / Dispatcher の env には注入しない。Watcher は ResourceQuota 同期で使用するため env に注入する。NetworkPolicy の `namespaceSelector` と cjobctl の `weight exclusive` コマンドも参照する。`PROMETHEUS_NAMESPACE_LABEL` も同様に NetworkPolicy にハードコードし、overlay で上書きする。

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
| ベース OS | 任意（例: Ubuntu 24.04） | `/bin/bash` が利用可能であること |
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

ソース: [`rbac-submit-api.yaml`](../k8s/base/rbac-submit-api.yaml)

設計上の要点:
- Submit API が TokenReview API を呼ぶために ClusterRole `token-reviewer` を付与する
- namespace の `get` 権限は、ユーザー namespace のアノテーション `cjob.io/username` からユーザー名を取得するために必要

---

## 9. Dispatcher / Watcher の ServiceAccount と RBAC

ソース: [`rbac-dispatcher.yaml`](../k8s/base/rbac-dispatcher.yaml)

設計上の要点:
- Watcher は管理の簡略化のため Dispatcher と `dispatcher-sa` を共用する。Watcher の Deployment には `serviceAccountName: dispatcher-sa` を指定する
- ClusterRole `cjob-job-controller` は Job の CRUD、Pod の読み取り、Node の読み取り、Namespace の一覧取得、ResourceQuota の一覧取得を許可する
- Node の `get`/`list` 権限は Watcher が `node_resources` テーブルにノード情報を同期するために必要
- Namespace の `list` 権限は Watcher が `USER_NAMESPACE_LABEL` ラベルを持つユーザー namespace を列挙するために必要
- ResourceQuota の `list` 権限は Watcher が `namespace_resource_quotas` テーブルに全ユーザー namespace の ResourceQuota 使用状況を一括同期するために必要

---

## 10. NetworkPolicy

ソース:
- [`networkpolicy-allow-submit-api.yaml`](../k8s/base/networkpolicy-allow-submit-api.yaml)
- [`networkpolicy-allow-metrics-scrape.yaml`](../k8s/base/networkpolicy-allow-metrics-scrape.yaml)

設計上の要点:
- このクラスタには default-deny NetworkPolicy は存在しないため、`cjob-system` namespace 内の Pod 間通信（Submit API ↔ PostgreSQL など）は制限しない
- `allow-submit-api`: User namespace（`cjob.io/user-namespace: "true"` ラベル付き）から Submit API（TCP 8080）への通信を許可する。User namespace 以外からのアクセスを制限しセキュリティを確保する
- `allow-metrics-scrape`: Prometheus namespace から Submit API（TCP 8080）への metrics scrape を許可する。base のデフォルトは `kubernetes.io/metadata.name: monitoring`。異なる namespace を使用する場合は overlay で `namespaceSelector.matchLabels` をパッチする（`overlay-example/kustomization.yaml` 参照）

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
kubectl label namespace ${NS_NAME} type=user
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

ソース: [`configmap-postgres-schema.yaml`](../k8s/base/configmap-postgres-schema.yaml)

設計上の要点:
- スキーマ SQL を ConfigMap に格納し、PostgreSQL 公式 image の initdb 自動実行機構（`/docker-entrypoint-initdb.d/`）で適用する
- `IF NOT EXISTS` を使用しているため再デプロイ時も安全に再実行できる（べき等性の確保）
- テーブル設計の詳細は [database.md](architecture/database.md) を参照

### 13.2 PostgreSQL StatefulSet

ソース: [`postgres/statefulset.yaml`](../k8s/base/postgres/statefulset.yaml)

設計上の要点:
- Headless Service（`clusterIP: None`）を使用する（StatefulSet の DNS 解決に必要）
- `postgres-schema` ConfigMap を `/docker-entrypoint-initdb.d/` にマウントしてスキーマを自動初期化する
- StorageClass は base では placeholder 値（`STORAGE_CLASS`）とし、overlay で環境に合わせて上書きする
- Replica は 1 固定（シングルインスタンス構成）

### 13.3 Submit API Deployment

ソース: [`submit-api/deployment.yaml`](../k8s/base/submit-api/deployment.yaml)

設計上の要点:
- stateless のため Replica 2 以上を推奨（可用性向上）
- `cli-binary` PVC を ReadOnly でマウントし、CLI バイナリ配信エンドポイント（`/v1/cli/*`）で使用する
- Liveness / Readiness probe は `/healthz` エンドポイントで行う
- image 名は Kustomize の `images[]` で overlay から上書きする（base では短縮名のみ）

### 13.4 Dispatcher Deployment

ソース: [`dispatcher/deployment.yaml`](../k8s/base/dispatcher/deployment.yaml)

設計上の要点:
- Replica 1 固定（複数にすると二重 dispatch が発生するため）
- Liveness probe はファイルタイムスタンプ方式: メインループが `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` ごとに `/tmp/liveness` をタッチし、最終更新から 120 秒以上経過した場合はループ停止とみなして再起動する
- `dispatcher-sa` を使用（Watcher と共用、§9 参照）

### 13.5 Watcher Deployment

ソース: [`watcher/deployment.yaml`](../k8s/base/watcher/deployment.yaml)

設計上の要点:
- Replica 1 固定（複数にすると二重 DB 更新が発生するため）
- Liveness probe はファイルタイムスタンプ方式: メインループが定期的に `/tmp/liveness` をタッチし、最終更新から 120 秒以上経過した場合はループ停止とみなして再起動する
- `serviceAccountName: dispatcher-sa` を指定し、Dispatcher と ServiceAccount を共用する（§9 参照）

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

`your-registry/cjob-*` から始まるイメージのみを許可する。
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
        message: "許可されていないイメージです。your-registry/cjob-* のイメージのみ使用できます。"
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

### 15.1 Prometheus メトリクスの scrape 設定

#### CJob アプリケーションメトリクス

Submit API と Watcher は Pod テンプレートに `prometheus.io/scrape` アノテーションを持つ。Annotation-based discovery を使用する Prometheus 環境ではこれだけで自動的に scrape される。

Prometheus Operator を使用する環境では、overlay の resources に `base/prometheus-operator` ディレクトリを追加する（`overlay-example/kustomization.yaml` 参照）。

| リソース | ファイル | 対象 | ポート | パス |
|---|---|---|---|---|
| ServiceMonitor `submit-api` | `prometheus-operator/servicemonitor-submit-api.yaml` | Submit API Service | `http` (8080) | `/metrics` |
| PodMonitor `watcher` | `prometheus-operator/podmonitor-watcher.yaml` | Watcher Pod | `metrics` (9090) | `/metrics` |

Prometheus Operator の `serviceMonitorNamespaceSelector` / `podMonitorNamespaceSelector` が `cjob-system` namespace を監視対象に含んでいることを確認する。

適用後、Grafana の Explore 画面で `cjob_jobs_submitted_total` を検索し、メトリクスが表示されることを確認する。

#### Kueue メトリクス

Kueue メトリクスを Prometheus で収集するための ServiceMonitor を作成する。これがないと Grafana ダッシュボードの Kueue 関連パネルが表示されない。

```bash
kubectl apply --server-side -f https://github.com/kubernetes-sigs/kueue/releases/download/v0.16.4/prometheus.yaml
```

適用後、Grafana の Explore 画面で `kueue_pending_workloads` を検索し、メトリクスが表示されることを確認する。

### 15.2 ClusterQueue リソースメトリクスの有効化

Grafana ダッシュボードで CPU/GPU 使用率ゲージを表示するため、ClusterQueue リソースメトリクスを有効化する。この設定により `kueue_cluster_queue_resource_usage` / `kueue_cluster_queue_nominal_quota` メトリクスが Prometheus に公開される。

```bash
kubectl edit configmap kueue-manager-config -n kueue-system
```

`controller_manager_config.yaml` の `metrics` セクションに `enableClusterQueueResources: true` を追加する：

```yaml
metrics:
  enableClusterQueueResources: true
```

変更後、kueue-controller-manager を再起動する：

```bash
kubectl rollout restart deployment kueue-controller-manager -n kueue-system
```

有効化の確認は、Grafana の Explore 画面で `kueue_cluster_queue_resource_usage` を検索してメトリクスが表示されることで行う。

### 15.3 Kueue リソースの作成

```bash
kubectl apply -f kueue/resource-flavor.yaml
kubectl apply -f kueue/cluster-queue.yaml
```

---

## 16. 計算ノードの準備

計算ノードには共通キー `cjob.io/flavor` のラベルを付与し、値に flavor 名を設定する。このラベルは Kueue ResourceFlavor の `nodeLabels` および ConfigMap `RESOURCE_FLAVORS` の `label_selector` と一致させる。全 flavor で同一キーを使用することで、Kueue が cross-flavor の矛盾を検出し、誤った flavor へのスケジューリングを防止する。Taint の値は ConfigMap `cjob-config` の `JOB_NODE_TAINT` で設定する（デフォルト: `role=computing:NoSchedule`）。

**重要:** ConfigMap `JOB_NODE_TAINT`・Kueue ResourceFlavor の `nodeTaints`・ノードの Taint の 3 箇所は同じ値に統一する必要がある。不一致の場合、Job Pod がスケジュールされない。

**Taint を使わない運用（共用ノード）:** 専用ノードを持たない環境では `JOB_NODE_TAINT` を空文字列に設定し、Kueue ResourceFlavor の `nodeTaints` を省略し、ノードへの Taint 付与を行わない。

### 16.1 CPU ノード

```bash
# CPU 計算ノードにラベルと Taint を付与する
kubectl label node <node-name> cjob.io/flavor=cpu
kubectl taint node <node-name> role=computing:NoSchedule

# 確認
kubectl get nodes -l cjob.io/flavor=cpu
```

### 16.2 GPU ノード

GPU ノードには CPU ノードと同じキー `cjob.io/flavor` を使用し、値に GPU flavor 名を設定する。Taint は CPU ノードと同じ値を使用する。

```bash
# GPU ノードにラベルと Taint を付与する
kubectl label node <gpu-node-name> cjob.io/flavor=gpu
kubectl taint node <gpu-node-name> role=computing:NoSchedule

# 確認
kubectl get nodes -l cjob.io/flavor=gpu
```

ノードの振り分けは共通キー `cjob.io/flavor` のラベル値で制御される。Dispatcher が flavor の `label_selector` を K8s Job の `nodeSelector` として設定し、Kueue がそれにマッチする ResourceFlavor の `nodeLabels` に基づいてノードにスケジュールする。

計算ノードを追加・撤去した場合、Watcher が `node_resources` テーブルを自動的に同期するため、Dispatcher や Submit API の設定変更は不要である。

### 16.3 新しい ResourceFlavor の追加手順

異なる CPU アーキテクチャや異なる GPU モデルのノードを追加する場合、以下の手順で新しい flavor を作成する。

#### 1. ノードにラベルと Taint を付与する

```bash
kubectl label node <node-name> cjob.io/flavor=<flavor名>    # 例: cjob.io/flavor=gpu-h100
kubectl taint node <node-name> role=computing:NoSchedule
```

#### 2. Kueue ResourceFlavor を作成する

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: <flavor名>         # 例: gpu-h100（DB の flavor 値と一致させる）
spec:
  nodeLabels:
    cjob.io/flavor: "<flavor名>"    # ステップ 1 で付与したラベルと一致させる
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

#### 3. ClusterQueue に flavor を追加する

```bash
kubectl edit clusterqueue cjob-cluster-queue
```

`spec.resourceGroups[0].flavors` に新しい flavor のエントリを追加する。GPU リソースを持たない flavor は `nvidia.com/gpu` の nominalQuota を `"0"` に設定する。他の flavor のリソースを保護する場合は `lendingLimit: "0"` を設定する。

#### 4. ConfigMap `RESOURCE_FLAVORS` に定義を追加する

```bash
kubectl edit configmap cjob-config -n cjob-system
```

`RESOURCE_FLAVORS` の JSON 配列に新しい flavor 定義を追加する。GPU を持つ flavor は `gpu_resource_name` を指定する。

```json
{"name": "gpu-h100", "label_selector": "cjob.io/flavor=gpu-h100", "gpu_resource_name": "nvidia.com/gpu"}
```

#### 5. コンポーネントを再起動する

```bash
kubectl rollout restart deployment submit-api dispatcher watcher -n cjob-system
```

#### 6. 動作確認

```bash
# ノード同期の確認（次回の同期サイクルで反映される）
cjobctl cluster resources

# nominalQuota の確認
cjobctl cluster show-quota

# ジョブ投入の確認
cjob add --flavor <flavor名> -- echo hello
```

---

## 17. Grafana ダッシュボードのセットアップ

ユーザー向けクラスタ状況確認用の Grafana ダッシュボードを配置する。詳細な設計は [monitoring.md](architecture/monitoring.md) を参照。

### 17.1 前提条件

- Kueue メトリクスの Prometheus scrape が設定されていること（§15.1 参照）
- Kueue の ClusterQueue リソースメトリクスが有効化されていること（§15.2 参照）

### 17.2 PostgreSQL 読み取り専用ユーザーの作成

Grafana から CJob の PostgreSQL に接続するための読み取り専用ユーザーを作成する。

```bash
kubectl exec -it postgres-0 -n cjob-system -- psql -U cjob -d cjob
```

```sql
CREATE ROLE grafana_reader LOGIN PASSWORD '<secure-password>';
GRANT CONNECT ON DATABASE cjob TO grafana_reader;
GRANT USAGE ON SCHEMA public TO grafana_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_reader;
```

### 17.3 Grafana データソースの追加

Grafana に PostgreSQL データソースを追加する（Prometheus が未登録の場合はそちらも追加する）。

1. Grafana UI で `Connections` > `Data sources` を開く
2. `Add data source` をクリックし、`PostgreSQL` を選択する
3. 以下の値を入力する：

| 項目 | 値 |
|---|---|
| Name | `CJob DB` |
| Host URL | `postgres.cjob-system.svc.cluster.local:5432` |
| Database name | `cjob` |
| Username | `grafana_reader` |
| Password | §17.2 で設定したパスワード |
| TLS/SSL Mode | `disable`（クラスタ内通信のため） |

4. `Save & test` をクリックし、`Database Connection OK` と表示されることを確認する

**注意:** Grafana が `cjob-system` namespace 外で動作している場合でも、クラスタ内 DNS（`<service>.<namespace>.svc.cluster.local`）で到達可能であれば上記の Host URL を使用できる。クラスタ外の Grafana から接続する場合は、NodePort や Ingress 等で PostgreSQL に到達可能な経路を設定すること。

### 17.4 Grafana ユーザーのロール制限

`grafana_reader` は全テーブルの `SELECT` 権限を持つため、Grafana 上で任意の SQL を実行できるユーザーは `jobs` テーブルの `env_json` カラム（ジョブ投入時の環境変数）を直接読み取れる。他ユーザーの環境変数にはシークレットが含まれる可能性があるため、一般ユーザーには Grafana の **Viewer** ロールのみを付与し、ダッシュボードの編集や Explore（任意 SQL 実行）へのアクセスを制限すること。

### 17.5 ダッシュボードのインポート

`k8s/base/grafana/dashboard-user.json` を Grafana UI からインポートする。

1. Grafana UI で `Dashboards` > `Import` を開く
2. `Upload dashboard JSON file` で `dashboard-user.json` を選択する
3. データソースの選択画面で、`Prometheus` と `CJob DB` をそれぞれ環境のデータソースに紐づける
4. `Import` を実行する

---

## 18. 初期セットアップ手順

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
docker build -t your-registry/cjob-submit-api:${VERSION} -f server/Dockerfile.api server/
docker build -t your-registry/cjob-dispatcher:${VERSION} -f server/Dockerfile.dispatcher server/
docker build -t your-registry/cjob-watcher:${VERSION} -f server/Dockerfile.watcher server/
docker push your-registry/cjob-submit-api:${VERSION}
docker push your-registry/cjob-dispatcher:${VERSION}
docker push your-registry/cjob-watcher:${VERSION}
# Job Pod（runtime image）は your-registry/cjob-jupyter:2.1.0 を使用する（別途管理）

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

# バージョン移行手順

稼働中の CJob システムを新しいバージョンに更新する際の標準手順。変更内容に応じて不要なステップはスキップする。

> **ステップ順序の原則**: 各ステップは依存関係に基づいて並んでいる。あるステップの成果物を別のステップが必要とする場合、成果物の準備を先に行う。
>
> - イメージのビルド・push → Kustomize のタグ更新・適用（イメージが存在しないタグを参照しない）
> - cjobctl のビルド → DB マイグレーション（新しいマイグレーションロジックで実行する）

## 前提条件

- リポジトリが clone 済みであること
- overlay が作成済みであること（[deployment.md](deployment.md) §17 参照）
- `kubectl` でクラスタにアクセスできること
- Docker でイメージのビルド・push ができること

## Step 1: リポジトリの更新と差分確認

```bash
cd /path/to/stg-cluster-job-system
git fetch && git checkout <VERSION>
```

base の ConfigMap にキーが追加されていないか確認する。追加がある場合は overlay の `configmap-cjob-config.yaml` にチューニング値を反映するか、デフォルト値のままでよいか判断する。

```bash
git diff <old-tag>...<new-tag> -- k8s/base/configmap-cjob-config.yaml
```

## Step 2: イメージのビルドと push

サーバーコンポーネント（Python）に変更がある場合に実行する。変更の有無は `git diff <old-tag>...<new-tag> -- server/src/` で確認できる。

```bash
read -r VERSION < VERSION

# 変更があるコンポーネントのみビルド・push する
docker build -t your-registry/cjob-submit-api:${VERSION} -f server/Dockerfile.api server/
docker build -t your-registry/cjob-dispatcher:${VERSION} -f server/Dockerfile.dispatcher server/
docker build -t your-registry/cjob-watcher:${VERSION} -f server/Dockerfile.watcher server/

docker push your-registry/cjob-submit-api:${VERSION}
docker push your-registry/cjob-dispatcher:${VERSION}
docker push your-registry/cjob-watcher:${VERSION}
```

サーバーコンポーネントに変更がない場合は、タグを付け替えて push する。

```bash
read -r VERSION < VERSION

# Submit APIの例．他のコンポーネントも同様
docker tag your-registry/cjob-submit-api:${OLD_VERSION} your-registry/cjob-submit-api:${VERSION}
docker push your-registry/cjob-submit-api:${VERSION}
```

## Step 3: CLI / 管理ツールのビルド

### 3.1 cjobctl（管理者 PC）

`ctl/` に変更がある場合、**または DB スキーマの更新（Step 5）を実行する場合**はビルドする。

```bash
cd ctl/
cargo build --release
```

### 3.2 cjob CLI（ユーザー配布用）

`cli/` に変更がある場合。クロスコンパイルの詳細は [build.md](build.md) を参照。

```bash
cd cli/
cargo build --release --target x86_64-unknown-linux-musl
```

## Step 4: K8s リソースの適用

overlay の `kustomization.yaml` の `newTag` を新バージョンに更新する（Step 2 でイメージが push 済みであることを確認してから行う）。

```bash
kubectl apply -k /path/to/my-overlay
```

これにより、ConfigMap・RBAC・Deployment 等が一括で更新される。Deployment の image タグが変わっている場合は自動的にローリングアップデートが行われる。

> `postgres-schema` ConfigMap は PostgreSQL 初回起動時にのみ実行される。既存の DB には Step 5 の `cjobctl db migrate` で反映する。

Kyverno ポリシーに変更がある場合は Kustomize 管理外のため個別に適用する。

**デプロイ順序に注意**: コンポーネント間にデータ依存がある場合は、データを生産する側を先にデプロイする。例えば Watcher が DB にデータを書き込み、Dispatcher や Submit API がそのデータを参照する場合は Watcher を最初にデプロイする。依存がない場合は順序を問わない。

```bash
kubectl rollout status deployment/watcher -n cjob-system
kubectl rollout status deployment/dispatcher -n cjob-system
kubectl rollout status deployment/submit-api -n cjob-system
```

## Step 5: DB スキーマの更新

テーブルやカラムの追加がある場合に実行する。`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` により冪等に実行できる。Step 3 でビルドした新しい `cjobctl` を使用すること。

```bash
cjobctl db migrate
```

## Step 6: cjob CLI の配布

Step 3.2 で CLI をビルドした場合、`cjobctl cli deploy` で PVC にバイナリを配置する。ユーザーは `cjob update` でセルフアップデートできる。

```bash
read -r VERSION < VERSION
cjobctl cli deploy --binary ./target/x86_64-unknown-linux-musl/release/cjob --version ${VERSION}
```

## Step 7: 動作確認

```bash
# コンポーネントの状態
cjobctl system status

# ジョブの投入テスト
cjob add --cpu 1 --memory 1Gi -- echo "upgrade test"
cjob list
```

バージョン固有の確認項目がある場合は、PR の Test plan を参照する。

---

## flavor ノードラベルのキー統一（#119）

ノードラベルのキーを flavor ごとに異なるキー（`cluster-job=true`, `cluster-gpu-job=true` 等）から共通キー `cjob.io/flavor` に統一する。3 箇所（ノードラベル・Kueue ResourceFlavor・ConfigMap）を同時に変更する必要があるため、メンテナンスウィンドウでの適用が望ましい。

### 1. 実行中ジョブの完了を待つ

```bash
cjobctl jobs active
```

実行中のジョブがある場合、完了を待つか、必要に応じてキャンセルする。

### 2. ノードのラベルを変更する

旧ラベルを削除し、新ラベルを付与する。

```bash
# CPU ノード
kubectl label node <node-name> cluster-job-
kubectl label node <node-name> cjob.io/flavor=cpu

# GPU ノード（例: flavor 名が gpu の場合）
kubectl label node <gpu-node-name> cluster-gpu-job-
kubectl label node <gpu-node-name> cjob.io/flavor=gpu
```

### 3. Kueue ResourceFlavor を更新する

各 ResourceFlavor の `nodeLabels` を更新する。

```bash
kubectl edit resourceflavor cpu
```

```yaml
# 変更前
spec:
  nodeLabels:
    cluster-job: "true"

# 変更後
spec:
  nodeLabels:
    cjob.io/flavor: "cpu"
```

GPU flavor も同様に更新する。

### 4. ConfigMap `RESOURCE_FLAVORS` を更新する

```bash
kubectl edit configmap cjob-config -n cjob-system
```

```yaml
# 変更前
RESOURCE_FLAVORS: |
  [
    {"name": "cpu", "label_selector": "cluster-job=true"},
    {"name": "gpu", "label_selector": "cluster-gpu-job=true", "gpu_resource_name": "nvidia.com/gpu"}
  ]

# 変更後
RESOURCE_FLAVORS: |
  [
    {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
    {"name": "gpu", "label_selector": "cjob.io/flavor=gpu", "gpu_resource_name": "nvidia.com/gpu"}
  ]
```

### 5. コンポーネントを再起動する

```bash
kubectl rollout restart deployment submit-api dispatcher watcher -n cjob-system
```

### 6. 動作確認

```bash
# ノード同期の確認
cjobctl cluster resources

# ジョブ投入の確認
cjob add --cpu 1 --memory 1Gi -- echo "label migration test"
cjob list
```

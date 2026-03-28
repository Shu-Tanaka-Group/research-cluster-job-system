# バージョン移行手順

稼働中の CJob システムを新しいバージョンに更新する際の標準手順。変更内容に応じて不要なステップはスキップする。

## 前提条件

- `kubectl` でクラスタにアクセスできること
- Docker でイメージのビルド・push ができること
- `cjobctl` がビルド済みであること（Step 5 で更新する場合は旧バージョンでよい）

## Step 1: K8s リソースの更新

### 1.1 Kustomize 管理リソースの更新

Kustomize で管理されているリソース（RBAC / Deployment / postgres-schema ConfigMap 等）を一括で更新する。変更の有無は `git diff <old-tag>...<new-tag> -- k8s/` で確認できる。

```bash
kubectl apply -k 'https://github.com/Shu-Tanaka-Group/stg-cluster-job-system/k8s/overlays/stg?ref=<VERSION>'
```

これにより、RBAC の権限変更、Deployment の image タグ更新（Step 3・4 に相当）が一括で適用される。Deployment に変更がある場合は自動的にローリングアップデートが行われる。

> `postgres-schema` ConfigMap は PostgreSQL 初回起動時にのみ実行される。既存の DB には Step 2 の `cjobctl db migrate` で反映する。

Kyverno ポリシーに変更がある場合は Kustomize 管理外のため個別に適用する。

### 1.2 ConfigMap `cjob-config` の更新

`cjob-config` は Kustomize 管理外のため、新しいキーの追加や既存キーの変更がある場合は手動で更新する。変更の有無は `git diff <old-tag>...<new-tag> -- k8s/base/configmap-cjob-config.yaml` で確認できる。

```bash
# 新しいキーを追加する例
kubectl patch configmap cjob-config -n cjob-system --type merge \
  -p '{"data":{"NEW_KEY":"value"}}'
```

> ConfigMap を更新しても、既存の Pod は再起動するまで古い環境変数を使い続ける。Step 4 でロールアウトが行われれば新しい値が反映される。変更がない場合は手動で再起動する。

```bash
kubectl rollout restart deployment/submit-api -n cjob-system
kubectl rollout restart deployment/dispatcher -n cjob-system
kubectl rollout restart deployment/watcher -n cjob-system
```

## Step 2: DB スキーマの更新

テーブルやカラムの追加がある場合に実行する。`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` により冪等に実行できる。

```bash
cjobctl db migrate
```

## Step 3: イメージのビルドと push

サーバーコンポーネント（Python）に変更がある場合に実行する。変更の有無は `git diff <old-tag>...<new-tag> -- server/src/` で確認できる。

```bash
read -r VERSION < VERSION

# 変更があるコンポーネントのみビルド・push する
docker build -t yusekiya/cjob-submit-api:${VERSION} -f server/Dockerfile.api server/
docker build -t yusekiya/cjob-dispatcher:${VERSION} -f server/Dockerfile.dispatcher server/
docker build -t yusekiya/cjob-watcher:${VERSION} -f server/Dockerfile.watcher server/

docker push yusekiya/cjob-submit-api:${VERSION}
docker push yusekiya/cjob-dispatcher:${VERSION}
docker push yusekiya/cjob-watcher:${VERSION}
```

サーバーコンポーネントに変更がない場合は、タグを付け替えてpushする。


```bash
read -r VERSION < VERSION

# Submit APIの例．他のコンポーネントも同様
docker tag yusekiya/cjob-submit-api:${OLD_VERSION} yusekiya/cjob-submit-api:${VERSION}
docker push yusekiya/cjob-submit-api:${VERSION}
```

## Step 4: コンポーネントの再デプロイ

Step 1 の `kubectl apply -k` で image タグの更新と Deployment の再デプロイが一括で行われる。Step 3 でイメージを push した後、以下でロールアウトの完了を確認する。

**デプロイ順序に注意**: コンポーネント間にデータ依存がある場合は、データを生産する側を先にデプロイする。例えば Watcher が DB にデータを書き込み、Dispatcher や Submit API がそのデータを参照する場合は Watcher を最初にデプロイする。依存がない場合は順序を問わない。

```bash
kubectl rollout status deployment/watcher -n cjob-system
kubectl rollout status deployment/dispatcher -n cjob-system
kubectl rollout status deployment/submit-api -n cjob-system
```

## Step 5: CLI / 管理ツールのビルド

### 5.1 cjobctl（管理者 PC）

`ctl/` に変更がある場合:

```bash
cd ctl/
cargo build --release
```

### 5.2 cjob CLI（ユーザー配布用）

`cli/` に変更がある場合。クロスコンパイルの詳細は [build.md](build.md) を参照。

```bash
cd cli/
cargo build --release --target x86_64-unknown-linux-musl
```

ビルド後、`cjobctl cli deploy` で PVC にバイナリを配置する。ユーザーは `cjob update` でセルフアップデートできる。

```bash
cjobctl cli deploy --binary ./target/x86_64-unknown-linux-musl/release/cjob --version ${VERSION}
```

## Step 6: 動作確認

```bash
# コンポーネントの状態
cjobctl status

# ジョブの投入テスト
cjob add --cpu 1 --memory 1Gi -- echo "upgrade test"
cjob list
```

バージョン固有の確認項目がある場合は、PR の Test plan を参照する。

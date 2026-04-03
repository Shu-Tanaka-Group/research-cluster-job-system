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

バージョン固有の確認項目がある場合は、PR の Test plan またはバージョン固有の移行手順を参照する。

## バージョン固有の移行手順

標準手順に加えて追加の作業が必要なバージョンでは、`docs/migration/` ディレクトリにバージョン固有の移行手順を用意している。該当バージョンへの更新時は標準手順と合わせて参照すること。大きな変更がないバージョンではファイルが存在しない場合がある。

- [v1.10.0](migration/v1.10.0.md) — Prometheus メトリクス有効化、flavor ノードラベル統一

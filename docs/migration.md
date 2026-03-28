# バージョン移行手順

稼働中の CJob システムを新しいバージョンに更新する際の標準手順。変更内容に応じて不要なステップはスキップする。

## 前提条件

- リポジトリが移行先バージョンのコミットにチェックアウトされていること
- `kubectl` でクラスタにアクセスできること
- Docker でイメージのビルド・push ができること
- `cjobctl` がビルド済みであること（Step 5 で更新する場合は旧バージョンでよい）

## Step 1: K8s リソースファイルの更新

変更がある場合のみ実行する。変更の有無は `git diff <old-tag>...<new-tag> -- docs/deployment.md` で確認できる。

### 1.1 ConfigMap

設定値の追加・変更・削除がある場合:

```bash
kubectl apply -f configmaps/cjob-config.yaml
```

> ConfigMap を更新しても、既存の Pod は再起動するまで古い環境変数を使い続ける。Step 4 の再デプロイで新しい値が反映される。

### 1.2 RBAC

権限の追加・変更がある場合:

```bash
kubectl apply -f rbac/submit-api-sa.yaml
kubectl apply -f rbac/dispatcher-sa.yaml
```

### 1.3 postgres-schema ConfigMap

新しいテーブルやインデックスの追加がある場合:

```bash
kubectl apply -f configmaps/postgres-schema.yaml
```

> `postgres-schema` は PostgreSQL 初回起動時にのみ実行される。既存の DB には Step 2 の `cjobctl db migrate` で反映する。

### 1.4 その他

NetworkPolicy、Kyverno ポリシー等に変更がある場合は個別に適用する。

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
docker tag yusekiya/cjob-submit-api:#{OLD_VERSION} yusekiya/cjob-submit-api:#{VERSION}
docker push yusekiya/cjob-submit-api:#{VERSION}
```

## Step 4: コンポーネントの再デプロイ

イメージまたは Deployment YAML に変更があるコンポーネントのみ再デプロイする。

**デプロイ順序に注意**: コンポーネント間にデータ依存がある場合は、データを生産する側を先にデプロイする。例えば Watcher が DB にデータを書き込み、Dispatcher や Submit API がそのデータを参照する場合は Watcher を最初にデプロイする。依存がない場合は順序を問わない。

```bash
read -r VERSION < VERSION

# Watcher
kubectl set image deployment/watcher -n cjob-system watcher=yusekiya/cjob-watcher:${VERSION}
kubectl rollout status deployment/watcher -n cjob-system

# Dispatcher
kubectl set image deployment/dispatcher -n cjob-system dispatcher=yusekiya/cjob-dispatcher:${VERSION}
kubectl rollout status deployment/dispatcher -n cjob-system

# Submit API
kubectl set image deployment/submit-api -n cjob-system submit-api=yusekiya/cjob-submit-api:${VERSION}
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

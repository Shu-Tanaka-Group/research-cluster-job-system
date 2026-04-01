# ビルド手順

## 概要

CJob のサーバーサイドコンポーネントは 3 つの Docker イメージで構成される。
すべて `server/` ディレクトリをビルドコンテキストとしてビルドする。

| イメージ | Dockerfile | 用途 |
|---|---|---|
| `your-registry/cjob-submit-api` | `server/Dockerfile.api` | Submit API |
| `your-registry/cjob-dispatcher` | `server/Dockerfile.dispatcher` | Dispatcher |
| `your-registry/cjob-watcher` | `server/Dockerfile.watcher` | Watcher |

ベースイメージはすべて `python:3.12-slim`。

## 前提条件

- Docker がインストールされていること
- イメージレジストリへの push 権限があること（push する場合）

## ビルド

リポジトリルートで実行する。

```bash
# Submit API
docker build -t your-registry/cjob-submit-api:latest -f server/Dockerfile.api server/

# Dispatcher
docker build -t your-registry/cjob-dispatcher:latest -f server/Dockerfile.dispatcher server/

# Watcher
docker build -t your-registry/cjob-watcher:latest -f server/Dockerfile.watcher server/
```

### バージョンタグ付きでビルドする場合

```bash
read -r VERSION < VERSION

docker build -t your-registry/cjob-submit-api:${VERSION} -f server/Dockerfile.api server/
docker build -t your-registry/cjob-dispatcher:${VERSION} -f server/Dockerfile.dispatcher server/
docker build -t your-registry/cjob-watcher:${VERSION} -f server/Dockerfile.watcher server/
```

## レジストリへの push

```bash
docker push your-registry/cjob-submit-api:latest
docker push your-registry/cjob-dispatcher:latest
docker push your-registry/cjob-watcher:latest

# バージョンタグ指定
docker push your-registry/cjob-submit-api:${VERSION}
docker push your-registry/cjob-dispatcher:${VERSION}
docker push your-registry/cjob-watcher:${VERSION}
```

## CLI（Rust）のビルド

CLI は Docker イメージではなく、シングルバイナリとしてビルドする。
ユーザーは GitHub Releases からダウンロードして `/home/jovyan/.local/bin/` に配置する。

### ローカルビルド

```bash
cd cli/
cargo build --release
```

ビルド成果物は `cli/target/release/cjob` に生成される。

### クロスコンパイル（macOS → Linux）

開発マシンが macOS の場合、K8s Pod 内で動作する Linux バイナリを生成するにはクロスコンパイルが必要。

`reqwest` の TLS バックエンド（rustls）が依存する `ring` クレートは C/アセンブリコードを含むため、単純に `--target x86_64-unknown-linux-gnu` を指定するだけではビルドに失敗する。以下のいずれかの方法を使用する。

#### 方法 1: musl ターゲット + クロスコンパイラ（推奨）

静的リンクされたバイナリを生成する方法。glibc に依存しないため、配布先の Linux ディストリビューションを問わない。

```bash
# musl クロスコンパイラのインストール（初回のみ）
brew install filosottile/musl-cross/musl-cross
rustup target add x86_64-unknown-linux-musl

# リンカの設定（初回のみ）
mkdir -p cli/.cargo
cat > cli/.cargo/config.toml << 'EOF'
[target.x86_64-unknown-linux-musl]
linker = "x86_64-linux-musl-gcc"
EOF

# ビルド
cd cli/
cargo build --release --target x86_64-unknown-linux-musl
```

成果物は `cli/target/x86_64-unknown-linux-musl/release/cjob` に生成される。

#### 方法 2: cross を使う

[cross](https://github.com/cross-rs/cross) は Docker コンテナ内でクロスコンパイルを行うツール。`docker` コマンドが使用可能で Docker デーモンが動作している必要がある。

> **注意**: Apple Silicon Mac では `cross` 0.2.5 時点でホストツールチェインの解決に失敗する既知の問題がある（`stable-x86_64-unknown-linux-gnu` をインストールしようとしてエラーになる）。その場合は方法 1 を使用すること。

```bash
# cross のインストール（初回のみ）
cargo install cross

# ビルド（Docker が起動している必要がある）
cd cli/
cross build --release --target x86_64-unknown-linux-gnu
```

成果物は `cli/target/x86_64-unknown-linux-gnu/release/cjob` に生成される。

## 管理 CLI（cjobctl）のビルド

`cjobctl` は管理者のローカル PC で動作する管理用 CLI である。DB に直接接続し、K8s API を利用する。

### ビルド

```bash
cd ctl/
cargo build --release
```

ビルド成果物は `ctl/target/release/cjobctl` に生成される。

### 設定

`~/.config/cjobctl/config.toml` を作成する。

```toml
[database]
database = "cjob"
user = "cjob"
password = "xxx"

[kubernetes]
namespace = "cjob-system"
```

DB コマンド実行時は `kubectl port-forward` が自動的に起動・終了される（`kubectl` が PATH に必要）。

```bash
kubectl port-forward svc/postgres 5432:5432 -n cjob-system
```

## Job Pod のランタイムイメージ

Job Pod が使用するランタイムイメージ（`your-registry/cjob-jupyter:2.1.0` 等）は本リポジトリの管理対象外である。
JupyterHub の User Pod と同一のイメージが使用される（CLI が `JUPYTER_IMAGE` 環境変数から自動取得する）。

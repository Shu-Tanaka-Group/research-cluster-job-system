# ビルド手順

## 概要

CJob のサーバーサイドコンポーネントは 3 つの Docker イメージで構成される。
すべて `server/` ディレクトリをビルドコンテキストとしてビルドする。

| イメージ | Dockerfile | 用途 |
|---|---|---|
| `yusekiya/cjob-submit-api` | `server/Dockerfile.api` | Submit API |
| `yusekiya/cjob-dispatcher` | `server/Dockerfile.dispatcher` | Dispatcher |
| `yusekiya/cjob-watcher` | `server/Dockerfile.watcher` | Watcher |

ベースイメージはすべて `python:3.12-slim`。

## 前提条件

- Docker がインストールされていること
- イメージレジストリへの push 権限があること（push する場合）

## ビルド

リポジトリルートで実行する。

```bash
# Submit API
docker build -t yusekiya/cjob-submit-api:latest -f server/Dockerfile.api server/

# Dispatcher
docker build -t yusekiya/cjob-dispatcher:latest -f server/Dockerfile.dispatcher server/

# Watcher
docker build -t yusekiya/cjob-watcher:latest -f server/Dockerfile.watcher server/
```

### バージョンタグ付きでビルドする場合

```bash
VERSION=0.1.0

docker build -t yusekiya/cjob-submit-api:${VERSION} -f server/Dockerfile.api server/
docker build -t yusekiya/cjob-dispatcher:${VERSION} -f server/Dockerfile.dispatcher server/
docker build -t yusekiya/cjob-watcher:${VERSION} -f server/Dockerfile.watcher server/
```

## レジストリへの push

```bash
docker push yusekiya/cjob-submit-api:latest
docker push yusekiya/cjob-dispatcher:latest
docker push yusekiya/cjob-watcher:latest
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

### クロスコンパイル（Linux 向け）

開発マシンが macOS の場合、K8s Pod 内で動作する Linux バイナリを生成するにはクロスコンパイルが必要。

```bash
# ターゲット追加（初回のみ）
rustup target add x86_64-unknown-linux-gnu

# ビルド
cd cli/
cargo build --release --target x86_64-unknown-linux-gnu
```

成果物は `cli/target/x86_64-unknown-linux-gnu/release/cjob` に生成される。

## Job Pod のランタイムイメージ

Job Pod が使用するランタイムイメージ（`yusekiya/stg-jupyter:2.1.0` 等）は本リポジトリの管理対象外である。
JupyterHub の User Pod と同一のイメージが使用される（CLI が `JUPYTER_IMAGE` 環境変数から自動取得する）。

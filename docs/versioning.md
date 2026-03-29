# バージョン管理

## 概要

CJob は単一の `VERSION` ファイルでプロジェクト全体のバージョンを管理する。`VERSION` ファイルを更新し、同期スクリプトを実行することで、各コンポーネントのバージョンが一括で揃う。

## バージョン管理の仕組み

| ファイル | 役割 |
|---|---|
| `VERSION` | プロジェクトのバージョンの原本（単一の semver 文字列） |
| `scripts/sync-version.sh` | `VERSION` の値を各コンポーネントの設定ファイルに同期する |

### 同期対象

`scripts/sync-version.sh` は以下のファイルの `version` フィールドを更新する。

| ファイル | コンポーネント |
|---|---|
| `server/pyproject.toml` | Submit API / Dispatcher / Watcher |
| `cli/Cargo.toml` | cjob CLI |
| `ctl/Cargo.toml` | cjobctl |

## バージョン更新手順

### Step 1: VERSION ファイルの更新

```bash
echo "X.Y.Z" > VERSION
```

### Step 2: 各コンポーネントへのバージョン同期

```bash
bash scripts/sync-version.sh
```

`sync-version.sh` は冪等であり、既に一致している場合は何もしない。

### Step 3: ロックファイルの更新

バージョン番号の変更をロックファイルに反映する。

```bash
# CLI
cd cli/ && cargo generate-lockfile && cd ..

# 管理 CLI
cd ctl/ && cargo generate-lockfile && cd ..

# Server
cd server/ && uv lock && cd ..
```

### Step 4: コミット

バージョン更新は 1 コミットにまとめる。対象ファイル:

- `VERSION`
- `server/pyproject.toml`
- `cli/Cargo.toml`
- `cli/Cargo.lock`
- `ctl/Cargo.toml`
- `ctl/Cargo.lock`
- `server/uv.lock`

## 備考

- バージョン形式は [Semantic Versioning](https://semver.org/) に従う
- `sync-version.sh` は pre-commit hook としても利用可能（[Git 運用規則](git_conventions.md) 参照）
- バージョン更新後の移行作業（ビルド・デプロイ・DB マイグレーション等）は [バージョン移行手順](migration.md) を参照

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
| `k8s/overlay-example/kustomization.yaml` | overlay サンプルのイメージタグ |

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

### Step 4: 移行手順の記載漏れ確認

前バージョンのタグからの差分を確認し、`docs/migration/unreleased.md` に記載すべき移行手順が漏れていないか確認する。

```bash
# 前バージョンのタグと現在の差分を確認
git diff <old-tag>..HEAD --stat

# 特に以下の変更を重点的に確認する
git diff <old-tag>..HEAD -- k8s/base/configmap-cjob-config.yaml  # ConfigMap のキー追加・変更
git diff <old-tag>..HEAD -- server/src/cjob/models.py            # DB スキーマの変更
git diff <old-tag>..HEAD -- docs/architecture/kueue.md           # Kueue リソースの変更
git diff <old-tag>..HEAD -- docs/deployment.md                   # デプロイ手順の変更
```

以下に該当する変更がある場合、`docs/migration/unreleased.md` に移行手順を追加する（ファイルが存在しない場合は新規作成する）:

- ConfigMap のキー追加・デフォルト値の変更（overlay への反映が必要）
- DB スキーマの変更（`cjobctl db migrate` の実行が必要）
- Kueue リソース（ResourceFlavor / ClusterQueue）の設定変更
- ノードラベル・Taint の変更
- RBAC や Kyverno ポリシーの変更
- 手動での設定変更やデータ移行が必要なその他の変更

### Step 5: 移行手順書のリネーム

`docs/migration/unreleased.md` が存在する場合、バージョン名にリネームする。

```bash
mv docs/migration/unreleased.md docs/migration/vX.Y.Z.md
```

`docs/migration.md` 末尾のリンクも更新する（`unreleased` → `vX.Y.Z`）。

リネーム後、以下のテンプレートを使って新しい `docs/migration/unreleased.md` を作成する。

````markdown
# 未リリース移行手順

本ファイルは **次回リリース向け** の移行手順を記載する作業ファイルである。リリース時にバージョン名（例: `v1.11.0.md`）にリネームし、新しい `unreleased.md` を作成する（[versioning.md](../versioning.md) 参照）。

[標準移行手順](../migration.md) に加えて次回リリース固有の移行手順がある場合は以下に追記する。
````

`unreleased.md` に記載がない（大きな変更がない）場合は、Step 5 全体（リネーム・再作成）をスキップしてよい。

### Step 6: コミット

バージョン更新は 1 コミットにまとめる。対象ファイル:

- `VERSION`
- `server/pyproject.toml`
- `cli/Cargo.toml`
- `cli/Cargo.lock`
- `ctl/Cargo.toml`
- `ctl/Cargo.lock`
- `server/uv.lock`
- `k8s/overlay-example/kustomization.yaml`
- `docs/migration/vX.Y.Z.md`（リネームした場合）
- `docs/migration/unreleased.md`（テンプレートから再作成した場合）
- `docs/migration.md`（リンクを更新した場合）

## 備考

- バージョン形式は [Semantic Versioning](https://semver.org/) に従う
- `sync-version.sh` は pre-commit hook としても利用可能（[Git 運用規則](git_conventions.md) 参照）
- バージョン更新後の移行作業（ビルド・デプロイ・DB マイグレーション等）は [バージョン移行手順](migration.md) を参照

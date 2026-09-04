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
| `k8s/overlay-example/kustomization.yaml` | overlay サンプルのイメージタグ（`newTag`）と base の参照バージョン（`?ref=`） |

## バージョン更新手順

以下の Step 1〜7 は `/release` skill で実行できる。skill はバージョン番号・リンク一覧の要約文・push についてユーザーの確認を取り、Step 4 では `/deploy-runbook` skill に委譲する。タグの作成と push（Step 7 の最後）は不可逆な操作のため skill では行わない。

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
cd cli/ && cargo update -p cjob && cd ..

# 管理 CLI
cd ctl/ && cargo update -p cjobctl && cd ..

# Server
cd server/ && uv lock && cd ..
```

いずれも自パッケージのバージョン行だけを書き換える。`cargo generate-lockfile` はロックファイルを全体再生成し、全依存を最新互換版へ更新してしまうため使わない。依存の更新はリリースとは別の PR で行い、リリースコミットに混ぜない。

### Step 4: 移行手順の記載漏れ確認

前バージョンのタグからの差分を確認し、`docs/migration/unreleased.md` に記載すべき移行手順が漏れていないか確認する。

`/deploy-runbook` skill を使うと、以下の確認を自動化できる（差分からの導出と `unreleased.md` の突き合わせ、記載漏れの報告）。

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

**各節の書式:** 見出しの直後に、その手順を必要とした変更の出所を 1 行で併記する。

```markdown
## <手順の見出し>

> 関連: issue #<番号>
```

デプロイ時の突き合わせ（`/deploy-runbook` skill）が、`unreleased.md` の各節と実際の変更を機械的に対応付けるために使う。PR 番号は PR 本文の `Closes #<番号>` から辿れるため必須ではないが、確定していれば `> 関連: issue #<番号> / PR #<番号>` と併記してよい。issue を伴わない変更は `> 関連: PR #<番号>`、PR も伴わない直接コミットは `> 関連: <コミットハッシュ>` とする。

### Step 5: 移行手順書のリネーム

`docs/migration/unreleased.md` に固有の移行手順が記載されている場合、ファイル冒頭のタイトルを修正し、 `unreleased.md` 作成に関する指示文を削除、そしてファイルをバージョン名にリネームする。

```bash
mv docs/migration/unreleased.md docs/migration/vX.Y.Z.md
mv docs_en/migration/unreleased.md docs_en/migration/vX.Y.Z.md
```

**日本語版と英語版の両方を同じように処理する。** `docs_en/` 側を落とすと、英語版の `unreleased.md` に前バージョンの内容が残り続ける。

`docs/migration.md` と `docs_en/migration.md` の末尾にある「バージョン固有の移行手順」のリンク一覧に `vX.Y.Z` の行を追加する（`unreleased` へのリンクは元から無いため、置換ではなく追加になる）。要約文はそのバージョンの主な移行作業を 1 行でまとめる。

リネーム後、以下のテンプレートを使って新しい `docs/migration/unreleased.md` と `docs_en/migration/unreleased.md` を作成する。

````markdown
# 未リリース移行手順

本ファイルは **次回リリース向け** の移行手順を記載する作業ファイルである。リリース時にバージョン名（例: `v1.11.0.md`）にリネームし、新しい `unreleased.md` を作成する（[versioning.md](../versioning.md) 参照）。

[標準移行手順](../migration.md) に加えて次回リリース固有の移行手順がある場合は以下に追記する。
````

英語版のテンプレートは既存の `docs_en/migration/unreleased.md` の冒頭部（自動翻訳の注記を含む）をそのまま使う。

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
- `docs/migration/vX.Y.Z.md` / `docs_en/migration/vX.Y.Z.md`（リネームした場合）
- `docs/migration/unreleased.md` / `docs_en/migration/unreleased.md`（テンプレートから再作成した場合）
- `docs/migration.md` / `docs_en/migration.md`（リンクを更新した場合）

### Step 7: リリースブランチ・PR・タグ

Step 6 のコミットを `release/vX.Y.Z` ブランチに載せ、PR を作成してマージした後にタグを打つ（[git_conventions.md](git_conventions.md) §1 参照）。

```bash
git checkout -b release/vX.Y.Z
# Step 1〜6 を実施してコミット
git push -u origin release/vX.Y.Z
```

PR は `/create-pr` skill で作成する。タイトルは `Bump version to X.Y.Z`、本文には以下を含める。

- `## Summary` — 前バージョン以降の主な変更（issue 番号を添える）
- `## Post-apply actions` — `docs/migration/vX.Y.Z.md` の内容を要約し、詳細はそのファイルへリンクする
- `## Test plan` — `sync-version.sh` の冪等性、各ファイルのバージョン一致、ロックファイルの更新、移行手順書のリネームとリンク追加、`unreleased.md` のテンプレート復帰

マージ後、`main` を最新化してマージコミットにタグを付けて push する。**タグ名は `v` 接頭辞なし**（ブランチ名は `v` あり、タグ名は `v` なし）。

```bash
git checkout main && git pull
git tag X.Y.Z
git push origin X.Y.Z
```

タグの push により [`.github/workflows/release.yml`](../.github/workflows/release.yml) が起動し、cjob CLI（`cjob-linux-x86_64`）をビルドして GitHub Release を作成する。プレリリース（`X.Y.Z-alpha.N` / `-beta.N` / `-rc.N`）は自動的に prerelease として扱われ、latest にはならない。

タグの push は GitHub Release を自動生成する不可逆な操作である。PR がマージ済みであること、タグ名が `VERSION` の内容と一致することを確認してから実行する。

## 備考

- バージョン形式は [Semantic Versioning](https://semver.org/) に従う
- `sync-version.sh` は pre-commit hook としても利用可能（[Git 運用規則](git_conventions.md) 参照）
- バージョン更新後の移行作業（ビルド・デプロイ・DB マイグレーション等）は [バージョン移行手順](migration.md) を参照

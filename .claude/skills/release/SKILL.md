---
name: release
description: 新しいリリースバージョンを確定する準備作業を実行する。VERSION の更新とコンポーネントへの同期、ロックファイルの更新、移行手順の記載漏れ確認（deploy-runbook skill に委譲）、移行手順書のリネームとリンク更新（日本語版・英語版の両方）、リリースブランチと PR の作成までを担う。タグの作成と push は行わない。
---

# リリース準備

`docs/versioning.md` の Step 1〜7 を実行し、リリース PR の作成までを行う skill。

**方針:**

- **設計書が正本である。** 手順の詳細は [versioning.md](../../../docs/versioning.md) と [git_conventions.md](../../../docs/git_conventions.md) にある。この skill は実行順序と、落としやすい箇所の明示を担う。両者が食い違う場合は設計書を正とし、skill 側を直す
- **日本語版と英語版を対称に扱う。** `docs/migration/` の操作は必ず `docs_en/migration/` にも同じ操作を行う。過去に手順書へ明記されておらず暗黙知になっていた箇所であり、最も落としやすい
- **タグは打たない。** タグの push は `.github/workflows/release.yml` を起動して GitHub Release を自動生成する不可逆な操作であり、この skill の範囲外とする。最後に手順を提示して終わる
- **バージョン番号と要約文はユーザーが決める。** 提案はするが、確認なしに確定しない

## 引数

```
$ARGUMENTS
```

- 引数なし: バージョン番号を提案してユーザーに確認する
- `<X.Y.Z>`: そのバージョンでリリース準備を行う（プレリリースは `X.Y.Z-{alpha,beta,rc}.N`）

## Step 0: 事前条件の確認とバージョン番号の決定

以下を確認する。満たさない場合は理由を示して中止する。

- 現在のブランチが `main` で、`origin/main` と同期していること
- 作業ツリーがクリーンであること（未コミットの変更がないこと）

```bash
git status -sb
git log --oneline -1
```

次にバージョン番号を決める。現在の `VERSION` と、直近タグ以降の変更内容から semver に従って提案する。

```bash
cat VERSION
git tag --sort=-creatordate | head -2
gh pr list --state merged --base main --limit 20 --json number,title,mergedAt
```

| 種別 | 判断基準 |
|---|---|
| major | 後方互換を壊す変更（CLI インターフェース・API 契約・DB スキーマの破壊的変更） |
| minor | 後方互換のある機能追加 |
| patch | 修正のみ |

**提案したバージョン番号は必ずユーザーに確認を取る。** 引数で指定された場合も、semver として妥当か（前バージョンより大きいか、形式が正しいか）を検証してから進む。

`scripts/sync-version.sh` が受け付ける形式は `X.Y.Z` または `X.Y.Z-{alpha,beta,rc}.N` に限られる。

## Step 1〜3: バージョンの更新と同期

```bash
echo "X.Y.Z" > VERSION
bash scripts/sync-version.sh
```

`sync-version.sh` は `server/pyproject.toml`（PEP 440 形式に変換）・`cli/Cargo.toml`・`ctl/Cargo.toml`・`k8s/overlay-example/kustomization.yaml` を更新し、更新したファイルを `git add` する。冪等であり、既に一致していれば何もしない。

続いてロックファイルを更新する。

```bash
cd cli && cargo generate-lockfile && cd ..
cd ctl && cargo generate-lockfile && cd ..
cd server && uv lock && cd ..
```

## Step 4: 移行手順の記載漏れ確認

**`deploy-runbook` skill を呼ぶ。** 引数なしで実行すれば、直近タグ `..HEAD` が対象範囲となり、`docs/migration/unreleased.md` を読む（この時点ではまだリネーム前なので正しい読み先になる）。

`[GAP]` が報告された場合は、**Step 5 のリネームより前に** `docs/migration/unreleased.md` と `docs_en/migration/unreleased.md` を修正する。リネーム後に気付くと、リリース済みのバージョン別ファイルへの追記になり扱いが面倒になる。

`[NOREF]`（`> 関連:` 行が無い節）が報告された場合も同様にここで補う。

## Step 5: 移行手順書のリネーム

`unreleased.md` に節（`## `）が 1 つも無い場合は Step 5 全体をスキップしてよい。以下は節がある場合の手順。

**日本語版と英語版の両方に対して同じ操作を行う。**

1. **リネーム**

   ```bash
   git mv docs/migration/unreleased.md docs/migration/vX.Y.Z.md
   git mv docs_en/migration/unreleased.md docs_en/migration/vX.Y.Z.md
   ```

2. **リネームしたファイルの冒頭を書き換える**

   | 対象 | 変更前 | 変更後 |
   |---|---|---|
   | 日本語版タイトル | `# 未リリース移行手順` | `# vX.Y.Z 移行手順` |
   | 英語版タイトル | `# Unreleased Migration Procedures` | `# vX.Y.Z Migration Procedures` |
   | 英語版の翻訳注記のリンク | `../../docs/migration/unreleased.md` | `../../docs/migration/vX.Y.Z.md` |

   あわせて、`unreleased.md` の作成に関する指示文（「本ファイルは **次回リリース向け** の…」と「[標準移行手順] に加えて次回リリース固有の移行手順がある場合は以下に追記する」）を削除し、`[標準移行手順](../migration.md) に加えて以下の手順を実施する。` に置き換える。英語版も同様。

3. **リンク一覧に追加**

   `docs/migration.md` と `docs_en/migration.md` の末尾「バージョン固有の移行手順」のリストに、最新が先頭に来るよう 1 行追加する。

   ```markdown
   - [vX.Y.Z](migration/vX.Y.Z.md) — <そのバージョンの主な移行作業を 1 行で>
   ```

   **要約文はユーザーに確認する。** 既存行（`v1.15.0` 等）の粒度に揃えること。

4. **新しい `unreleased.md` を作成する**

   日本語版は versioning.md Step 5 のテンプレートを使う。英語版はリネーム前の `docs_en/migration/unreleased.md` の冒頭部（自動翻訳の注記を含む）をそのまま使い、翻訳注記のリンクは `../../docs/migration/unreleased.md` に戻す。

## Step 6: 検証とコミット

ロックファイルの再生成でビルドが壊れていないことを確認する。

```bash
cd cli && cargo test && cd ..
cd ctl && cargo test && cd ..
cd server && uv run --extra test --extra api --with httpx python -m pytest tests/ -q && cd ..
```

`sync-version.sh` の冪等性と各ファイルのバージョン一致を確認する。

```bash
bash scripts/sync-version.sh   # 2 回目は何も出力しないこと
git diff --stat                # 差分が増えないこと
```

確認できたら 1 コミットにまとめる。対象ファイルは versioning.md Step 6 の一覧を参照する（`docs_en/` 側を含めること）。コミットメッセージのタイトルは `Bump version to X.Y.Z` とする。

## Step 7: ブランチと PR

```bash
git checkout -b release/vX.Y.Z
git push -u origin release/vX.Y.Z
```

**push の前にユーザーの承認を取る。**

PR は `/create-pr` skill で作成する。タイトルは `Bump version to X.Y.Z`。本文の構成は versioning.md Step 7 を参照する（Summary / Post-apply actions / Test plan）。`## Post-apply actions` は `docs/migration/vX.Y.Z.md` の内容を要約し、詳細はそのファイルへリンクする。

## Step 8: タグ（この skill では実行しない）

PR がマージされたら、ユーザーが以下を実行する。**この skill はコマンドを提示するだけで、実行しない。**

```bash
git checkout main && git pull
git tag X.Y.Z          # v 接頭辞なし
git push origin X.Y.Z
```

タグ名が `VERSION` の内容と一致していること、PR がマージ済みであることを確認するようユーザーに伝える。タグの push により GitHub Release が自動生成され、取り消しには Release とタグの削除が必要になる。

## 注意事項

- `docs_en/` 側のリネーム・リンク追加・翻訳注記のリンク修正は、過去に手順書へ明記されておらず落としやすい。Step 5 の各操作は日本語版・英語版を対にして実施し、完了後に `git status` で 6 ファイル（`vX.Y.Z.md` × 2、`unreleased.md` × 2、`migration.md` × 2）が変更対象になっていることを確認する
- Step 4 の `[GAP]` 修正は Step 5 のリネームより前に行う
- バージョン番号・リンク一覧の要約文・push は、いずれもユーザーの確認を経てから確定する
- タグの作成と push は行わない
- ファイル列挙・内容検索には Glob / Grep ツールを使う。bash の `find` / `grep` を使わない

# Git 運用規則

## 1. ブランチ命名規則

issue に紐づく変更:

```
<変更のタイプ>/#<issue番号>_<タイトル>
```

issue に紐づかない変更:

```
<変更のタイプ>/<タイトル>
```

- タイトルはケバブケース（小文字、単語区切りはハイフン）で記述する
- issue に紐づかない軽微な変更は main に直接コミットしてよい。ブランチを切って PR にしてもよく、その場合は issue 番号を省いた上記の形式を使う
- `release` タイプは例外的に `release/v<バージョン>` 形式とする（例: `release/v1.15.0`）。手順は [versioning.md](versioning.md) §Step 7 を参照

### 変更のタイプ

| タイプ | 用途 |
|---|---|
| `feature` | 新機能の追加 |
| `fix` | バグ修正 |
| `docs` | ドキュメントのみの変更 |
| `refactor` | 機能変更を伴わないコードの改善 |
| `test` | テストの追加・修正 |
| `release` | バージョン更新（リリース準備） |

### 例

```
feature/#2_gap-filling-dispatch-for-large-jobs
fix/#15_cancel-race-condition
docs/#8_update-deployment-guide
docs/overlay-remote-base          # issue に紐づかない変更
release/v1.15.0
```

### タグ命名規則

リリースタグは `v` 接頭辞を**付けない**（例: `1.15.0`）。ブランチ名は `release/v1.15.0` と `v` を付けるため、両者で非対称になる点に注意する。タグ形式は [`.github/workflows/release.yml`](../.github/workflows/release.yml) の `on.push.tags` パターン（`[0-9]+.[0-9]+.[0-9]+*`）と一致させる必要がある。

## 2. コミットメッセージ

### フォーマット

```
<タイトル行>

<本文（任意）>

Co-Authored-By: <モデル名> <noreply@anthropic.com>
```

- タイトル行は英語で記述する
- タイトル行は動詞の原形で始める（Add / Fix / Update / Implement / Remove 等）
- issue に紐づくコミットはタイトル末尾に `(#<issue番号>)` を付ける
- 本文は日本語でも英語でもよい。変更の目的（why）を記述する
- Claude が作成したコミットには `Co-Authored-By` 行を付ける。`<モデル名>` には実行時のモデル名を使用する（例: `Claude Opus 4.6 (1M context)`, `Claude Sonnet 4.6` 等）

### タイトル行の動詞の使い分け

| 動詞 | 用途 |
|---|---|
| Add | 新しいファイル・機能・テストの追加 |
| Implement | 設計済みの機能の実装 |
| Update | 既存の機能・ドキュメントの更新 |
| Fix | バグ修正、設計書と実装の不整合修正 |
| Remove | ファイル・機能の削除 |
| Bump | バージョン番号の更新 |

### 例

```
Add job execution time limit (activeDeadlineSeconds) to design docs

巨大なリソースを要求するJobがBestEffortFIFOの下でstarvationされる問題への
対策として、ジョブ実行時間上限を導入する。

Co-Authored-By: <モデル名> <noreply@anthropic.com>
```

```
Implement gap filling dispatch logic (#2)

滞留ジョブ検知と隙間充填フィルタリングを追加。

Co-Authored-By: <モデル名> <noreply@anthropic.com>
```

## 3. Pull Request

- タイトルは短く（70文字以内）
- body に `## Summary`（箇条書き）と `## Test plan`（チェックリスト）を含める
- 変更の適用後に手動操作が必要な場合は `## Post-apply actions` セクションを追加する
- issue をクローズする場合は body に `Closes #<issue番号>` を記述する

## 4. main への直接コミット

以下の場合は issue・ブランチ・PR を作成せず main に直接コミットしてよい。

- ドキュメントの軽微な修正（誤字、構成変更、設計変更を伴わないもの）
- テストの追加（機能変更を伴わないもの）
- 設定値の調整

# Git 運用規則

## 1. ブランチ命名規則

```
<変更のタイプ>/#<issue番号>_<タイトル>
```

- タイトルはケバブケース（小文字、単語区切りはハイフン）で記述する
- issue に紐づかない軽微な変更は main に直接コミットしてよい

### 変更のタイプ

| タイプ | 用途 |
|---|---|
| `feature` | 新機能の追加 |
| `fix` | バグ修正 |
| `docs` | ドキュメントのみの変更 |
| `refactor` | 機能変更を伴わないコードの改善 |
| `test` | テストの追加・修正 |

### 例

```
feature/#2_gap-filling-dispatch-for-large-jobs
fix/#15_cancel-race-condition
docs/#8_update-deployment-guide
```

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
- issue をクローズする場合は body に `Closes #<issue番号>` を記述する

## 4. main への直接コミット

以下の場合は issue・ブランチ・PR を作成せず main に直接コミットしてよい。

- ドキュメントの軽微な修正（誤字、構成変更）
- テストの追加（機能変更を伴わないもの）
- 設定値の調整

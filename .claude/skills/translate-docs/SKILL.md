---
name: translate-docs
description: Translate Japanese documentation in docs/ to English and save to docs_en/. Use this skill whenever docs/ files are modified to keep English documentation in sync.
---

# Translate Documentation to English

`docs/` 配下の日本語ドキュメントを英語に翻訳し、`docs_en/` 配下に同じディレクトリ構成で配置する。
`README.md` は例外として、プロジェクトルートに `README.en.md` として配置する。

## 対象の決定

引数でファイルパスが指定された場合はそのファイルのみを対象とする。
引数がない場合は、現在のブランチで変更された `docs/` 配下のファイルを `git diff` で検出し、対象とする。

```
$ARGUMENTS
```

## 手順

1. 対象ファイルを特定する
   - 引数があればそのファイルを対象とする（例: `docs/architecture/cli.md`）
   - 引数がなければ `git diff --name-only main -- docs/ README.md` で変更されたファイルを検出する
2. 対象ファイルごとに以下を実行する:
   a. 日本語版ファイルを読む
   b. 英語版の対応パスにファイルが既にあれば読む
   c. 英語に翻訳して書き出す
      - `docs/` 配下のファイル → `docs_en/` 配下の同じパスに配置
      - `README.md` → プロジェクトルートの `README.en.md` に配置
3. 翻訳結果をユーザーに報告する

## 翻訳ルール

- Markdown の構造（見出し、リスト、テーブル、コードブロック、リンク）はそのまま維持する
- コードブロック内のコード・コマンドは翻訳しない
- コードブロック内の日本語コメントは英語に翻訳する
- 技術用語（Kubernetes, Kueue, PostgreSQL, FastAPI 等）はそのまま使う
- 日本語版内のファイルパスへのリンクは `docs_en/` 内の対応パスに書き換える
  - 例: `docs/architecture/cli.md` → `docs_en/architecture/cli.md`
  - `README.md` へのリンクは `README.en.md` に書き換える
  - 上記以外の `docs/` 外へのリンクはそのまま維持する
- 固有名詞（CJob, cjob, cjobctl 等）はそのまま使う
- 既存の英語版がある場合は、変更された部分のみ更新するのではなく、全体を再翻訳する（用語や文体の一貫性を保つため）
- 翻訳したファイルの冒頭（タイトルの前）に以下の注意書きを挿入する:
  ```
  > *This document was auto-translated from the [Japanese original](<日本語版への相対パス>) by Claude and may contain errors. Refer to the original for the authoritative content.*
  ```
  - `<日本語版への相対パス>` は翻訳先ファイルから見た日本語版ファイルへの相対パスとする
    - 例: `docs_en/architecture/cli.md` → `../../docs/architecture/cli.md`
    - 例: `README.en.md` → `README.md`

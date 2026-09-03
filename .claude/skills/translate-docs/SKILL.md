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

0. **[glossary.md](glossary.md) を読む。** 訳語はこの用語集で固定する。差分翻訳における一貫性はこの用語集が担保するため、省略しない
1. 対象ファイルを特定する
   - 引数があればそのファイルを対象とする（例: `docs/architecture/cli.md`）
   - 引数がなければ `git diff --name-only main -- docs/ README.md` で変更されたファイルを検出する
2. ファイルごとに翻訳方式を決める（下記「翻訳方式の選択」）
3. 対象ファイルごとに以下を実行する:
   a. 日本語版ファイルを読む（差分翻訳の場合は `git diff main -- <path>` で変更箇所も読む）
   b. 英語版の対応パスにファイルが既にあれば読む
   c. 英語に翻訳して書き出す
      - `docs/` 配下のファイル → `docs_en/` 配下の同じパスに配置
      - `README.md` → プロジェクトルートの `README.en.md` に配置
4. **用語集に無い用語を訳した場合は [glossary.md](glossary.md) に追加する**（下記「用語集の更新」）
5. **今回使った用語で `docs_en/` 全体を Grep し、既存の訳語と食い違っていないか確認する**（下記「ファイル間の一貫性確認」）
6. 翻訳結果をユーザーに報告する。用語集に追加した用語と、手順 5 で見つかった不一致があれば併せて報告する

## 翻訳方式の選択

| 方式 | 適用条件 | 内容 |
|---|---|---|
| **差分翻訳** | 既存の英語版があり、日本語版の変更が部分的（節の追加・行の修正・表への行追加など） | 変更された箇所に対応する英語版の箇所だけを更新する。日本語版の diff と同じ行数・同じ構造の差分になるのが正常 |
| **全体翻訳** | 英語版が存在しない、または日本語版が大規模に改稿された（構成変更・節の大幅な入れ替え） | ファイル全体を訳し直す |

差分翻訳を既定とする。日本語版の diff（`git diff main -- <path> --stat`）と英語版の diff の行数が大きく食い違う場合は、対応関係を取り違えている可能性があるため見直す。

**差分翻訳で文体を合わせる方法:** 変更箇所の前後にある既存の英訳を読み、同じ語彙・同じ文の運びに揃える。用語集は語彙を固定するが文体までは規定しないため、周辺の文脈を読むことで補う。

## 用語集の更新

用語集に無い用語を訳したときは、[glossary.md](glossary.md) の該当カテゴリに 1 行追加する。以下に当てはまるものを対象とする。

- このプロジェクト固有の概念（`滞留ガード` → `stall guard` 等）
- 複数のファイルに登場しうる一般語で、複数の訳し方があるもの（`控除する` → `subtract` 等）

一度しか登場しない語や、訳し方が一意に定まる語は追加しない。用語集が肥大すると参照コストが上がり、かえって守られなくなる。

## ファイル間の一貫性確認

翻訳後、今回使った主要な用語（用語集に載っている語と、新たに追加した語）で `docs_en/` 全体を Grep し、既存の訳語と食い違っていないか確認する。

```bash
grep -rn "<訳語>" docs_en/ | wc -l
grep -rn "<別の訳し方>" docs_en/ | wc -l
```

食い違いが見つかった場合は、**今回の翻訳箇所を用語集に合わせる**。既存の他ファイルの記述までこの場で直す必要はないが、件数と該当ファイルをユーザーに報告する。

この確認が必要なのは、従来の「全体翻訳」がファイル内の一貫性しか保証していなかったためである。実際に `pre-check` と `precheck`、`stall` と `stagnate` がファイル単位で分かれていた。差分翻訳に切り替えても揺れが増えないよう、この手順で担保する。

## 翻訳ルール

- Markdown の構造（見出し、リスト、テーブル、コードブロック、リンク）はそのまま維持する
- コードブロック内のコード・コマンドは翻訳しない
- コードブロック内の日本語コメントは英語に翻訳する
- 技術用語（Kubernetes, Kueue, PostgreSQL, FastAPI 等）はそのまま使う
- 英語版ドキュメント内のリンクは、対応する英語版ファイルを指すようにする
  - 同じディレクトリ階層内の相対リンクはそのまま維持する（`docs_en/` 内の相対パスとして自然に英語版を指すため）
    - 例: `docs_en/architecture/system_design.md` 内の `[resources.md](resources.md)` → そのまま（`docs_en/architecture/resources.md` を指す）
    - 例: `docs_en/architecture/system_design.md` 内の `[deployment.md](../deployment.md)` → そのまま（`docs_en/deployment.md` を指す）
  - 日本語版の絶対パス形式のリンクは `docs_en/` 内の対応パスに書き換える
    - 例: `docs/architecture/cli.md` → `docs_en/architecture/cli.md`
  - `README.md` へのリンクは `README.en.md` に書き換える
  - ドキュメント以外へのリンク（ソースコード、外部 URL 等）はそのまま維持する
  - 日本語版の見出しアンカー（`#xxx`）は英語版の見出しに合わせて英語のアンカーに書き換える
    - 例: `deployment.md#11-namespace-作成スクリプト完成版` → `deployment.md#11-namespace-creation-script-complete-version`
- 固有名詞（CJob, cjob, cjobctl 等）はそのまま使う
- 訳語は [glossary.md](glossary.md) に従う。用語集と食い違う訳語を使わない
- 既存の英語版がある場合は原則として差分翻訳とする（「翻訳方式の選択」参照）。全体を訳し直すのは、英語版が無い場合と日本語版が大規模に改稿された場合に限る
- 翻訳したファイルの冒頭（タイトルの前）に以下の注意書きを挿入する:
  ```
  > *This document was auto-translated from the [Japanese original](<日本語版への相対パス>) by Claude and may contain errors. Refer to the original for the authoritative content.*
  ```
  - `<日本語版への相対パス>` は翻訳先ファイルから見た日本語版ファイルへの相対パスとする
    - 例: `docs_en/architecture/cli.md` → `../../docs/architecture/cli.md`
    - 例: `README.en.md` → `README.md`

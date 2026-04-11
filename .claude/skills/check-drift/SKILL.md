---
name: check-drift
description: Check for divergence between design docs in docs/ and their implementations. Runs per-component checks in parallel via Explore subagents and verifies findings before reporting. Report only; no auto-fix.
---

# 設計書 ↔ 実装 乖離チェック

`docs/` 配下の設計書と対応する実装の乖離をコンポーネント単位でチェックする skill。

**方針:**
- CLAUDE.md の「設計書が正本」原則に従う。乖離が見つかった場合は原則コード側を設計書に合わせて修正すべきだが、この skill は報告のみを行い修正はユーザーの判断に委ねる。
- コンポーネント単位で並列化し、`Explore` サブエージェントに各コンポーネントのチェックを委譲する。
- サブエージェントの報告には事実誤認が含まれる場合があるため、メインエージェントが報告内容を再読して検証してから最終レポートを出す。

## 引数

```
$ARGUMENTS
```

- 引数にコンポーネント名を空白区切りで指定する（例: `watcher`、`watcher dispatcher`、`all`）
- 引数なしの場合はユーザーに対象コンポーネントを確認する
- 無効なコンポーネント名が指定されたら、下記マッピング表を提示して再確認する

## コンポーネント ↔ 設計書・コードのマッピング

| コンポーネント | 設計書 | コード |
|---|---|---|
| `api` | `docs/architecture/api.md` | `server/src/cjob/api/` |
| `dispatcher` | `docs/architecture/dispatcher.md` | `server/src/cjob/dispatcher/` |
| `watcher` | `docs/architecture/watcher.md` | `server/src/cjob/watcher/` |
| `database` | `docs/architecture/database.md` | `server/src/cjob/models.py`、マイグレーション |
| `cli` | `docs/architecture/cli.md` | `cli/src/` |
| `cjobctl` | `docs/architecture/cjobctl.md` | `ctl/src/` |
| `kueue` | `docs/architecture/kueue.md` | K8s マニフェスト、dispatcher の Kueue 連携 |
| `resources` | `docs/architecture/resources.md` | `server/src/cjob/config.py`、`dispatcher/k8s_job.py`、`dispatcher/resource_utils.py` |
| `monitoring` | `docs/architecture/monitoring.md` | `server/src/cjob/metrics.py`、`k8s/base/grafana/*.json`、`k8s/base/prometheus-operator/*.yaml`、deployment の Prometheus 関連 |
| `system` | `docs/architecture/system_design.md` | システム全体（横断的に参照） |

複数の設計書を参照するコンポーネントはすべての設計書と実装の一致をチェックする。

## 手順

### Step 1: スコープ決定

1. 引数を解析して対象コンポーネントを決定する
2. 引数がなければユーザーに対象を確認する
3. `all` が指定されたら上記マッピング表のすべてのコンポーネントを対象とする

### Step 2: サブエージェントへの並列委譲

対象コンポーネントそれぞれに対して `Agent` ツールで `Explore` サブエージェントを起動する。**独立したチェックは 1 つのメッセージ内で並列に呼ぶ**こと（複数 `Agent` tool use を単一メッセージに含める）。

各サブエージェントに渡すプロンプトには以下を含める:

1. 対象コンポーネント名と該当する設計書・コードのパス
2. チェック観点（下記「チェック観点」の項目から、コンポーネントに関係するもののみ）
3. 出力形式（下記「サブエージェントの出力形式」）
4. thoroughness は `very thorough` を指定する

### チェック観点

各サブエージェントは以下の観点から、コンポーネントに関係する項目をチェックする:

1. **API 仕様** — エンドポイントのパス・HTTP メソッド・認証要件、リクエスト・レスポンススキーマ（フィールド名・型・必須/任意）、ステータスコード・エラー形式が設計書と実装 (`routes.py` / `schemas.py` / `services.py`) で一致しているか
2. **DB スキーマ** — テーブル名・カラム名・型・NULL 制約・デフォルト値・PK/FK・インデックスが設計書と `models.py` およびマイグレーションで一致しているか。設計書の SQL スニペットが実装可能か
3. **設定パラメータ** — 環境変数名・デフォルト値・型・説明が設計書と `server/src/cjob/config.py` で一致しているか。設計書にない新規設定、または設計書にあるが未実装の設定の検出
4. **CLI インターフェース** — サブコマンド・引数名・short/long flag・出力フォーマット（JSON フィールド、human-readable の列順）・バリデーションルールが設計書と Rust 実装で一致しているか
5. **K8s リソース・ラベル** — ラベル名・値の生成ルール、リソース定義（requests/limits/tolerations/volume/env 等）が設計書と実装（`dispatcher/k8s_job.py` 等）で一致しているか
6. **アルゴリズム・制御フロー** — 設計書で明記されたロジックと実装の一致（DRF 計算式、reconcile ステップ順序、状態遷移の条件、dispatch 順序、in-flight 計算 等）
7. **定数値・リテラル** — 設計書記載のデフォルト値・制限値・タイムアウト値とコード内リテラル・定数定義の一致
8. **観測性の利用側との整合（主に `monitoring` コンポーネント向け）** — 設計書で定義されたメトリクス名・ラベルキー・メトリクスタイプが、利用側（Grafana ダッシュボード JSON、Prometheus アラートルール YAML、ServiceMonitor / PodMonitor 等）で正しく参照されているか
   - Grafana ダッシュボードの PromQL クエリ（例: `cjob_jobs_completed_total{status="failed"}`）が参照するメトリクス名・ラベル名・ラベル値が、`monitoring.md` および `metrics.py` の定義と一致しているか
   - 設計書に定義されているが利用側でまったく参照されていない孤立メトリクスを検出する（提供しているのに誰も使っていない）
   - 利用側で参照されているが設計書に定義がないメトリクスを検出する（ダッシュボードが壊れている）
   - ServiceMonitor / PodMonitor のターゲットポート・パスが設計書記載の `WATCHER_METRICS_PORT` / `DISPATCHER_METRICS_PORT` 等と一致しているか
   - 失敗モード: メトリクス名やラベルがリネームされたのにダッシュボード/アラート側が追従せず、サイレントに観測不能状態になる

### サブエージェントの出力形式

```
## <コンポーネント名>

### 観点 1: API 仕様
- [CRITICAL] 乖離の内容（1 行要約）
  - 設計書: `docs/architecture/api.md:123` — `該当記述の短い抜粋`
  - 実装: `server/src/cjob/api/routes.py:45` — `該当記述の短い抜粋`
  - 差分の詳細: 何がどう食い違っているか
- [WARN] ...
- 差異なし / 該当なし の観点はそう明記する

### 観点 2: DB スキーマ
...
```

severity:
- `CRITICAL` — 機能不全・ユーザー影響を伴う乖離（API 契約の破壊、DB スキーマの不一致による CRUD 不可 等）
- `WARN` — 仕様の食い違いだが、直接的な機能不全は起きていない（デフォルト値のズレ、メッセージ文言の違い 等）
- `INFO` — 設計書と実装の表現の違いで、実質的な動作差分はないが整理の価値があるもの

### Step 3: 報告の検証（必須）

サブエージェントの報告には事実誤認が含まれる可能性があるため、**最終レポートに含める前に必ず以下を実施する**:

1. 各 finding について、引用されている設計書と実装の該当箇所（`<file>:<line>`）を `Read` ツールで直接再読する
2. 設計書の記述と実装が実際に食い違っていることを自分の目で確認する
   - サブエージェントが設計書を読み違えている可能性がある
   - コードの別の箇所を見落としている可能性がある（例: 設計書の記述はコード A にはないが、コード B にある）
   - 設計書の記述が「例示」であって「仕様」ではなかった可能性がある
3. 確認できた finding のみを最終レポートに含める
4. 誤検出だった finding は破棄する（ユーザーには提示しない）
5. 検証時に追加の乖離に気付いた場合は、独立した finding として追加する
6. 検証で破棄した finding の件数も最終レポートの先頭サマリに記載する（透明性のため）

### Step 4: 最終レポート

検証済みの finding をユーザーに提示する。以下を含める:

- **サマリ**: 総 finding 数、severity 別内訳、検証で破棄した件数、対象コンポーネント一覧
- **コンポーネント別詳細**: 各 finding の severity・1 行要約・該当箇所（設計書側・実装側）・差分の詳細
- **対応方針の補足**: CLAUDE.md の「設計書が正本」原則により、原則はコード側を設計書に合わせる。ただし設計書の記述自体が古い場合は設計書を更新する選択肢もある点を注記する

## 注意事項

- 設計書中の SQL / JSON / コード例ブロックは、サンプルであって厳密な実装仕様ではない場合もある。finding を出す前に、該当箇所が「仕様の明示」か「例示」かを見極める
- 日本語版 `docs/` を正とし、英語版 `docs_en/` は対象外（翻訳ズレは `translate-docs` skill の範疇）
- マイグレーション履歴 (`docs/migration/`) は実装済みの履歴なので drift チェック対象外
- `docs/testing.md` のようなメタドキュメントは、コード（テストファイル）との関係性が他の設計書と異なるため、観点を「テスト件数の一致」「テストファイル名の一致」に絞ってチェックする
- ファイル実在チェック・ファイル列挙には Glob ツール（または `Grep` の `output_mode="files_with_matches"`）を使うこと。bash の `for f in ...; do [ -f ... ]; done` や `ls` / `find` を使わない（ハーネスの allowlist にマッチせず承認待ちを引き起こすため）。サブエージェントへのプロンプトにもこの方針を含めること
- サブエージェントは中間ファイル・ドラフト・メモを `/tmp` 等に書き込まないこと。`Explore` subagent は Write/Edit ツールを持たないため、`cat > /tmp/file << EOF` のヒアドキュメントで書こうとすると Bash 承認待ちが発生する。最終レポートはレスポンスとして直接返すだけで十分。サブエージェントへのプロンプトにも「中間ファイルを書かず、最終レポートはレスポンスとして返す」を明示すること

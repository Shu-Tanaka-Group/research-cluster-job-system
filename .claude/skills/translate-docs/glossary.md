# 翻訳用語集（ja → en）

`docs/` を `docs_en/` に翻訳する際の訳語を固定するための対応表。`translate-docs` skill から参照する。

## 位置づけ

- **この用語集は `docs/` ではなく skill 配下に置く。** 日本語設計書から英語版を導出するための規則であり、システム設計そのものではない。`docs/` に置くと用語集自体が翻訳対象になり循環する
- **差分翻訳の一貫性はこの用語集が担保する。** 従来の「全体翻訳」はファイル内の一貫性しか保証せず、ファイル間の揺れは防げていなかった（`pre-check` と `precheck` がファイル単位で分かれていた等）
- **未登録の用語を訳したら、この表に追加する。** 使うほど育つ運用を前提にしている

## 表記の一般規則

| 規則 | 例 |
|---|---|
| 複合語の名詞形はスペース区切り、修飾語として使うときはハイフン | `gap filling` / `gap-filling precheck`、`time limit` / `time-limit enforcement` |
| 見出し（`##` 以上）は Title Case | `### Design Decisions`、`#### Constraints and Limitations` |
| コンポーネント名は文中では Title Case、K8s リソース名・設定値としては原文のまま | `Submit API` / `submit-api`（Deployment 名）、`Dispatcher` / `dispatcher` |
| 設定キー・カラム名・ステータス値は翻訳しない | `WATCHER_DISPATCH_TIMEOUT_SEC`、`unschedulable_count`、`DISPATCHED` |
| Kubernetes / Kueue の用語は原語のまま | `Pod`、`Job`、`namespace`、`admit`、`nominalQuota`、`ResourceQuota` |
| 疑似コード・SQL・コマンドの中身は翻訳しない（コメントのみ英訳する） | |

## ジョブとライフサイクル

| 日本語 | 英語 | 備考 |
|---|---|---|
| ジョブ投入 | job submission | 動詞は `submit` |
| 投入 Pod / 投入元 Pod | submitting Pod | |
| 実行 Pod | Job Pod | K8s の Job Pod を指すため原語 |
| 保留 | hold（動詞）/ held（状態） | `cjob hold` に対応 |
| 差し戻す | requeue | `revert` / `roll back` は使わない |
| 再試行 | retry | |
| 指数バックオフ | exponential backoff | `back-off` と綴らない |
| 滞留する（ジョブが DISPATCHED から進まない） | stall | **`stagnate` は使わない** |
| 滞留ジョブ | stalled job | |
| 滞留ガード | stall guard | |
| 猶予期間 | grace period | |
| 制限時間 | time limit | 修飾語は `time-limit` |
| 実行時間 | execution time | |
| 状態遷移 | state transition | |
| 完了フォールバック | completion fallback | |
| 消失検出 | disappearance detection | |
| 誤判定 | misjudgment | |
| 二重実行 | double execution | |

## スケジューリング

| 日本語 | 英語 | 備考 |
|---|---|---|
| 隙間充填 | gap filling | 見出しは `Gap Filling`、修飾語は `gap-filling` |
| プレチェック | pre-check | **`precheck` と綴らない** |
| per-node bin-packing プレチェック | per-node bin-packing pre-check | |
| 仮配置 | provisional placement | `tentative placement` は使わない |
| 均等分配 | even distribution | |
| 控除する / 差し引く | subtract | **`deduct` は使わない** |
| 残量 | remaining capacity | |
| 残リソース | remaining resources | |
| 割当待ち | awaiting resource allocation | パネル名・ラベルは `Awaiting Resource Allocation`。説明文中では `waiting for resource allocation` も可 |
| 累計消費量 | cumulative consumption | `cumulative usage` は使わない |
| 公平性 | fairness | |
| fair sharing | fair sharing | 見出しは `Fair Sharing` |
| dominant share | dominant share | DRF 用語のため原語 |
| in-flight ジョブ | in-flight job | |
| 候補 | candidate | |
| 閾値 | threshold | |
| サイクル | cycle | 日本語側の使い分けに従う（`reconcile cycle` / `scan cycle` / `dispatch cycle` / `polling cycle`） |

## リソースと設定

| 日本語 | 英語 | 備考 |
|---|---|---|
| effective allocatable | effective allocatable | 原語のまま |
| ノード残量 | per-node remaining capacity | |
| 上限 | limit / ceiling | quota の上限は `limit`、バックオフ等の頭打ちは `ceiling` |
| 同期 | sync（名詞）/ synchronize（動詞） | |
| 反映する | reflect / apply | ConfigMap への反映は `reflect` |
| 上書きする | overwrite | |
| 冪等 | idempotent | |
| ユーザー namespace | user namespace | 修飾語は `user-namespace` |
| dispatch budget | dispatch budget | ハイフンなし |

## コンポーネントと運用

| 日本語 | 英語 | 備考 |
|---|---|---|
| 管理者 | administrator | |
| 実環境 | production environment | |
| ロールアウト | rollout | |
| 再起動 | restart | |
| 事前確認 | pre-check（作業）/ prior verification（文脈次第） | 設計上の「プレチェック」と区別が必要な場合は後者 |
| 移行手順 | migration procedures | **`migration steps` は使わない**。ファイルのタイトルも `... Migration Procedures` |
| 標準移行手順 | standard migration procedures | |
| 前提条件 | Prerequisites | 見出し。文中の「前提」は `assumption` / `premise` |
| 運用 | operation(s) | |
| 記載漏れ | omission | |

## ドキュメントとプロセス

| 日本語 | 英語 | 備考 |
|---|---|---|
| 設計書 | specification | 「設計書が正本」の文脈では `specification` |
| 正本 | authoritative | `source of truth` は使わない |
| 乖離 | divergence | |
| 設計判断 | Design Decisions | 見出し |
| 制約と限界 | Constraints and Limitations | 見出し |
| 想定される制約 | Expected Constraints | 見出し |
| 背景 | Background | 見出し |
| 方針 | Approach / Policy | 見出しは文脈で選ぶ |
| 論点 | Open Questions | 見出し |
| 影響範囲 | Scope of Impact | 見出し |
| 対応方針 | Resolution | 見出し |

## 使い分けが正当なもの（揺れではない）

以下は日本語側にも対応する使い分けがあるため、統一しない。

- `time limit`（名詞）/ `time-limit`（修飾語）
- `user namespace`（名詞）/ `user-namespace`（修飾語）
- `gap filling`（名詞）/ `gap-filling`（修飾語）/ `Gap Filling`（見出し）
- `Submit API`（コンポーネント名）/ `submit-api`（K8s Deployment 名・イメージ名）
- `reconcile cycle` / `scan cycle` / `dispatch cycle` / `polling cycle`（日本語側も使い分けている）
- `Prerequisites`（見出し）/ `prerequisite`（文中）
- `Awaiting Resource Allocation`（パネル名・ラベル）/ `waiting for resource allocation`（説明文）

## 混同しやすい語

| 日本語 | 英語 | 注意 |
|---|---|---|
| 滞留（ジョブが DISPATCHED から進まない） | stall | |
| 膠着・停滞（公平性の均衡が動かない） | stagnation / stagnate | **`stall` に置き換えない。** `dispatcher.md` の `fairness stagnates` / `equilibrium stagnation` は公平性の文脈であり、ジョブの滞留とは別概念 |
| 事前確認（デプロイ前の作業） | prior verification | 設計上の「プレチェック」（`pre-check`）と紛らわしいため、運用手順の文脈では区別する |

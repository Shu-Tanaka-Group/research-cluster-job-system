# 未リリース移行手順

本ファイルは **次回リリース向け** の移行手順を記載する作業ファイルである。リリース時にバージョン名（例: `v1.14.0.md`）にリネームし、新しい `unreleased.md` を作成する（[versioning.md](../versioning.md) 参照）。

[標準移行手順](../migration.md) に加えて次回リリース固有の移行手順がある場合は以下に追記する。

## Grafana ダッシュボードの再インポート

ユーザー向けダッシュボード `k8s/base/grafana/dashboard-user.json` のパネル文言・SQL を以下の通り見直した。Grafana UI から再インポートが必要（[deployment.md](../deployment.md) §17.5 参照）。

- 「待機中ジョブ数」→「リソース割当待ちジョブ数」にリネーム。集計対象を `QUEUED + DISPATCHING + DISPATCHED` から `DISPATCHED` のみに変更
- 「Flavor 別キュー使用状況」: 「待機中」列を「リソース割当待ち」にリネームし集計対象を `DISPATCHED` のみに変更。さらに「投入済み」(`QUEUED`) 列を追加し、Flavor ごとのキュー状態を全アクティブ状態（QUEUED / DISPATCHED / RUNNING / HELD）で確認できるようにした
- 「キュー内ジョブ数の推移」の「待機中」凡例を「リソース割当待ち」にリネーム
- 「ジョブ状態の内訳」piechart で `DISPATCHING` を表示対象から除外。`QUEUED` を「投入済み」、`DISPATCHED` を「割当待ち」にリネーム
- 「リソース割当て待ち (P50)」「リソース割当て待ち時間の推移 (P50 / P95)」の送り仮名「て」を削除（「リソース割当待ち」に統一）

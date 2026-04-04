# 未リリース移行手順

[標準移行手順](../migration.md) の Watcher ビルド・再起動（ロールアウト再起動）により新ロジックが適用される。追加の環境変数・DB スキーマ変更・ConfigMap 変更は不要。

## `node_resources` の effective allocatable 化に伴う確認事項

Watcher が `node_resources` テーブルに記録する CPU・memory を `allocatable - DaemonSet Pod requests` に変更した（issue #134）。Watcher 再起動後、次の同期サイクル（最大 `NODE_RESOURCE_SYNC_INTERVAL_SEC` 秒後、デフォルト 300 秒）で DB の値が更新される。

更新後に以下を確認する。

- `cjobctl cluster set-quota --flavor <name>` で設定済みの nominalQuota が、新しい effective allocatable の合計以下に収まっているかを確認する。超過している場合は `cjobctl cluster set-quota` で下方修正しないと、DISPATCHED で待機するジョブが発生する可能性がある
- `cjob flavor info` の TASK LIMIT 表示が想定通り（DaemonSet Pod 分だけ減少）になっているかを確認する

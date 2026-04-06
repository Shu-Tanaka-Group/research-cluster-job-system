# 未リリース移行手順

本ファイルは **次回リリース向け** の移行手順を記載する作業ファイルである。リリース時にバージョン名（例: `v1.11.0.md`）にリネームし、新しい `unreleased.md` を作成する（[versioning.md](../versioning.md) 参照）。

[標準移行手順](../migration.md) に加えて、以下の追加作業が必要。

## ConfigMap 更新: `DISPATCH_FETCH_MULTIPLIER` の追加

Dispatcher の候補取得数に余剰を持たせる設定 `DISPATCH_FETCH_MULTIPLIER` を新設した（issue #136）。ConfigMap `cjob-config` にキーを追加したうえで、Dispatcher Deployment を再起動する。

```bash
# ConfigMap を適用（base マニフェストにキーが追加されている）
kubectl apply -k k8s/overlay-<env>

# Dispatcher を再起動して新しい環境変数を読み込ませる
kubectl -n cjob-system rollout restart deploy/dispatcher
```

デフォルト値は `10` である。overlay で上書きする必要は通常ない。

## `node_resources` の effective allocatable 化に伴う確認事項

Watcher が `node_resources` テーブルに記録する CPU・memory を `allocatable - DaemonSet Pod requests` に変更した（issue #134）。Watcher 再起動後、次の同期サイクル（最大 `NODE_RESOURCE_SYNC_INTERVAL_SEC` 秒後、デフォルト 300 秒）で DB の値が更新される。

更新後に以下を確認する。

- `cjobctl cluster set-quota --flavor <name>` で設定済みの nominalQuota が、新しい effective allocatable の bin-packing 考慮済み合計（各ノード `cpu_millicores` を整数コアに切り下げて合算）以下に収まっているかを確認する。超過している場合は `cjobctl cluster set-quota` で下方修正しないと、DISPATCHED で待機するジョブが発生する可能性がある。`cjobctl cluster set-quota` のバリデーションは新バージョンから bin-packing 考慮済みの合計を使用する（詳細は [cjobctl.md](../architecture/cjobctl.md) §5.4 `set-quota` 項目、[database.md](../architecture/database.md) §6.2 参照）
- `cjob flavor info` の TASK LIMIT 表示が想定通り（DaemonSet Pod 分だけ減少）になっているかを確認する

## Flavor-aware budget による ResourceQuota サイジングの見直し

Dispatcher の dispatch budget が namespace 単位から `(namespace, flavor)` 単位に変更された（issue #138）。これにより、各 flavor が独立した budget（`DISPATCH_BUDGET_PER_NAMESPACE`、デフォルト 32）を持つようになり、namespace あたりの最大 active ジョブ数が理論上 `DISPATCH_BUDGET_PER_NAMESPACE × flavor 数` に増加する。

ResourceQuota が従来の単一 budget（32 ジョブ分）を前提にサイジングされている場合、全 flavor で同時に budget 上限まで active になると ResourceQuota に達しやすくなる可能性がある。この場合、ResourceQuota プレチェック（§2.5）により QUEUED に留まるジョブが増えるが、過剰 dispatch にはならない（保守的な方向）。

必要に応じて ResourceQuota の上限を見直す。実運用では全 flavor が同時に budget 上限に達することは稀であるため、即座の変更は必須ではない。

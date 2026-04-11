# 未リリース移行手順

本ファイルは **次回リリース向け** の移行手順を記載する作業ファイルである。リリース時にバージョン名（例: `v1.13.0.md`）にリネームし、新しい `unreleased.md` を作成する（[versioning.md](../versioning.md) 参照）。

[標準移行手順](../migration.md) に加えて次回リリース固有の移行手順がある場合は以下に追記する。

## cjobctl CLI コマンド変更

`cjobctl counters list` は `cjobctl jobs counters` に移行された。既存のスクリプトや手順書で旧コマンドを使用している場合は更新すること。

## ClusterQueue から `cohortName` / `lendingLimit` を削除する

ClusterQueue の設計を見直し、`cohortName: cjob-cohort` と GPU flavor に設定していた `lendingLimit: "0"` を削除した。Dispatcher が Job Pod に flavor ごとの `nodeSelector` を必ず設定しているため、Kueue の flavor 照合によって別 flavor の quota を消費することは構造的に発生せず、これらの設定は実質的に no-op だった。

既存環境では ClusterQueue リソースを以下の手順で更新する:

```bash
kubectl edit clusterqueue cjob-cluster-queue
```

以下の変更を行う:

1. `spec.cohortName` の行を削除する
2. `spec.resourceGroups[0].flavors[*].resources[*].lendingLimit` の行をすべて削除する

変更後、`cjobctl cluster show-quota` の出力から `(lendingLimit: 0)` の併記が消えることを確認する。Kueue 側の admission 挙動は変化しないため、実行中・pending のジョブへの影響はない。

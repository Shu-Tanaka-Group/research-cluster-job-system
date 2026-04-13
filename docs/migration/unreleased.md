# 未リリース移行手順

本ファイルは **次回リリース向け** の移行手順を記載する作業ファイルである。リリース時にバージョン名（例: `v1.14.0.md`）にリネームし、新しい `unreleased.md` を作成する（[versioning.md](../versioning.md) 参照）。

[標準移行手順](../migration.md) に加えて次回リリース固有の移行手順がある場合は以下に追記する。

## Watcher の memory limit 引き上げ

Watcher の K8s API 呼び出しがページネーションおよび軽量 dataclass 化されたことに伴い、`k8s/base/watcher/deployment.yaml` の `resources.limits.memory` が `256Mi` から `1Gi` に変更された（request は 128Mi のまま）。`kubectl apply -k` で overlay を適用し、watcher Deployment を rollout することで新しい memory limit が反映される。

```bash
kubectl apply -k overlays/<env>
kubectl rollout restart deployment watcher -n cjob-system
```

独自 overlay で watcher の `resources` を上書きしている場合は、overlay 側の memory limit を同等以上（1Gi 推奨）に更新すること。

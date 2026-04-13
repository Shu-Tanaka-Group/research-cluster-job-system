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

## `cjob-config` への `WATCHER_K8S_LIST_PAGE_SIZE` 追加

`cjob-config` ConfigMap に新しい標準キー `WATCHER_K8S_LIST_PAGE_SIZE`（デフォルト `"500"`）が追加される。`kubectl apply -k overlays/<env>` で base の ConfigMap が反映された後、以下のいずれかを実行すること。

- 通常ケース: `cjobctl system restart watcher`（env は ConfigMap から読み込まれる）
- 独自 overlay で `cjob-config` の内容をパッチしている場合: overlay 側の ConfigMap patch に `WATCHER_K8S_LIST_PAGE_SIZE: "500"` を追記してから apply する。値を明示しない場合でも Python 側のデフォルト（500）で動作するが、`cjobctl config show` の出力と一致させるため ConfigMap にも載せることを推奨する

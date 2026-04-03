# 未リリース移行手順

[標準移行手順](../migration.md) に加えて、以下の追加作業が必要。

## flavor ノードラベルのキー統一（#119）

ノードラベルのキーを flavor ごとに異なるキー（`cluster-job=true`, `cluster-gpu-job=true` 等）から共通キー `cjob.io/flavor` に統一する。3 箇所（ノードラベル・Kueue ResourceFlavor・ConfigMap）を同時に変更する必要があるため、メンテナンスウィンドウでの適用が望ましい。

### 1. 実行中ジョブの完了を待つ

```bash
cjobctl jobs active
```

実行中のジョブがある場合、完了を待つか、必要に応じてキャンセルする。

### 2. ノードのラベルを変更する

旧ラベルを削除し、新ラベルを付与する。

```bash
# CPU ノード
kubectl label node <node-name> cluster-job-
kubectl label node <node-name> cjob.io/flavor=cpu

# GPU ノード（例: flavor 名が gpu の場合）
kubectl label node <gpu-node-name> cluster-gpu-job-
kubectl label node <gpu-node-name> cjob.io/flavor=gpu
```

### 3. Kueue ResourceFlavor を更新する

各 ResourceFlavor の `nodeLabels` を更新する。

```bash
kubectl edit resourceflavor cpu
```

```yaml
# 変更前
spec:
  nodeLabels:
    cluster-job: "true"

# 変更後
spec:
  nodeLabels:
    cjob.io/flavor: "cpu"
```

GPU flavor も同様に更新する。

### 4. ConfigMap `RESOURCE_FLAVORS` を更新する

```bash
kubectl edit configmap cjob-config -n cjob-system
```

```yaml
# 変更前
RESOURCE_FLAVORS: |
  [
    {"name": "cpu", "label_selector": "cluster-job=true"},
    {"name": "gpu", "label_selector": "cluster-gpu-job=true", "gpu_resource_name": "nvidia.com/gpu"}
  ]

# 変更後
RESOURCE_FLAVORS: |
  [
    {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
    {"name": "gpu", "label_selector": "cjob.io/flavor=gpu", "gpu_resource_name": "nvidia.com/gpu"}
  ]
```

### 5. コンポーネントを再起動する

```bash
kubectl rollout restart deployment submit-api dispatcher watcher -n cjob-system
```

### 6. 動作確認

```bash
# ノード同期の確認
cjobctl cluster resources

# ジョブ投入の確認
cjob add --cpu 1 --memory 1Gi -- echo "label migration test"
cjob list
```

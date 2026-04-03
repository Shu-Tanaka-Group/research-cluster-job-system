# 未リリース移行手順

[標準移行手順](../migration.md) に加えて、以下の追加作業が必要。

## Prometheus メトリクスの有効化（#121）

### 1. ConfigMap の確認

ConfigMap `cjob-config` に `WATCHER_METRICS_PORT` キーが追加された。デフォルト値は `"9090"`。overlay でカスタマイズしている場合は値を追加する。

### 2. NetworkPolicy の確認

Prometheus namespace から Submit API への metrics scrape を許可する NetworkPolicy `allow-metrics-scrape` が追加された。base のデフォルトは `kubernetes.io/metadata.name: monitoring` ラベルで namespace を識別する。

Prometheus が `monitoring` 以外の namespace で動作している場合は、overlay で NetworkPolicy の `namespaceSelector.matchLabels` をパッチする（`overlay-example/kustomization.yaml` 参照）。

### 3. Prometheus scrape 設定の確認

Submit API と Watcher の Pod テンプレートに `prometheus.io/scrape` アノテーションが追加された。Prometheus が annotation-based service discovery を使用している場合は自動的に scrape される。ServiceMonitor を使用している場合は、別途設定を追加する。

### 4. Grafana ダッシュボードの再インポート

`k8s/base/grafana/dashboard-user.json` が更新された。Grafana UI の `Dashboards > Import` から JSON ファイルを再インポートする。

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

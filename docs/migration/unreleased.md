# 未リリース移行手順

[標準移行手順](../migration.md) に加え、以下の追加作業が必要。

## Dispatcher PodMonitor の適用

Dispatcher のメトリクスを Prometheus で scrape するため、PodMonitor を適用する。

```bash
kubectl apply -f k8s/base/prometheus-operator/podmonitor-dispatcher.yaml
```

## Dispatcher Deployment の再適用

Dispatcher にメトリクスポート（9090）が追加されたため、Deployment を再適用する。

```bash
kubectl apply -f k8s/base/dispatcher/deployment.yaml
kubectl rollout restart deployment dispatcher -n cjob-system
```

# 運用ガイド

管理操作は `cjobctl` CLI で行う。セットアップは [ビルド手順](build.md) を参照。

各コマンドが実行する SQL クエリの詳細は `ctl/src/cmd/` 配下のソースコードを参照。

| ソースファイル | 対応コマンド |
|---|---|
| `ctl/src/cmd/jobs.rs` | `cjobctl jobs` サブコマンド全般 |
| `ctl/src/cmd/usage.rs` | `cjobctl usage list / reset` |
| `ctl/src/cmd/counters.rs` | `cjobctl counters list` |
| `ctl/src/cmd/weight.rs` | `cjobctl weight` サブコマンド全般 |
| `ctl/src/cmd/cluster.rs` | `cjobctl cluster` サブコマンド全般 |
| `ctl/src/cmd/cli_deploy.rs` | `cjobctl cli deploy` |
| `ctl/src/cmd/cli_list.rs` | `cjobctl cli list` |
| `ctl/src/cmd/cli_remove.rs` | `cjobctl cli remove` |
| `ctl/src/cmd/cli_set_latest.rs` | `cjobctl cli set-latest` |
| `ctl/src/cmd/db_migrate.rs` | `cjobctl db migrate` |

## 1. DB 状態の確認

### 1.1 PostgreSQL への接続

アドホッククエリが必要な場合は、直接 PostgreSQL に接続できる。

```bash
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob
```

`-c` オプションで直接実行することもできる。

```bash
kubectl exec -it -n cjob-system postgres-0 -- psql -U cjob -d cjob -c "<SQL>"
```

### 1.2 ジョブ一覧の確認

```bash
# 全ジョブの概要
cjobctl jobs list

# namespace でフィルタ
cjobctl jobs list --namespace user-alice

# ステータスでフィルタ
cjobctl jobs list --status RUNNING

# namespace × ステータスのジョブ数（ピボットテーブル）
cjobctl jobs summary
```

### 1.3 累計リソース消費量の確認

日別消費量・7日間ウィンドウ集計・DRF dominant share を一括表示する。

```bash
cjobctl usage list
```

### 1.4 ジョブカウンターの確認

```bash
cjobctl counters list
```

### 1.5 滞留ジョブの確認

DISPATCHED のまま長時間経過しているジョブ（隙間充填の対象）を確認する。

```bash
cjobctl jobs stalled
```

### 1.6 RUNNING ジョブの残り時間

```bash
cjobctl jobs remaining
```

### 1.7 クラスタリソース総量の確認

Watcher が K8s ノードから自動取得したリソース情報を確認する。

```bash
cjobctl cluster resources
```

以下の3つのセクションが表示される。

- **Node Resources**: ノードごとの allocatable（CPU / メモリ / GPU）と最終更新時刻
- **Cluster Totals**: 全ノードの合計値。Dispatcher の DRF 正規化に使用される
- **Max per Node**: 各リソースの最大ノード値。Submit API のリソース超過リジェクトの閾値となる

ノードの追加・撤去は Watcher が `NODE_RESOURCE_SYNC_INTERVAL_SEC`（デフォルト 300 秒）間隔で自動反映する。手動更新は不要。

テーブルが空の場合は Watcher が未起動または対象ノードが存在しない状態を示す。計算ノードに `cluster-job=true` ラベルが付与されていることを確認すること（[deployment.md](deployment.md) §16 参照）。

### 1.8 ClusterQueue の nominalQuota 確認

Kueue ClusterQueue に設定されている現在の nominalQuota を ResourceFlavor ごとに表示する。`lendingLimit` が設定されている場合はその値も併記される。

```bash
cjobctl cluster show-quota
```

### 1.9 ClusterQueue の nominalQuota 更新

`--flavor` で更新対象の ResourceFlavor を指定する（必須）。`--cpu`、`--memory`、`--gpu` で更新するリソースを指定する（少なくとも 1 つ必須）。

```bash
# cpu-flavor の CPU とメモリを更新
cjobctl cluster set-quota --flavor cpu-flavor --cpu 256 --memory 1000Gi

# cpu-flavor の CPU のみ更新（メモリは現在値を維持）
cjobctl cluster set-quota --flavor cpu-flavor --cpu 128

# gpu-flavor の GPU を更新
cjobctl cluster set-quota --flavor gpu-flavor --gpu 4

# gpu-flavor の CPU・メモリ・GPU をまとめて更新
cjobctl cluster set-quota --flavor gpu-flavor --cpu 64 --memory 500Gi --gpu 4

# cpu-flavor の GPU quota を削除
cjobctl cluster set-quota --flavor cpu-flavor --gpu 0
```

指定値は `node_resources` テーブルの allocatable 合計と比較してバリデーションされる。

- **allocatable 超過** → エラーで中断。`--force` を指定すると警告付きで適用を許可する（ノード追加直前に quota を先行設定する場合など）
- **極端に小さい値**（allocatable の 10% 未満） → 警告を表示するが適用は可能

更新後の確認は以下でも行える。

```bash
kubectl get clusterqueue cjob-cluster-queue -o jsonpath='{range .spec.resourceGroups[*].flavors[*].resources[*]}name={.name} nominalQuota={.nominalQuota}{"\n"}{end}'
```

## 2. コンポーネントの状態確認

### 2.1 Pod の状態

```bash
cjobctl status
```

### 2.2 ログの確認

```bash
# Dispatcher（デフォルト: 直近50行）
cjobctl logs dispatcher

# 表示行数を指定
cjobctl logs watcher --tail 100

# Submit API
cjobctl logs submit-api
```

### 2.3 ConfigMap の確認

```bash
cjobctl config show
```

## 3. namespace の weight 管理

namespace ごとの fair sharing の重み（weight）を管理する。weight が大きい namespace ほど多くのリソースを公平に受け取れる。

テーブルに行がない namespace はデフォルト weight = 1 として扱われる。

```bash
# 現在の weight 一覧
cjobctl weight list

# 特定 namespace の weight を設定
cjobctl weight set user-alice 2

# weight をデフォルト（1）に戻す
cjobctl weight reset user-alice
```

### 特定ユーザーにクラスタを専有させる場合

K8s の namespace ラベル（`cjob.io/user-namespace=true`）を元に、専有ユーザー以外の全 namespace を weight = 0（dispatch 禁止）に設定する。

```bash
# user-alice にクラスタを専有させる
cjobctl weight exclusive user-alice

# 専有を解除（全員の weight をデフォルトに戻す）
cjobctl weight exclusive --release
```

専有中に新しい namespace が作成された場合は、専有コマンドを再実行して追加分を weight = 0 にする。

## 4. 累計リソース消費量の手動リセット

```bash
# 特定 namespace のリセット
cjobctl usage reset --namespace user-alice

# 全 namespace のリセット
cjobctl usage reset --all
```

## 5. ユーザーのアクセス制御

namespace のラベル `cjob.io/user-namespace=true` の有無でジョブ投入の可否を制御する。このラベルは NetworkPolicy で参照されており、ラベルがない namespace からは Submit API への通信がブロックされる。

### 5.1 ユーザーのアクセスを停止する

```bash
# ラベルを削除してジョブ投入を停止
kubectl label namespace <namespace> cjob.io/user-namespace-

# 必要に応じて実行中のジョブをキャンセル
cjobctl jobs cancel --namespace <namespace> --all
```

ラベルを外しても、既に QUEUED / DISPATCHED / RUNNING のジョブはそのまま動き続ける。完全に停止したい場合は `cjobctl jobs cancel --all` で該当 namespace の全アクティブジョブをキャンセルすること。

### 5.2 ユーザーのアクセスを再開する

```bash
kubectl label namespace <namespace> cjob.io/user-namespace=true
```

## 6. DB スキーマの更新

バージョンアップ時に新しいテーブルやカラムを追加する場合。`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` により冪等に実行できる。

```bash
cjobctl db migrate
```

## 7. 計算ノードの追加

### 7.1 CPU ノードのラベル・taint の付与

新しい CPU ノードには以下のラベルと taint を付与する。taint の値は ConfigMap `cjob-config` の `JOB_NODE_TAINT` に合わせること（デフォルト: `role=computing:NoSchedule`）。

```bash
kubectl label node <node-name> cluster-job=true
kubectl taint node <node-name> role=computing:NoSchedule
```

ラベル `cluster-job=true` は ConfigMap `cjob-config` の `NODE_LABEL_SELECTOR` で指定された値と一致している必要がある。値を変更している場合は、ConfigMap の設定に合わせること。

```bash
# 現在の設定を確認（NODE_LABEL_SELECTOR, GPU_NODE_LABEL_SELECTOR, JOB_NODE_TAINT）
cjobctl config show
```

このラベルは以下の 2 箇所で参照される。

| 参照元 | 用途 |
|---|---|
| Kueue cpu-flavor（`nodeLabels`） | CPU Job Pod をラベル付きノードにのみスケジュールする |
| Watcher（`NODE_LABEL_SELECTOR`） | ラベル付きノードの allocatable リソースを DB に同期する |

taint はジョブ以外の Pod が計算ノードにスケジュールされることを防ぐ。ConfigMap `JOB_NODE_TAINT`・Kueue ResourceFlavor の `nodeTaints`・ノードの Taint の 3 箇所は同じ値に統一する必要がある。`JOB_NODE_TAINT` が空文字列の場合は taint を付与しない。

### 7.1.1 GPU ノードのラベル・taint の付与

GPU ノードには CPU ノードとは異なるラベル `cluster-gpu-job=true` を付与する。taint は CPU ノードと同じ値を使用する。

```bash
kubectl label node <gpu-node-name> cluster-gpu-job=true
kubectl taint node <gpu-node-name> role=computing:NoSchedule
```

ラベル `cluster-gpu-job=true` は ConfigMap `cjob-config` の `GPU_NODE_LABEL_SELECTOR` で指定された値と一致している必要がある。

| 参照元 | 用途 |
|---|---|
| Kueue gpu-flavor（`nodeLabels`） | GPU Job Pod をラベル付きノードにのみスケジュールする |
| Watcher（`GPU_NODE_LABEL_SELECTOR`） | GPU ノードの allocatable リソースを DB に同期する |

### 7.2 リソース情報の反映確認

Watcher が `NODE_RESOURCE_SYNC_INTERVAL_SEC`（デフォルト 300 秒）間隔で自動的にノードを検出し、`node_resources` テーブルを更新する。

```bash
# ノードが認識されたことを確認
cjobctl cluster resources
```

### 7.3 ClusterQueue の nominalQuota 更新

ノード追加によりクラスタの総リソースが増加した場合、ClusterQueue の nominalQuota を更新する。

```bash
# 現在の quota を確認
cjobctl cluster show-quota

# 新しい総量に合わせて更新
cjobctl cluster set-quota --cpu <new-total> --memory <new-total>
```

Watcher の同期が完了する前に quota を設定したい場合は `--force` を使用する。

```bash
cjobctl cluster set-quota --cpu <new-total> --memory <new-total> --force
```

## 8. CLI バイナリの管理

新しいバージョンの `cjob` CLI バイナリをビルドし、PVC に配置する手順。配置後、ユーザーは `cjob update` でセルフアップデートできる。

### 8.1 登録済みバージョンの確認

```bash
cjobctl cli list
```

出力例:

```
VERSION            LATEST
1.3.0-beta.1
1.3.0              ← latest
1.2.0
1.1.0
```

### 8.2 安定版のデプロイ

```bash
# 1. CLI バイナリのビルド（ビルド環境の準備は build.md §3 を参照）
cargo build --release --target x86_64-unknown-linux-musl --manifest-path cli/Cargo.toml

# 2. PVC にバイナリを配置（latest は更新されない）
cjobctl cli deploy --binary ./cli/target/x86_64-unknown-linux-musl/release/cjob --version <version>

# 3. latest を更新してユーザーに公開
cjobctl cli deploy --binary ./cli/target/x86_64-unknown-linux-musl/release/cjob --version <version> --release
```

`--release` を付けない場合、バイナリは PVC に配置されるが `latest` は更新されない。動作確認後に `--release` 付きで再デプロイするか、`cjobctl cli set-latest` で latest を変更する。

### 8.3 ベータ版のデプロイ

ベータ版（バージョン文字列に `-` を含むもの）は `--release` を付けられない。latest は変更されないため、ユーザーが `cjob update` を実行しても安定版のまま維持される。

```bash
cjobctl cli deploy --binary ./cjob --version 1.3.1-beta.1
```

### 8.4 latest バージョンの変更

既にデプロイ済みのバージョンに対して latest を変更する。誤って latest を更新してしまった場合や、問題のあるバージョンからロールバックする場合に使用する。

```bash
# latest を 1.2.0 に変更（ロールバック）
cjobctl cli set-latest 1.2.0
```

プレリリース版は latest に設定できない。

### 8.5 配置後の確認

```bash
# Submit API の /v1/cli/version エンドポイントで最新バージョンが返ることを確認
kubectl exec -it -n cjob-system deploy/submit-api -- curl -s http://localhost:8080/v1/cli/version
```

### 8.6 古いバージョンの削除

```bash
# 単一バージョンの削除
cjobctl cli remove 1.1.0

# 複数バージョンの同時削除
cjobctl cli remove 1.0.0 1.1.0
```

`latest` に指定されているバージョンは削除できない。削除前に確認プロンプトが表示される。

### 8.7 内部処理の詳細

`cjobctl cli deploy` は以下を自動的に実行する（[cjobctl.md](architecture/cjobctl.md) §5.6 参照）。

1. `kubectl run` で `cli-binary` PVC をマウントした一時 Pod（busybox）を起動
2. `kubectl cp` でバイナリを `/cli-binary/<version>/cjob` にコピー
3. 一時 Pod 内で `chmod +x` を実行
4. `--release` 指定時のみ latest ファイルを更新
5. 一時 Pod を削除

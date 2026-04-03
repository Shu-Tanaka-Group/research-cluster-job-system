# 運用ガイド

管理操作は `cjobctl` CLI で行う。セットアップは [ビルド手順](build.md) を参照。

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

# 個別ジョブの詳細表示
cjobctl jobs status --namespace user-alice --job-id 42

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
- **Max per Node**: 各リソースの最大ノード値。Submit API のリソース超過リジェクトでは nominalQuota と合わせて `min(最大ノード, nominalQuota)` が有効上限となる

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
# cpu の CPU とメモリを更新
cjobctl cluster set-quota --flavor cpu --cpu 256 --memory 1000Gi

# cpu の CPU のみ更新（メモリは現在値を維持）
cjobctl cluster set-quota --flavor cpu --cpu 128

# gpu の GPU を更新
cjobctl cluster set-quota --flavor gpu --gpu 4

# gpu の CPU・メモリ・GPU をまとめて更新
cjobctl cluster set-quota --flavor gpu --cpu 64 --memory 500Gi --gpu 4

# cpu の GPU quota を削除
cjobctl cluster set-quota --flavor cpu --gpu 0
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
cjobctl system status
```

### 2.2 ログの確認

```bash
# Dispatcher（デフォルト: 直近50行）
cjobctl system logs dispatcher

# 表示行数を指定
cjobctl system logs watcher --tail 100

# Submit API
cjobctl system logs submit-api
```

### 2.3 ConfigMap の確認・変更

```bash
# 現在の設定値を一覧表示
cjobctl config show

# 設定値の変更
cjobctl config set DISPATCH_BATCH_SIZE 100

# JSON 値の変更（ファイルから読み込み）
cjobctl config set RESOURCE_FLAVORS --from-file flavors.json

# 現在の ConfigMap を YAML でバックアップ
cjobctl config dump > cjob-config-backup.yaml

# バックアップから復元
kubectl apply -f cjob-config-backup.yaml
```

`config set` は変更前に確認プロンプト (`[y/N]`) を表示する。`--yes` でスキップ可能。更新後は影響を受けるコンポーネントの再起動コマンドが表示されるので、それに従って `cjobctl system restart` を実行すること。

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

namespace のラベル `cjob.io/user-namespace` の値でジョブ投入の可否を制御する。このラベルは NetworkPolicy で参照されており、ラベルの値が `"true"` でない namespace からは Submit API への通信がブロックされる。

### 5.1 ユーザー一覧の確認

```bash
# 全ユーザー namespace の一覧
cjobctl user list

# 有効なユーザーのみ表示
cjobctl user list --enabled

# 無効なユーザーのみ表示
cjobctl user list --disabled
```

### 5.2 ユーザーのアクセスを停止する

```bash
# 単一 namespace を無効化
cjobctl user disable --namespace user-bob

# 複数 namespace を同時に無効化
cjobctl user disable --namespace user-alice user-bob

# 必要に応じて実行中のジョブをキャンセル
cjobctl jobs cancel --namespace <namespace> --all
```

無効化しても、既に QUEUED / DISPATCHED / RUNNING のジョブはそのまま動き続ける。完全に停止したい場合は `cjobctl jobs cancel --namespace <namespace> --all` で該当 namespace の全アクティブジョブをキャンセルすること。

### 5.3 ユーザーのアクセスを再開する

```bash
# 単一 namespace を有効化
cjobctl user enable --namespace user-charlie

# 複数 namespace を同時に有効化
cjobctl user enable --namespace user-alice user-bob
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
kubectl label node <node-name> cjob.io/flavor=cpu
kubectl taint node <node-name> role=computing:NoSchedule
```

ラベル `cjob.io/flavor=cpu` は ConfigMap `cjob-config` の `RESOURCE_FLAVORS` で定義された該当 flavor の `label_selector` と一致している必要がある。全 flavor で共通キー `cjob.io/flavor` を使用し、値に flavor 名を設定する。

```bash
# 現在の設定を確認（RESOURCE_FLAVORS, JOB_NODE_TAINT）
cjobctl config show
```

このラベルは以下の 2 箇所で参照される。

| 参照元 | 用途 |
|---|---|
| Kueue ResourceFlavor（`nodeLabels`） | Job Pod をラベル付きノードにのみスケジュールする |
| Watcher（`RESOURCE_FLAVORS` の `label_selector`） | ラベル付きノードの allocatable リソースを DB に同期する |

taint はジョブ以外の Pod が計算ノードにスケジュールされることを防ぐ。ConfigMap `JOB_NODE_TAINT`・Kueue ResourceFlavor の `nodeTaints`・ノードの Taint の 3 箇所は同じ値に統一する必要がある。`JOB_NODE_TAINT` が空文字列の場合は taint を付与しない。

### 7.1.1 GPU ノードのラベル・taint の付与

GPU ノードには CPU ノードと同じキー `cjob.io/flavor` を使用し、値に GPU flavor 名を設定する。taint は CPU ノードと同じ値を使用する。

```bash
kubectl label node <gpu-node-name> cjob.io/flavor=gpu
kubectl taint node <gpu-node-name> role=computing:NoSchedule
```

ラベルは ConfigMap `cjob-config` の `RESOURCE_FLAVORS` で定義された該当 GPU flavor の `label_selector` と一致している必要がある。

| 参照元 | 用途 |
|---|---|
| Kueue ResourceFlavor（`nodeLabels`） | GPU Job Pod をラベル付きノードにのみスケジュールする |
| Watcher（`RESOURCE_FLAVORS` の `label_selector`） | GPU ノードの allocatable リソースを DB に同期する |

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

# 新しい総量に合わせて更新（flavor を指定する）
cjobctl cluster set-quota --flavor cpu --cpu <new-total> --memory <new-total>
```

Watcher の同期が完了する前に quota を設定したい場合は `--force` を使用する。

```bash
cjobctl cluster set-quota --flavor cpu --cpu <new-total> --memory <new-total> --force
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

## 9. システムの停止・起動

メンテナンスや K8s クラスタ停止の前に、CJob システムを安全に停止する。PostgreSQL は停止しない（データ保全のため）。

### 9.1 システムの停止

```bash
cjobctl system stop
```

以下の処理が順番に実行される。

1. Submit API を replicas=0 にスケールダウン（新規ジョブ投入を遮断）
2. DB のジョブ状態を更新:
   - DISPATCHING / DISPATCHED → QUEUED に戻す
   - RUNNING → FAILED（`last_error: system shutdown`）
   - QUEUED → 変更なし（起動後に自動的に再 dispatch される）
3. 全ユーザー namespace の K8s Job を削除
4. Dispatcher、Watcher を replicas=0 にスケールダウン

ユーザーの `cjob.io/user-namespace` ラベルは変更されない。停止前後でアクセス権限は保持される。

確認プロンプトをスキップする場合:

```bash
cjobctl system stop --yes
```

### 9.2 システムの起動

```bash
cjobctl system start
```

Dispatcher（1）、Watcher（1）、Submit API（2）をスケールアップする。QUEUED のまま残っていたジョブは Dispatcher が自動的に再 dispatch する。

Submit API の replicas を変更する場合:

```bash
cjobctl system start --submit-api-replicas 3
```

起動後の確認:

```bash
cjobctl system status
```

### 9.3 K8s クラスタ停止を伴う場合

1. `cjobctl system stop` で CJob を停止する
2. K8s クラスタを停止する
3. K8s クラスタを起動する
4. PostgreSQL Pod が Ready になるまで待つ
5. `cjobctl system start` で CJob を起動する

### 9.4 コンポーネントの rolling restart

コンポーネントのイメージ更新や設定変更（`cjobctl config set` 後）を反映する場合に使用する。

```bash
# 単一コンポーネントの再起動
cjobctl system restart dispatcher
cjobctl system restart watcher
cjobctl system restart submit-api
```

`kubectl rollout restart` と同等の処理を実行する。Pod が順次入れ替わるため、Submit API（replicas >= 2）ではダウンタイムなしで更新できる。

## 10. パラメータチューニング

システムの挙動を調整する主要なパラメータは ConfigMap で管理される（§2.3 参照）。変更後は影響を受けるコンポーネントの再起動が必要。

### 10.1 パラメータ一覧と設計根拠

全パラメータの設定値一覧・各レイヤーの関係・設計根拠は [リソース設計](architecture/resources.md) §2 を参照。

### 10.2 スケジューリング調整

Dispatcher のジョブ dispatch 順序・頻度・公平性に関するパラメータの詳細と調整指針は [Dispatcher 設計](architecture/dispatcher.md) §1 を参照。主要なパラメータ：

| パラメータ | 調整の目的 |
|---|---|
| `DISPATCH_BUDGET_PER_NAMESPACE` | namespace あたりの同時アクティブジョブ数の上限 |
| `DISPATCH_BATCH_SIZE` | 1 サイクルあたりの dispatch 総数の上限 |
| `DISPATCH_ROUND_SIZE` | ラウンドロビンと DRF（消費量ベースの公平性）のバランス制御 |
| `FAIR_SHARE_WINDOW_DAYS` | DRF の消費量集計ウィンドウ日数 |

### 10.3 リソース制限の調整

クラスタのリソース制限（ResourceQuota、ClusterQueue nominalQuota 等）の設定値と調整方法は [リソース設計](architecture/resources.md) §1 を参照。ClusterQueue の nominalQuota 更新手順は本ガイドの §7.3 を参照。

## 付録: ソースコード参照

各コマンドが実行する SQL クエリの詳細は `ctl/src/cmd/` 配下のソースコードを参照。

| ソースファイル | 対応コマンド |
|---|---|
| `ctl/src/cmd/jobs.rs` | `cjobctl jobs` サブコマンド全般 |
| `ctl/src/cmd/usage.rs` | `cjobctl usage list / reset` |
| `ctl/src/cmd/counters.rs` | `cjobctl counters list` |
| `ctl/src/cmd/weight.rs` | `cjobctl weight` サブコマンド全般 |
| `ctl/src/cmd/cluster.rs` | `cjobctl cluster` サブコマンド全般 |
| `ctl/src/cmd/cli/deploy.rs` | `cjobctl cli deploy` |
| `ctl/src/cmd/cli/list.rs` | `cjobctl cli list` |
| `ctl/src/cmd/cli/remove.rs` | `cjobctl cli remove` |
| `ctl/src/cmd/cli/set_latest.rs` | `cjobctl cli set-latest` |
| `ctl/src/cmd/config/show.rs` | `cjobctl config show` |
| `ctl/src/cmd/config/set.rs` | `cjobctl config set` |
| `ctl/src/cmd/config/dump.rs` | `cjobctl config dump` |
| `ctl/src/cmd/user.rs` | `cjobctl user` サブコマンド全般 |
| `ctl/src/cmd/system/stop.rs` | `cjobctl system stop` |
| `ctl/src/cmd/system/start.rs` | `cjobctl system start` |
| `ctl/src/cmd/system/restart.rs` | `cjobctl system restart` |
| `ctl/src/cmd/system/status.rs` | `cjobctl system status` |
| `ctl/src/cmd/system/logs.rs` | `cjobctl system logs` |
| `ctl/src/cmd/db_migrate.rs` | `cjobctl db migrate` |

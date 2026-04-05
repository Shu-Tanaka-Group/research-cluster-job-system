# cjobctl 設計

## 1. 概要

`cjobctl` は CJob システムの管理者向け CLI ツールである。管理者のローカル PC 上で動作し、PostgreSQL への直接接続と Kubernetes API を通じてシステムの状態確認・設定変更を行う。

ユーザー向け CLI `cjob` が Submit API を経由するのに対し、`cjobctl` は DB と K8s API に直接アクセスする。

```
管理者 PC
├── cjobctl ──→ PostgreSQL（kubectl port-forward 経由・自動）
└── cjobctl ──→ Kubernetes API（kubeconfig 経由）
```

## 2. 技術スタック

| 項目 | 技術 |
|---|---|
| 言語 | Rust |
| CLI フレームワーク | Clap（derive） |
| DB クライアント | tokio-postgres |
| K8s クライアント | kube + k8s-openapi |
| 非同期ランタイム | tokio |
| 設定ファイル | TOML（toml crate） |

## 3. 接続方式

### 3.1 DB 接続

DB コマンドの実行時に `kubectl port-forward` を自動的に起動する。ローカルポートは OS に自動割り当て（ポート 0 指定）させ、既存プロセスとの競合を回避する。コマンド完了時に port-forward プロセスは自動終了する。

```
cjobctl → kubectl port-forward (自動起動、ランダムポート)
        → 127.0.0.1:<random> → svc/postgres:5432
        → tokio-postgres で接続
        → コマンド完了 → port-forward プロセス kill
```

`kubectl` が PATH に存在し、kubeconfig でクラスタにアクセスできることが前提となる。

### 3.2 K8s 接続

`kube::Client::try_default()` により kubeconfig から自動的にクライアントを構成する。port-forward は不要。

## 4. 設定ファイル

`~/.config/cjobctl/config.toml`:

```toml
[database]
database = "cjob"
user = "cjob"
password = "xxx"

[kubernetes]
namespace = "cjob-system"   # 省略時デフォルト
```

`host` / `port` は自動 port-forward が管理するため設定不要。

## 5. コマンド一覧

### 5.1 DB 状態確認

| コマンド | 概要 | 対象テーブル |
|---|---|---|
| `cjobctl jobs list [--namespace <ns>] [--status <s>] [--sort <field>] [--reverse] [-o wide]` | ジョブ一覧 | `jobs` |
| `cjobctl jobs status --namespace <ns> --job-id <id>` | 個別ジョブの詳細表示（`cjob status` と同等） | `jobs` |
| `cjobctl jobs summary` | namespace × ステータスのジョブ数（ピボットテーブル） | `jobs` |
| `cjobctl jobs stalled [--sort <field>] [--reverse]` | DISPATCHED のまま滞留しているジョブ | `jobs` |
| `cjobctl jobs remaining [--sort <field>] [--reverse]` | RUNNING ジョブの残り時間 | `jobs` |
| `cjobctl jobs cancel --namespace <ns> [--job-id <id> \| --status <s> \| --all]` | ジョブのキャンセル | `jobs` |
| `cjobctl counters list` | namespace ごとの job_id カウンター | `user_job_counters` |

#### ソートオプション

`jobs list`、`jobs stalled`、`jobs remaining` は `--sort` オプションでソートフィールドを変更できる。`--reverse` を併用すると降順になる。

| コマンド | 使用可能なソートフィールド | デフォルト |
|---|---|---|
| `jobs list` | `NAMESPACE`, `CREATED`, `DISPATCHED`, `STARTED`, `FINISHED` | `NAMESPACE`（namespace, job_id の複合順） |
| `jobs stalled` | `NAMESPACE`, `CREATED` | `CREATED`（dispatched_at 昇順） |
| `jobs remaining` | `NAMESPACE`, `CREATED` | `REMAINING`（remaining_sec 昇順） |

`--sort FINISHED`、`--sort DISPATCHED`、`--sort STARTED` を `stalled` / `remaining` で指定した場合はエラーとする（該当カラムが存在しないため）。

#### `-o wide` オプション

`jobs list` の表示カラムは NAMESPACE, JOB_ID, TYPE, STATUS, COMMAND, CREATED, FINISHED とする。TYPE は `completions IS NULL` のジョブを `job`、それ以外を `sweep` と表示する。

`-o wide`（`--output wide`）を指定すると、上記に加えて以下のカラムが追加される:

- **DISPATCHED**: ジョブの dispatch 時刻（DB の `dispatched_at` カラム、NULL の場合は `-` 表示）
- **STARTED**: ジョブの開始時刻（DB の `started_at` カラム、NULL の場合は `-` 表示）
- **FLAVOR**: 指定 ResourceFlavor 名（DB の `flavor` カラム）
- **CPU**: 指定 CPU リソース量（DB の `cpu` カラム）
- **MEMORY**: 指定メモリリソース量（DB の `memory` カラム）
- **GPU**: 指定 GPU 数（DB の `gpu` カラム、0 の場合は `-` 表示）
- **NODE**: ジョブ実行ノード名（DB の `node_name` カラム、NULL の場合は `-` 表示）

`-o wide` での時間列の表示順: CREATED → DISPATCHED → STARTED → FINISHED

`DISPATCHED`・`STARTED` は NULL を含む可能性があるため、`--sort` の NULL 処理は `FINISHED` と同様（`--reverse` 未指定時は `NULLS LAST`、指定時は `NULLS FIRST`）とする。

ノード名は Watcher が RUNNING 遷移時に Pod の `spec.nodeName` から取得し DB に記録する。一瞬で完了するジョブ（RUNNING を経由せず直接 SUCCEEDED/FAILED に遷移）の場合は、完了遷移時にフォールバックとして Pod から取得を試みる。QUEUED / DISPATCHED 等の未実行ジョブは `-` 表示となる。

### 5.2 リソース消費量

| コマンド | 概要 | 対象テーブル |
|---|---|---|
| `cjobctl usage list [--namespace <ns>]` | 日別消費量・7日ウィンドウ集計・DRF dominant share | `namespace_daily_usage`, `namespace_weights` |
| `cjobctl usage reset [--namespace <ns> \| --all]` | 消費量データの削除 | `namespace_daily_usage` |
| `cjobctl usage quota [--namespace <ns>]` | 全 namespace の ResourceQuota 使用状況 | `namespace_resource_quotas` + K8s namespace 一覧 |

`usage list` の Daily Usage はデフォルトで日付昇順（古い日付が上）で表示する。`--namespace` オプションで特定 namespace のデータのみに絞り込める（Daily / 7-Day Window / DRF すべてのセクションに適用）。

`usage list` の DRF dominant share 計算は Dispatcher（`server/src/cjob/dispatcher/scheduler.py`）と同一の式を使用する:

```
dominant_share = GREATEST(cpu_share, mem_share, gpu_share) / weight
```

クラスタのリソース総量は DB の `node_resources` テーブルから `SUM()` で取得する。テーブルが空の場合は dominant share 列を `N/A` と表示する（Dispatcher は DRF ソートを無効化して namespace 名順にフォールバックするが、cjobctl は表示ツールのため計算不能であることを明示する）。

#### `cjobctl usage quota`

全ユーザー namespace の ResourceQuota 使用状況を表示する。K8s API からユーザー namespace 一覧を取得し（`weight exclusive` と同じパターン）、DB の `namespace_resource_quotas` テーブルと突き合わせる。

- CPU は cores 表示（millicores / 1000）、`cjob usage`（#105）と統一
- Memory は GiB 表示（MiB / 1024）
- GPU は個数表示
- `updated_at` は相対時間（`Xm ago`, `Xh ago` 等）で鮮度を表示
- `--namespace` で特定 namespace にフィルタ可能
- DB に行がない namespace（ResourceQuota 未設定）は各列を `-` で表示
- ユーザー namespace が存在しない場合は "No user namespaces found." を表示

各列は動的カラム幅で整形する（ヘッダーとデータの最大幅に合わせて列幅を決定し、列間はスペース3つ）。

出力例:

```
Namespace      CPU (used/hard)   Memory (used/hard)   GPU (used/hard)   Updated
user-alice      20.0 / 300.0      80Gi / 1250Gi       0 / 4             2m ago
user-bob       260.0 / 300.0     800Gi / 1250Gi       1 / 4             2m ago
user-charlie   -                 -                    -                 -
```

### 5.3 namespace weight 管理

| コマンド | 概要 | 対象 |
|---|---|---|
| `cjobctl weight list` | 全 namespace の weight 一覧 | DB: `namespace_weights` |
| `cjobctl weight set <namespace> <weight>` | weight の設定（UPSERT） | DB: `namespace_weights` |
| `cjobctl weight reset <namespace>` | weight をデフォルト（1）に戻す | DB: `namespace_weights` |
| `cjobctl weight exclusive <namespace>` | 指定 namespace にクラスタを専有させる | DB + K8s |
| `cjobctl weight exclusive --release` | 専有モード解除 | DB: `namespace_weights` |

`weight exclusive` は K8s API で `cjob.io/user-namespace=true` ラベルを持つ namespace を列挙し、指定以外の全 namespace を weight = 0 に設定する。cjobctl は `config.toml` の `[kubernetes]` セクション内 `user_namespace_label` でラベルセレクタを変更できる。

### 5.4 クラスタリソース確認

| コマンド | 概要 | 対象 |
|---|---|---|
| `cjobctl cluster resources` | ノードごとの allocatable、クラスタ合計、ノード最大値（リジェクト閾値）を表示 | DB: `node_resources` |
| `cjobctl cluster flavor-usage` | ResourceFlavor ごとのリソース使用率を表示 | K8s: ClusterQueue |
| `cjobctl cluster show-quota` | ClusterQueue の nominalQuota を ResourceFlavor ごとに表示 | K8s: ClusterQueue |
| `cjobctl cluster set-quota --flavor <name> [--cpu <n>] [--memory <s>] [--gpu <n>] [--force]` | 指定 ResourceFlavor の nominalQuota を更新 | DB + K8s: ClusterQueue |

#### `cjobctl cluster resources`

出力例:

```
=== Node Resources ===
NODE              FLAVOR      CPU (cores)   Memory (GiB)   GPU   Updated
node-compute-01   cpu              64         256.0          0   2026-03-27 10:05:00
node-compute-02   cpu              64         256.0          0   2026-03-27 10:05:00
node-gpu-01       gpu-a100         32         128.0          4   2026-03-27 10:05:00

=== Cluster Totals (for DRF normalization) ===
CPU:    160 cores (160000m)
Memory: 640.0 GiB (655360 MiB)
GPU:    4

=== Per-Flavor Totals (set-quota reference) ===
FLAVOR      CPU (cores)   Memory (GiB)   GPU
cpu              128         512.0          0
gpu-a100          32         128.0          4

=== Per-Flavor Max Node Allocatable ===
FLAVOR      CPU (cores)   Memory (GiB)   GPU
cpu               64         256.0          0
gpu-a100          32         128.0          4
```

`Per-Flavor Totals` は `cjobctl cluster set-quota` のバリデーションが使用する値と一致する。CPU は各ノードの `cpu_millicores` を整数コアに切り下げてから合算（bin-packing 考慮）し、memory/GPU は単純合算。一方 `Cluster Totals (for DRF normalization)` は Dispatcher の DRF 正規化に使う cluster-wide の effective allocatable 合計（切り下げなし）を示す。

#### `cjobctl cluster flavor-usage`

ClusterQueue の各 ResourceFlavor について、nominalQuota に対する現在の予約済みリソース（`status.flavorsReservation`）の使用率を表示する。

出力例:

```
=== ResourceFlavor Usage (cjob-cluster-queue) ===
FLAVOR          RESOURCE          RESERVED    NOMINAL   USAGE
cpu             cpu                     48        256   18.8%
cpu             memory               192Gi     1000Gi   19.2%
cpu             nvidia.com/gpu           0          0       -
gpu-a100        cpu                     16         64   25.0%
gpu-a100        memory                64Gi      500Gi   12.8%
gpu-a100        nvidia.com/gpu           2          4   50.0%
```

#### `cjobctl cluster show-quota`

ClusterQueue の各 ResourceFlavor について nominalQuota を表示する。`lendingLimit` が設定されているリソースはその値も併記する。

出力例:

```
=== ClusterQueue nominalQuota (cjob-cluster-queue) ===

[cpu]
  CPU:    256
  Memory: 1000Gi
  GPU:    0

[gpu-a100]
  CPU:    64      (lendingLimit: 0)
  Memory: 500Gi   (lendingLimit: 0)
  GPU:    4        (lendingLimit: 0)
```

#### `cjobctl cluster set-quota`

指定した ResourceFlavor の nominalQuota を更新する。`--flavor` は必須で、更新対象の ResourceFlavor 名を指定する。`--cpu`、`--memory`、`--gpu` はすべてオプショナルで、指定されたリソースのみ更新される。少なくとも 1 つは指定が必要。

```bash
# cpu flavor の CPU とメモリを更新
cjobctl cluster set-quota --flavor cpu --cpu 256 --memory 1000Gi

# gpu-a100 flavor の GPU を更新
cjobctl cluster set-quota --flavor gpu-a100 --gpu 4
```

指定値は `node_resources` テーブルの allocatable 合計（指定 flavor のノードのみ）と比較してバリデーションされる。allocatable を超過する場合はエラーとなるが、`--force` で上書き可能。`--flavor` に指定する名前は Kueue ResourceFlavor の `metadata.name` と一致させる（DB の `node_resources.flavor` 列の値とも統一されている）。

CPU の allocatable 合計は、ノードごとの `cpu_millicores` を整数コアに切り下げてから合算する（`SUM((cpu_millicores / 1000) * 1000)`）。これは、各ノードの端数コア（例: DaemonSet Pod 差し引き後の 0.633 cores の余剰）が整数コアジョブの bin-packing 制約上使用できないため、nominalQuota は「各ノードの整数コア部分の合計」以下に抑える必要があるという考え方に基づく。メモリと GPU は切り下げず単純合算する。

### 5.5 K8s 状態確認

| コマンド | 概要 | K8s API |
|---|---|---|
| `cjobctl system status` | cjob-system の Pod 一覧 | `Api::<Pod>::list()` |
| `cjobctl system logs <component> [--tail <n>]` | コンポーネントログ | `Api::<Pod>::logs()` |
| `cjobctl config show` | cjob-config ConfigMap の内容 | `Api::<ConfigMap>::get()` |
| `cjobctl config set <key> <value> [--yes]` | ConfigMap の設定値を更新 | `Api::<ConfigMap>::patch()` |
| `cjobctl config set <key> --from-file <path> [--yes]` | ファイルから設定値を更新 | `Api::<ConfigMap>::patch()` |
| `cjobctl config dump` | ConfigMap を `kubectl apply` 可能な YAML で出力 | `Api::<ConfigMap>::get()` |

`system logs` の有効なコンポーネント名: `dispatcher`, `watcher`, `submit-api`。Pod のラベル `app=<component>` で特定する。

#### `cjobctl config set`

ConfigMap `cjob-config` の指定キーの値を更新する。変更内容を表示した上で `[y/N]` の確認プロンプトを挟む。`--yes` で確認をスキップ可能。

```bash
# スカラー値の更新
$ cjobctl config set DISPATCH_BATCH_SIZE 100
DISPATCH_BATCH_SIZE: 50 → 100
Proceed? [y/N] y

Updated 'DISPATCH_BATCH_SIZE' in cjob-config.

Restart the following component(s) to apply:
  cjobctl system restart dispatcher

# JSON 値の更新（ファイルから）
$ cjobctl config set RESOURCE_FLAVORS --from-file flavors.json
RESOURCE_FLAVORS: [{"name":"cpu",...}] → [{"name":"cpu",...},{"name":"gpu",...}]
Proceed? [y/N] y

Updated 'RESOURCE_FLAVORS' in cjob-config.

Restart the following component(s) to apply:
  cjobctl system restart dispatcher
  cjobctl system restart watcher
  cjobctl system restart submit-api
```

**バリデーション:**

CLI 側で以下のバリデーションを行う:

- キーが ConfigMap に存在すること（不明なキーは拒否）
- 更新不可キー（`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`）は拒否（DB 接続変更はインフラ作業が必要なため）
- 値の型チェック:
  - 整数型キー: `i64` としてパース可能であること
  - 真偽値型キー: `true` または `false`（大文字小文字不問、保存時は小文字に正規化）
  - JSON 型キー: 有効な JSON であること
  - 文字列型キー: 常に有効

**`value` と `--from-file` の排他:**

`value`（位置引数）と `--from-file` は同時に指定できない。`--from-file` 指定時は `value` を省略する。どちらも指定されない場合はエラー。

**キーとコンポーネントの対応:**

更新後、影響を受けるコンポーネントの再起動コマンドを表示する。各キーとコンポーネントの対応は以下の通り:

| キー | 型 | コンポーネント |
|---|---|---|
| `DISPATCH_BUDGET_PER_NAMESPACE` | int | dispatcher |
| `DISPATCH_BATCH_SIZE` | int | dispatcher |
| `DISPATCH_FETCH_MULTIPLIER` | int | dispatcher |
| `DISPATCH_ROUND_SIZE` | int | dispatcher |
| `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` | int | dispatcher, watcher |
| `DISPATCH_RETRY_INTERVAL_SEC` | int | dispatcher |
| `DISPATCH_MAX_RETRIES` | int | dispatcher |
| `GAP_FILLING_ENABLED` | bool | dispatcher |
| `GAP_FILLING_STALL_THRESHOLD_SEC` | int | dispatcher |
| `FAIR_SHARE_WINDOW_DAYS` | int | dispatcher, submit-api |
| `RESOURCE_FLAVORS` | json | dispatcher, watcher, submit-api |
| `DEFAULT_FLAVOR` | string | submit-api |
| `NODE_RESOURCE_SYNC_INTERVAL_SEC` | int | watcher |
| `CLUSTER_QUEUE_NAME` | string | watcher |
| `RESOURCE_QUOTA_NAME` | string | watcher |
| `RESOURCE_QUOTA_SYNC_INTERVAL_SEC` | int | watcher |
| `USER_NAMESPACE_LABEL` | string | watcher |
| `MAX_QUEUED_JOBS_PER_NAMESPACE` | int | submit-api |
| `MAX_SWEEP_COMPLETIONS` | int | submit-api |
| `DEFAULT_TIME_LIMIT_SECONDS` | int | submit-api |
| `MAX_TIME_LIMIT_SECONDS` | int | submit-api |
| `LOG_BASE_DIR` | string | submit-api |
| `CLI_BINARY_DIR` | string | submit-api |
| `KUEUE_LOCAL_QUEUE_NAME` | string | dispatcher |
| `WORKSPACE_MOUNT_PATH` | string | dispatcher |
| `TTL_SECONDS_AFTER_FINISHED` | int | dispatcher |
| `JOB_NODE_TAINT` | string | dispatcher |
| `WATCHER_METRICS_PORT` | int | watcher |
| `LOG_LEVEL` | string | dispatcher, watcher, submit-api |

**更新不可キー:**

| キー | 理由 |
|---|---|
| `POSTGRES_HOST` | DB 接続変更はインフラ作業が必要 |
| `POSTGRES_PORT` | DB 接続変更はインフラ作業が必要 |
| `POSTGRES_DB` | DB 接続変更はインフラ作業が必要 |
| `POSTGRES_USER` | DB 接続変更はインフラ作業が必要 |
| `POSTGRES_PASSWORD` | DB 接続変更はインフラ作業が必要 |

#### `cjobctl config dump`

ConfigMap `cjob-config` の内容を `kubectl apply -f` 可能なクリーンな YAML 形式で標準出力に出力する。バックアップや別環境への適用に使用する。

管理フィールド（`managedFields`, `resourceVersion`, `uid`, `creationTimestamp`, `annotations` 内の `kubectl.kubernetes.io/*`）は除去する。

```bash
$ cjobctl config dump > cjob-config-backup.yaml

# 復元
$ kubectl apply -f cjob-config-backup.yaml
```

### 5.6 CLI バイナリの配布

| コマンド | 概要 | 対象 |
|---|---|---|
| `cjobctl cli list` | PVC 上の登録済みバージョン一覧を表示する | K8s Pod + PVC |
| `cjobctl cli deploy --binary <path> --version <version> [--release]` | CLI バイナリを PVC に配置する | K8s Pod + PVC |
| `cjobctl cli remove <version>...` | PVC 上の指定バージョンのバイナリを削除する（複数指定可） | K8s Pod + PVC |
| `cjobctl cli set-latest <version>` | latest バージョンポインタを変更する | K8s Pod + PVC |

すべてのサブコマンドは一時 Pod（busybox）+ `kubectl exec` のパターンで PVC を操作する。一時 Pod には最小イメージ（`busybox`）を使用し、PVC の `cli-binary` を `/cli-binary` にマウントする。

使用例は [deployment.md](../deployment.md) §4.1 および [operations.md](../operations.md) §8 を参照。

#### `cjobctl cli list`

PVC 上のディレクトリ構造から登録済みバージョンの一覧を表示する。

```
$ cjobctl cli list
VERSION            LATEST
1.3.0-beta.1
1.3.0              ← latest
1.2.0
1.1.0
```

内部処理:
1. 一時 Pod を起動する
2. `ls /cli-binary/` でバージョンディレクトリの一覧を取得する
3. `cat /cli-binary/latest` で latest バージョンを取得する
4. semver 降順でソートし、latest マーカー付きで表示する
5. 一時 Pod を削除する

#### `cjobctl cli deploy`

内部処理:
1. `kubectl run` で `cli-binary` PVC（ReadWriteMany）をマウントした一時 Pod を起動する
2. `kubectl cp` でバイナリを一時 Pod 内の `/cli-binary/<version>/cjob` にコピーする
3. 一時 Pod 内で `chmod +x` を実行する
4. `--release` オプション指定時のみ `latest` ファイルを更新する
5. 一時 Pod を削除する

`--release` はプレリリース版（バージョン文字列に `-` を含むもの）との併用不可。プレリリース判定はバージョン文字列に `-` を含むかどうかで行う。

```bash
# バイナリを配置するのみ（latest は更新されない）
$ cjobctl cli deploy --binary ./cjob --version 1.3.0
Deployed v1.3.0 (latest unchanged: 1.2.0)

# バイナリ配置 + latest を更新
$ cjobctl cli deploy --binary ./cjob --version 1.3.0 --release
Deployed v1.3.0 (latest updated)

# ベータ版の配置（--release は使用不可）
$ cjobctl cli deploy --binary ./cjob --version 1.3.1-beta.1
Deployed v1.3.1-beta.1 (latest unchanged: 1.3.0)

$ cjobctl cli deploy --binary ./cjob --version 1.3.1-beta.1 --release
Error: Cannot use --release with pre-release version 1.3.1-beta.1.
```

#### `cjobctl cli set-latest`

PVC 上の `latest` ファイルを指定バージョンに変更する。バイナリの配置は行わない。誤って latest を更新してしまった場合や、問題のあるバージョンからロールバックする場合に使用する。

```bash
# latest を 1.2.0 に変更
$ cjobctl cli set-latest 1.2.0
Latest updated to v1.2.0.

# 存在しないバージョンはエラー
$ cjobctl cli set-latest 9.9.9
Error: Version 9.9.9 not found on PVC. Deploy it first.

# プレリリース版は指定不可
$ cjobctl cli set-latest 1.3.0-beta.1
Error: Cannot set pre-release version 1.3.0-beta.1 as latest.
```

内部処理:
1. 一時 Pod を起動する
2. 指定バージョンのディレクトリが存在するか確認する
3. `echo "<version>" > /cli-binary/latest` で latest ファイルを更新する
4. 一時 Pod を削除する

#### `cjobctl cli remove`

PVC 上の指定バージョンのバイナリディレクトリを削除する。

```bash
# 単一バージョンの削除
$ cjobctl cli remove 1.1.0
Removed CLI v1.1.0.

# 複数バージョンの同時削除
$ cjobctl cli remove 1.0.0 1.1.0
Removed 2 versions.

$ cjobctl cli remove 1.3.0
Error: Cannot remove version 1.3.0: it is the current latest.
```

内部処理:
1. 一時 Pod を起動する
2. `cat /cli-binary/latest` で latest バージョンを取得する
3. 指定バージョンのバリデーション（latest の場合はエラー、存在しない場合はエラー）
4. 確認プロンプトを表示する
5. 各バージョンの `rm -rf /cli-binary/<version>` で削除する
6. 一時 Pod を削除する

### 5.7 ユーザー管理

| コマンド | 概要 | 対象 |
|---|---|---|
| `cjobctl user list [--enabled \| --disabled]` | ユーザー namespace 一覧 | K8s: Namespace |
| `cjobctl user enable --namespace <ns>...` | CJob を有効化（複数指定可） | K8s: Namespace |
| `cjobctl user disable --namespace <ns>...` | CJob を無効化（複数指定可） | K8s: Namespace |

ユーザー namespace は `type=user` ラベルを持つ Namespace として識別する。各 namespace の `cjob.io/username` アノテーションからユーザー名を、`cjob.io/user-namespace` ラベルの値から有効/無効状態を取得する。

#### `cjobctl user list`

`type=user` ラベルを持つ全 namespace を一覧表示する。

```
$ cjobctl user list
NAMESPACE          USERNAME       ENABLED
user-alice         alice          true
user-bob           bob            true
user-charlie       charlie        false
```

- `--enabled`: `cjob.io/user-namespace` ラベルの値が `"true"` の namespace のみ表示
- `--disabled`: `cjob.io/user-namespace` ラベルの値が `"true"` でない namespace のみ表示
- `--enabled` と `--disabled` は排他（同時指定不可）

#### `cjobctl user enable`

指定 namespace に `cjob.io/user-namespace: "true"` ラベルを設定する。複数 namespace を同時に指定可能。

実行前に全 namespace を事前バリデーションし、存在しない namespace や `type=user` ラベルを持たない namespace が含まれる場合はエラーを返す。バリデーションが通るまでラベルの変更は一切行わない。

```bash
$ cjobctl user enable --namespace user-charlie
Enabled CJob for namespace 'user-charlie'.

$ cjobctl user enable --namespace user-alice user-bob
Enabled CJob for namespace 'user-alice'.
Enabled CJob for namespace 'user-bob'.
```

#### `cjobctl user disable`

指定 namespace の `cjob.io/user-namespace` ラベルの値を `"false"` に変更する。複数 namespace を同時に指定可能。

事前バリデーションは `enable` と同様（存在確認 + `type=user` ラベル検証）。

```bash
$ cjobctl user disable --namespace user-bob
Disabled CJob for namespace 'user-bob'.

$ cjobctl user disable --namespace user-alice user-bob
Disabled CJob for namespace 'user-alice'.
Disabled CJob for namespace 'user-bob'.
```

### 5.8 DB スキーマ管理

| コマンド | 概要 |
|---|---|
| `cjobctl db migrate` | 冪等なスキーママイグレーション実行 |

`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN IF NOT EXISTS` を使用しており、何度実行しても安全。

### 5.9 システム管理

| コマンド | 概要 | 対象 |
|---|---|---|
| `cjobctl system stop [--yes]` | CJob システムの安全な停止 | DB + K8s: Deployment |
| `cjobctl system start [--submit-api-replicas <n>]` | CJob システムの起動 | K8s: Deployment |
| `cjobctl system restart <component>` | コンポーネントの rolling restart | K8s: Deployment |
| `cjobctl system status` | cjob-system の Pod 一覧 | K8s: Pod |
| `cjobctl system logs <component> [--tail <n>]` | コンポーネントログ | K8s: Pod |

#### `cjobctl system stop`

CJob システムを安全に停止する。メンテナンスや K8s クラスタ停止の前に実行する。PostgreSQL は停止しない。

停止シーケンス:

1. アクティブジョブ数を表示し、確認プロンプトを表示する（`--yes` でスキップ可）
2. Submit API を replicas=0 にスケールダウンし、新規ジョブ投入を遮断する
3. DB のジョブ状態を更新する:
   - DISPATCHING → QUEUED（`retry_after = NULL`, `retry_count = 0` にリセット）
   - DISPATCHED → QUEUED
   - RUNNING → FAILED（`last_error = 'system shutdown'`, `finished_at = NOW()`）
   - QUEUED → 変更なし
4. 全ユーザー namespace の K8s Job（`cjob.io/job-id` ラベル付き）を `propagationPolicy=Background` で削除する
5. Dispatcher、Watcher を replicas=0 にスケールダウンする

namespace の `cjob.io/user-namespace` ラベルは変更しない。再起動前後でユーザーのアクセス権限は保持される。

QUEUED に戻されたジョブは、システム起動後に Dispatcher が自動的に再 dispatch する。DISPATCHING のリセットは Dispatcher の起動時初期化（[dispatcher.md](dispatcher.md) §2.6）と同等の処理である。

```bash
$ cjobctl system stop
Active jobs: 15 (QUEUED: 8, DISPATCHING: 1, DISPATCHED: 2, RUNNING: 4)
This will:
  - Scale down submit-api, dispatcher, watcher to 0 replicas
  - Revert 3 DISPATCHING/DISPATCHED jobs to QUEUED
  - Fail 4 RUNNING jobs (last_error: system shutdown)
  - Delete K8s Jobs in all user namespaces
  - 8 QUEUED jobs will be re-dispatched on next start
Proceed? [y/N] y
Scaled down submit-api to 0 replicas.
Reverted 1 DISPATCHING job(s) to QUEUED.
Reverted 2 DISPATCHED job(s) to QUEUED.
Failed 4 RUNNING job(s).
Deleted 6 K8s Job(s).
Scaled down dispatcher to 0 replicas.
Scaled down watcher to 0 replicas.
CJob system stopped. PostgreSQL remains running.
```

#### `cjobctl system start`

CJob システムを起動する。各 Deployment をデフォルトの replicas にスケールアップする。

- Dispatcher: 1
- Watcher: 1
- Submit API: 2（`--submit-api-replicas` で変更可）

```bash
$ cjobctl system start
Scaled up dispatcher to 1 replica(s).
Scaled up watcher to 1 replica(s).
Scaled up submit-api to 2 replica(s).
CJob system started. Use 'cjobctl system status' to check pod status.
```

#### `cjobctl system restart`

指定したコンポーネントの Deployment を rolling restart する。`kubectl rollout restart` と同等の処理で、Pod template の annotation `kubectl.kubernetes.io/restartedAt` に現在時刻を設定して K8s の rolling update をトリガーする。

有効なコンポーネント名: `dispatcher`, `watcher`, `submit-api`

```bash
$ cjobctl system restart submit-api
Restarting submit-api... (use 'cjobctl system status' to check)
```

## 6. 破壊的操作の安全策

以下のコマンドは実行前に `[y/N]` の確認プロンプトを表示する:

- `cjobctl jobs cancel`
- `cjobctl usage reset`
- `cjobctl weight exclusive --release`
- `cjobctl cli remove`
- `cjobctl system stop`
- `cjobctl config set`

## 7. ソースコード構成

```
ctl/
├── Cargo.toml
└── src/
    ├── main.rs            # Clap 定義 + コマンドディスパッチ
    ├── config.rs          # 設定ファイル読み込み
    ├── db.rs              # port-forward 自動起動 + DB 接続
    ├── k8s.rs             # K8s クライアント初期化
    └── cmd/
        ├── mod.rs
        ├── cli/
        │   ├── mod.rs         # 共有ユーティリティ + サブモジュール宣言
        │   ├── deploy.rs      # cli deploy (ベータ版サポート含む)
        │   ├── list.rs        # cli list
        │   ├── remove.rs      # cli remove
        │   └── set_latest.rs  # cli set-latest
        ├── system/
        │   ├── mod.rs         # 共有定数 + scale_deployment ヘルパー
        │   ├── stop.rs        # system stop
        │   ├── start.rs       # system start
        │   ├── restart.rs     # system restart (rolling update)
        │   ├── status.rs      # system status (Pod 一覧)
        │   └── logs.rs        # system logs (コンポーネントログ)
        ├── config/
        │   ├── mod.rs         # サブモジュール宣言
        │   ├── show.rs        # config show (ConfigMap 表示)
        │   ├── set.rs         # config set (ConfigMap 更新)
        │   └── dump.rs        # config dump (ConfigMap YAML 出力)
        ├── jobs.rs        # jobs list/stalled/remaining/summary
        ├── usage.rs       # usage list/reset + ClusterTotals
        ├── counters.rs    # counters list
        ├── weight.rs      # weight list/set/reset/exclusive
        ├── cluster.rs     # cluster resources
        ├── db_migrate.rs  # db migrate
        └── user.rs        # user list/enable/disable
```

各コマンドが実行する SQL クエリは `ctl/src/cmd/` 配下の対応ファイルを参照。

## 8. cjob CLI との違い

| | cjob（ユーザー CLI） | cjobctl（管理 CLI） |
|---|---|---|
| 対象ユーザー | 一般ユーザー | クラスタ管理者 |
| 実行環境 | K8s クラスタ内の User Pod | 管理者のローカル PC |
| 通信先 | Submit API（HTTP） | PostgreSQL（直接）+ K8s API |
| 認証 | ServiceAccount JWT | kubeconfig + DB パスワード |
| 操作範囲 | 自身の namespace のジョブのみ | 全 namespace |
| 配布方法 | `cjob update`（Submit API 経由） | ソースからビルド |

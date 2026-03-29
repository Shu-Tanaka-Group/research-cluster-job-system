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
| `cjobctl jobs summary` | namespace × ステータスのジョブ数（ピボットテーブル） | `jobs` |
| `cjobctl jobs stalled [--sort <field>] [--reverse]` | DISPATCHED のまま滞留しているジョブ | `jobs` |
| `cjobctl jobs remaining [--sort <field>] [--reverse]` | RUNNING ジョブの残り時間 | `jobs` |
| `cjobctl jobs cancel --namespace <ns> [--job-id <id> \| --status <s> \| --all]` | ジョブのキャンセル | `jobs` |
| `cjobctl counters list` | namespace ごとの job_id カウンター | `user_job_counters` |

#### ソートオプション

`jobs list`、`jobs stalled`、`jobs remaining` は `--sort` オプションでソートフィールドを変更できる。`--reverse` を併用すると降順になる。

| コマンド | 使用可能なソートフィールド | デフォルト |
|---|---|---|
| `jobs list` | `NAMESPACE`, `CREATED`, `FINISHED` | `NAMESPACE`（namespace, job_id の複合順） |
| `jobs stalled` | `NAMESPACE`, `CREATED` | `CREATED`（dispatched_at 昇順） |
| `jobs remaining` | `NAMESPACE`, `CREATED` | `REMAINING`（remaining_sec 昇順） |

`--sort FINISHED` を `stalled` / `remaining` で指定した場合はエラーとする（該当カラムが存在しないため）。

#### `-o wide` オプション

`jobs list` に `-o wide`（`--output wide`）を指定すると、通常の表示に加えて以下のカラムが追加される:

- **CPU**: 指定 CPU リソース量（DB の `cpu` カラム）
- **MEMORY**: 指定メモリリソース量（DB の `memory` カラム）
- **GPU**: 指定 GPU 数（DB の `gpu` カラム、0 の場合は `-` 表示）
- **NODE**: ジョブ実行ノード名（DB の `node_name` カラム、NULL の場合は `-` 表示）

ノード名は Watcher が RUNNING 遷移時に Pod の `spec.nodeName` から取得し DB に記録する。QUEUED / DISPATCHED 等のジョブは `-` 表示となる。

### 5.2 リソース消費量

| コマンド | 概要 | 対象テーブル |
|---|---|---|
| `cjobctl usage list [--namespace <ns>]` | 日別消費量・7日ウィンドウ集計・DRF dominant share | `namespace_daily_usage`, `namespace_weights` |
| `cjobctl usage reset [--namespace <ns> \| --all]` | 消費量データの削除 | `namespace_daily_usage` |

`usage list` の Daily Usage はデフォルトで日付昇順（古い日付が上）で表示する。`--namespace` オプションで特定 namespace のデータのみに絞り込める（Daily / 7-Day Window / DRF すべてのセクションに適用）。

`usage list` の DRF dominant share 計算は Dispatcher（`server/src/cjob/dispatcher/scheduler.py`）と同一の式を使用する:

```
dominant_share = GREATEST(cpu_share, mem_share, gpu_share) / weight
```

クラスタのリソース総量は DB の `node_resources` テーブルから `SUM()` で取得する。テーブルが空の場合は dominant share 列を `N/A` と表示する（Dispatcher は DRF ソートを無効化して namespace 名順にフォールバックするが、cjobctl は表示ツールのため計算不能であることを明示する）。

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

| コマンド | 概要 | 対象テーブル |
|---|---|---|
| `cjobctl cluster resources` | ノードごとの allocatable、クラスタ合計、ノード最大値（リジェクト閾値）を表示 | `node_resources` |
| `cjobctl cluster flavor-usage` | ResourceFlavor ごとのリソース使用率を表示 | K8s: ClusterQueue |

出力例:

```
=== Node Resources ===
NODE              CPU (cores)   Memory (GiB)   GPU   Updated
node-compute-01        64         256.0          0   2026-03-27 10:05:00
node-compute-02        64         256.0          0   2026-03-27 10:05:00
node-gpu-01            32         128.0          4   2026-03-27 10:05:00

=== Cluster Totals (for DRF normalization) ===
CPU:    160 cores (160000m)
Memory: 640.0 GiB (655360 MiB)
GPU:    4

=== Max per Node (Submit API rejection threshold) ===
CPU:    64 cores (64000m)
Memory: 256.0 GiB (262144 MiB)
GPU:    4
```

#### `cjobctl cluster flavor-usage`

ClusterQueue の各 ResourceFlavor について、nominalQuota に対する現在の予約済みリソース（`status.flavorsReservation`）の使用率を表示する。

出力例:

```
=== ResourceFlavor Usage (cjob-cluster-queue) ===
FLAVOR          RESOURCE          RESERVED    NOMINAL   USAGE
cpu-flavor      cpu                     48        256   18.8%
cpu-flavor      memory               192Gi     1000Gi   19.2%
cpu-flavor      nvidia.com/gpu           0          0       -
gpu-flavor      cpu                     16         64   25.0%
gpu-flavor      memory                64Gi      500Gi   12.8%
gpu-flavor      nvidia.com/gpu           2          4   50.0%
```

### 5.5 K8s 状態確認

| コマンド | 概要 | K8s API |
|---|---|---|
| `cjobctl status` | cjob-system の Pod 一覧 | `Api::<Pod>::list()` |
| `cjobctl logs <component> [--tail <n>]` | コンポーネントログ | `Api::<Pod>::logs()` |
| `cjobctl config show` | cjob-config ConfigMap の内容 | `Api::<ConfigMap>::get()` |

`logs` の有効なコンポーネント名: `dispatcher`, `watcher`, `submit-api`。Pod のラベル `app=<component>` で特定する。

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

### 5.7 DB スキーマ管理

| コマンド | 概要 |
|---|---|
| `cjobctl db migrate` | 冪等なスキーママイグレーション実行 |

`CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ADD COLUMN IF NOT EXISTS` を使用しており、何度実行しても安全。

## 6. 破壊的操作の安全策

以下のコマンドは実行前に `[y/N]` の確認プロンプトを表示する:

- `cjobctl jobs cancel`
- `cjobctl usage reset`
- `cjobctl weight exclusive --release`
- `cjobctl cli remove`

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
        ├── jobs.rs        # jobs list/stalled/remaining/summary
        ├── usage.rs       # usage list/reset + ClusterTotals
        ├── counters.rs    # counters list
        ├── weight.rs      # weight list/set/reset/exclusive
        ├── cluster.rs     # cluster resources
        ├── cli_deploy.rs  # cli deploy (ベータ版サポート含む)
        ├── cli_list.rs    # cli list
        ├── cli_remove.rs  # cli remove
        ├── cli_set_latest.rs # cli set-latest
        ├── db_migrate.rs  # db migrate
        ├── status.rs      # K8s Pod 状態
        ├── logs.rs        # K8s コンポーネントログ
        └── config_show.rs # K8s ConfigMap 表示
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

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
| `cjobctl jobs list [--namespace <ns>] [--status <s>]` | ジョブ一覧 | `jobs` |
| `cjobctl jobs summary` | namespace × ステータスのジョブ数（ピボットテーブル） | `jobs` |
| `cjobctl jobs stalled` | DISPATCHED のまま滞留しているジョブ | `jobs` |
| `cjobctl jobs remaining` | RUNNING ジョブの残り時間 | `jobs` |
| `cjobctl jobs cancel --namespace <ns> [--job-id <id> \| --status <s> \| --all]` | ジョブのキャンセル | `jobs` |
| `cjobctl counters list` | namespace ごとの job_id カウンター | `user_job_counters` |

### 5.2 リソース消費量

| コマンド | 概要 | 対象テーブル |
|---|---|---|
| `cjobctl usage list` | 日別消費量・7日ウィンドウ集計・DRF dominant share | `namespace_daily_usage`, `namespace_weights` |
| `cjobctl usage reset [--namespace <ns> \| --all]` | 消費量データの削除 | `namespace_daily_usage` |

`usage list` の DRF dominant share 計算は Dispatcher（`server/src/cjob/dispatcher/scheduler.py`）と同一の式を使用する:

```
dominant_share = GREATEST(cpu_share, mem_share, gpu_share) / weight
```

クラスタのリソース総量は DB の `node_resources` テーブルから `SUM()` で取得する。テーブルが空の場合はデフォルト値を使用する。

### 5.3 namespace weight 管理

| コマンド | 概要 | 対象 |
|---|---|---|
| `cjobctl weight list` | 全 namespace の weight 一覧 | DB: `namespace_weights` |
| `cjobctl weight set <namespace> <weight>` | weight の設定（UPSERT） | DB: `namespace_weights` |
| `cjobctl weight reset <namespace>` | weight をデフォルト（1）に戻す | DB: `namespace_weights` |
| `cjobctl weight exclusive <namespace>` | 指定 namespace にクラスタを専有させる | DB + K8s |
| `cjobctl weight exclusive --release` | 専有モード解除 | DB: `namespace_weights` |

`weight exclusive` は K8s API で `cjob.io/user-namespace=true` ラベルを持つ namespace を列挙し、指定以外の全 namespace を weight = 0 に設定する。

### 5.4 クラスタリソース確認

| コマンド | 概要 | 対象テーブル |
|---|---|---|
| `cjobctl cluster resources` | ノードごとの allocatable、クラスタ合計、ノード最大値（リジェクト閾値）を表示 | `node_resources` |

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
| `cjobctl cli deploy --binary <path> --version <version>` | CLI バイナリを PVC に配置する | K8s Pod + PVC |

内部処理:
1. `kubectl run` で `cli-binary` PVC（ReadWriteMany）をマウントした一時 Pod を起動する
2. `kubectl cp` でバイナリを一時 Pod 内の `/cli-binary/<version>/cjob` にコピーする
3. 一時 Pod 内で `chmod +x` を実行する
4. 一時 Pod 内で `echo "<version>" > /cli-binary/latest` を実行する
5. 一時 Pod を削除する

一時 Pod には最小イメージ（`busybox`）を使用し、PVC の `cli-binary` を `/cli-binary` にマウントする。

使用例は [deployment.md](../deployment.md) §4.1 および [operations.md](../operations.md) §8 を参照。

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
        ├── cli_deploy.rs  # cli deploy
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

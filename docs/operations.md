# 運用ガイド

管理操作は `cjobctl` CLI で行う。セットアップは [ビルド手順](build.md) を参照。

各コマンドが実行する SQL クエリの詳細は `ctl/src/cmd/` 配下のソースコードを参照。

| ソースファイル | 対応コマンド |
|---|---|
| `ctl/src/cmd/jobs.rs` | `cjobctl jobs` サブコマンド全般 |
| `ctl/src/cmd/usage.rs` | `cjobctl usage list / reset` |
| `ctl/src/cmd/counters.rs` | `cjobctl counters list` |
| `ctl/src/cmd/weight.rs` | `cjobctl weight` サブコマンド全般 |
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

## 5. DB スキーマの更新

バージョンアップ時に新しいテーブルやカラムを追加する場合。`CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` により冪等に実行できる。

```bash
cjobctl db migrate
```

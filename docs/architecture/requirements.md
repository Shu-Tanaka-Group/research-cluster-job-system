# 機能要件・使用例

## 1. 提供したいジョブキューシステムの機能

### 1.1 ユーザー向け基本機能

本システムが提供する主要機能は次の通りである。

- シェルコマンドをジョブとして投入する
- 投入済みジョブの一覧を確認する
- 個別ジョブの状態を確認する
- ジョブをキャンセルする
- ジョブのログを確認する（リアルタイム追跡含む）
- 将来的に workflow engine から API 経由で利用できる

### 1.2 ジョブ投入時に再現したい情報

ユーザーがジョブを投入した時点の以下の情報をジョブ実行時に再現する。

- 作業ディレクトリ (`cwd`)
- export 済み環境変数（仮想環境の `PATH` / `VIRTUAL_ENV` を含む）
- 実行コマンド文字列

### 1.3 実行制御上の機能

- ジョブの一時保管
- ジョブの Kubernetes Job への変換
- Kueue による admission 制御
- namespace ごとの dispatch 数制御
- namespace ごとの ResourceQuota による意図しない無制限消費の防止（安全網）
- 実行状態の追跡
- Kubernetes Job / Pod 状態と内部状態 DB の整合

## 2. ジョブキューシステムの使用例

### 2.1 単一ジョブの投入

```bash
cjob add -- python main.py --alpha 0.1 --beta 16
```

### 2.2 シェルスクリプトの実行

```bash
cjob add -- bash run_experiment.sh case001
```

### 2.3 仮想環境を利用した実行

```bash
source /home/jovyan/myenv/bin/activate
cjob add -- python main.py --config config.yaml
# PATH / VIRTUAL_ENV が export 済みのため Job Pod で venv が再現される
```

### 2.4 ジョブ一覧表示

```bash
cjob list
```

### 2.5 状態確認

```bash
cjob status <job-id>
```

### 2.6 キャンセル

```bash
cjob cancel <job-id>
```

### 2.7 ログ取得

```bash
# 完了後に確認
cjob logs <job-id>

# リアルタイム追跡
cjob logs --follow <job-id>
```

### 2.8 完了済みジョブの削除

```bash
# 単体指定
cjob delete 5

# 範囲指定・複数指定
cjob delete 1-5
cjob delete 1,3,5
cjob delete 1-5,8,10-12

# 完了済みジョブを全て削除（実行中ジョブはスキップ）
cjob delete --all
```

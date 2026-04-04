# CLI 設計

## 1. 基本コマンド

```bash
cjob add [--cpu <cpu>] [--memory <memory>] [--flavor <name>] [--gpu <N>] [--time-limit <duration>] -- <command...>
cjob sweep -n <count> --parallel <n> [--flavor <name>] [--gpu <N>] [--time-limit <duration>] -- <command...>
cjob list [--status <status>] [--time-limit <range>] [--format ids] [--limit <n>] [--all] [--reverse]
cjob status <job-id>
cjob cancel <job-id>              # 単体指定
cjob cancel <start>-<end>         # 範囲指定（例: 1-10）
cjob cancel <id>,<id>,...         # 個別複数指定（例: 1,3,5）
cjob cancel <start>-<end>,<id>,.. # 組み合わせ（例: 1-5,8,10-12）
cjob delete <job-id>              # 単体指定
cjob delete <start>-<end>         # 範囲指定（例: 1-10）
cjob delete <id>,<id>,...         # 個別複数指定（例: 1,3,5）
cjob delete <start>-<end>,<id>,.. # 組み合わせ（例: 1-5,8,10-12）
cjob delete --all                 # 完了済みジョブを全て削除
cjob hold <job-id>                # 単体指定
cjob hold <start>-<end>           # 範囲指定（例: 1-10）
cjob hold <id>,<id>,...           # 個別複数指定（例: 1,3,5）
cjob hold <start>-<end>,<id>,..   # 組み合わせ（例: 1-5,8,10-12）
cjob hold --all                   # QUEUED 状態のジョブを全て保留
cjob release <job-id>             # 単体指定
cjob release <start>-<end>        # 範囲指定（例: 1-10）
cjob release <id>,<id>,...        # 個別複数指定（例: 1,3,5）
cjob release <start>-<end>,<id>,.. # 組み合わせ（例: 1-5,8,10-12）
cjob release --all                # HELD 状態のジョブを全て解除
cjob reset
cjob logs <job-id>
cjob logs --follow <job-id>
cjob logs <job-id> --index <n>           # sweep: 特定インデックスのログ表示
cjob logs --follow <job-id> --index <n>  # sweep: 特定インデックスのログ追跡
cjob usage
cjob flavor list                         # 利用可能な flavor 一覧
cjob flavor info <name>                  # 指定 flavor のリソース上限
cjob update
cjob config list                              # 全設定を表示
cjob config add <table> <key> <value>         # リスト型の設定に要素を追加
cjob config remove <table> <key> <value>      # リスト型の設定から要素を削除
cjob config set <table> <key> <value>         # スカラー型の設定値を変更
cjob config unset <table> <key>               # スカラー型の設定値を削除
```

## 2. 使用例

### 2.1 単一ジョブの投入

```bash
cjob add -- python main.py --alpha 0.1 --beta 16

# GPU ジョブの投入（flavor を指定）
cjob add --flavor gpu-a100 --gpu 1 -- python train.py --epochs 100
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

### 2.4 パラメータスイープ

```bash
# 100 タスクを並列 10 で実行
cjob sweep -n 100 --parallel 10 -- python main.py --trial _INDEX_

# 時間制限付き
cjob sweep -n 50 --parallel 5 --time-limit 6h -- bash run.sh
```

各タスクは `_INDEX_` プレースホルダーで識別される（0-origin、0 〜 completions-1）。`_INDEX_` は Job Pod 実行時に実際のインデックス値（`$CJOB_INDEX`）に置換される。

### 2.5 ジョブ一覧表示

```bash
cjob list
```

### 2.6 状態確認

```bash
cjob status <job-id>
```

### 2.7 キャンセル

```bash
cjob cancel <job-id>
```

### 2.8 ログ取得

```bash
# 完了後に確認
cjob logs <job-id>

# リアルタイム追跡
cjob logs --follow <job-id>
```

### 2.9 完了済みジョブの削除

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

## 3. `cjob sweep` の動作

1. `cjob add` と同様に `pwd`、export 済み環境変数、`CJOB_IMAGE` / `JUPYTER_IMAGE` を収集する（両方未設定の場合はエラー終了）
2. `--` 以降の argv を shell-safe に連結して command を生成する
3. `-n` を `completions`、`--parallel` を `parallelism` として `POST /v1/sweep` に送信する
4. `job_id` とタスク数・並列数を表示する

### 引数

| 引数 | 必須 | 説明 |
|---|---|---|
| `-n <count>` | 必須 | タスク総数（completions）。上限はサーバ側 `MAX_SWEEP_COMPLETIONS`（デフォルト 1000） |
| `--parallel <n>` | 任意 | 同時実行数（parallelism）。デフォルト 1 |
| `--time-limit <duration>` | 任意 | sweep **全体**の実行時間上限。省略時はサーバ側デフォルト |
| `--cpu <cpu>` | 任意 | CPU リソース。デフォルト "1" |
| `--memory <memory>` | 任意 | メモリリソース。デフォルト "1Gi" |
| `--gpu <N>` | 任意 | GPU 数。デフォルト 0（GPU なし） |
| `--flavor <name>` | 任意 | ResourceFlavor 名（例: "cpu", "gpu-a100"）。省略時はサーバ側デフォルト |
| `-- <command>` | 必須 | 各タスクで実行するコマンド |

### `_INDEX_` プレースホルダー

コマンド中の `_INDEX_` は CLI が Submit API に送信する前に `$CJOB_INDEX` シェル変数に置換される。Job Pod 実行時に `CJOB_INDEX` 環境変数（= K8s の `JOB_COMPLETION_INDEX`）が展開され、各タスク固有のインデックス値となる。

- 0-origin（K8s の `JOB_COMPLETION_INDEX` と同一）
- 値の範囲: `0` 〜 `completions - 1`

スクリプトファイル内では `$CJOB_INDEX` 環境変数を直接参照できる。スクリプトファイルの中身はユーザーのシェルによる展開を受けないため、`_INDEX_` プレースホルダーを使わずに `$CJOB_INDEX` をそのまま記述できる。

```bash
# run.sh
echo "index is $CJOB_INDEX"
python main.py --trial $CJOB_INDEX
```

```bash
cjob sweep -n 10 --parallel 5 -- bash run.sh
```

## 4. `cjob add` の動作

1. `pwd` を取得する
2. export 済み環境変数を収集する（`PATH` / `VIRTUAL_ENV` を含む）
3. 環境変数 `CJOB_IMAGE` からコンテナイメージ名を取得する（未設定時は `JUPYTER_IMAGE` にフォールバック。両方未設定の場合はエラー終了する）
4. `--` 以降の argv を shell-safe に連結して command を生成する
5. `--time-limit` が指定されていれば秒数に変換する（省略時は API のデフォルト値を使用）
6. ServiceAccount JWT と namespace を固定パスから読み取る
7. API にジョブ投入を行う（`image`, `time_limit_seconds` フィールドを含む）
8. `job_id` を表示する

### `--time-limit` オプション

実行時間の上限を指定する。省略時はサーバ側のデフォルト（24時間）が適用される。

```bash
cjob add --time-limit 3600 -- python main.py    # 秒数で指定
cjob add --time-limit 1h -- python main.py       # 1時間
cjob add --time-limit 6h -- python main.py       # 6時間
cjob add --time-limit 1d -- python main.py       # 1日
cjob add --time-limit 3d -- python main.py       # 3日
```

受け付ける表記: 整数（秒）、`<数値>s`（秒）、`<数値>m`（分）、`<数値>h`（時間）、`<数値>d`（日）。最大値はサーバー側の `MAX_TIME_LIMIT_SECONDS`（デフォルト 604800 = 7 日）で制限される。

## 5. `cjob logs` の動作

`cjob logs` はログの閲覧に特化する。ログの削除は `cjob delete` または `cjob reset` が担う。

ジョブ状態によって以下のように動作する。

| 状態 | 動作 |
|---|---|
| QUEUED / DISPATCHING / DISPATCHED | ログファイル未生成のため最大 5分待機（待機中は状態と経過時間を表示） |
| HELD | 保留中のためログなし。「ジョブは保留中です」と表示し、`cjob release` で解除を促す |
| RUNNING | ファイル生成後に tail -f で追跡（`--follow` 時） |
| SUCCEEDED / FAILED | ファイルを全量表示して終了 |
| CANCELLED | ファイルがあれば表示、なければ "No logs available" |
| DELETING | reset 処理中。ファイルがあれば表示、なければ "No logs available（reset 処理中）" を表示して終了 |

ログファイルは PVC 上にあり、CLI が直接読む。ログディレクトリのパスは `GET /v1/jobs/{job_id}` で取得した `log_dir` を使用する。

### QUEUED / DISPATCHING / DISPATCHED 中の待機フィードバック

待機中は `GET /v1/jobs/{job_id}` を数秒ごとにポーリングし、状態と経過時間を表示する。5分経過してもジョブが開始しない場合はタイムアウトメッセージを表示して終了する。

```
$ cjob logs --follow 3
ジョブ 3 の開始を待機中... (QUEUED) [0:00:12]
ジョブ 3 の開始を待機中... (DISPATCHING) [0:00:25]
ジョブ 3 の開始を待機中... (DISPATCHED) [0:00:48]
ジョブ 3 が開始しました。ログを追跡します。
<ログ出力>
```

```
$ cjob logs --follow 3   # 5分経過しても開始しない場合
ジョブ 3 の開始を待機中... (DISPATCHED) [5:00:00]
タイムアウトしました。ジョブはまだ DISPATCHED 状態です。
`cjob status 3` で状態を確認してください。
```

### `--follow` の終了条件

`--follow` モードは Ctrl-C によりユーザーが明示的に終了する。ジョブが `SUCCEEDED` / `FAILED` / `CANCELLED` に遷移しても自動終了しない。

ただし `--follow` 指定なし（通常の `cjob logs`）でジョブがすでに終了状態の場合は、ファイルを全量表示して終了する。

```
$ cjob logs --follow 3
<ログ出力中>
^C      ← ユーザーが Ctrl-C で終了
```

## 6. `cjob list` の動作

`GET /v1/jobs` を呼び出し、結果を表形式で表示する。デフォルトでは最新50件を JOB_ID 昇順で表示する。

```
$ cjob list
JOB_ID  TYPE   STATUS      PROGRESS    COMMAND                                  CREATED              FINISHED
51      job    SUCCEEDED   -           python main.py --alpha 0.1 --beta 16     2026-03-23 12:34     2026-03-23 12:37
52      job    RUNNING     -           python main.py --alpha 0.2 --beta 16     2026-03-23 12:35     -
53      sweep  RUNNING     48/2/100    python main.py --trial $CJOB_INDEX       2026-03-23 12:35     -
54      sweep  SUCCEEDED   98/2/100    python main.py --trial $CJOB_INDEX       2026-03-23 12:36     2026-03-23 13:00
（100件中最新の50件を表示。全件表示するには --all を使用してください）
```

TYPE 列は通常ジョブが `job`、sweep ジョブが `sweep`。PROGRESS 列は sweep ジョブの場合に `成功数/失敗数/全体数` を表示し、通常ジョブは `-` を表示する。

オプション：

- `--status <status>`：指定したステータスのジョブのみ表示（例: `--status RUNNING`）
- `--time-limit <range>`：time_limit_seconds の範囲でフィルターする。`<min>:<max>` 形式で指定する。`<min>` は以上、`<max>` は未満。片方を省略可能（例: `6h:12h`, `:12h`, `6h:`）。duration の書式は `cjob add --time-limit` と同じ（整数秒、`<数値>s/m/h/d`）。CLI で秒数に変換し、API の `time_limit_ge` / `time_limit_lt` パラメータとして送信する
- `--format ids`：ジョブ ID をコンマ区切りで出力する（例: `1,3,5,8`）。テーブル表示の代わりに ID のみを出力し、他のサブコマンドへの入力として使用できる。該当ジョブがない場合は何も出力しない
- `--limit <n>`：表示件数を最新 n 件に制限する（1 以上）。省略時はデフォルト50件。API の `limit` パラメータに値を送る
- `--all`：全件表示する。API の `limit` パラメータを省略する（API は `limit` 省略時に全件を返す）
- `--reverse`：JOB_ID の降順で表示する

```bash
cjob list                                    # 最新 50 件を昇順で表示
cjob list --all                              # 全件を昇順で表示
cjob list --reverse                          # 最新 50 件を降順で表示
cjob list --status RUNNING                   # RUNNING の最新 50 件を表示
cjob list --limit 10                         # 最新 10 件のみ表示
cjob list --status QUEUED --time-limit 6h:   # QUEUED で time_limit が 6 時間以上のジョブ
cjob list --time-limit :12h                  # time_limit が 12 時間未満のジョブ
cjob list --time-limit 6h:12h               # time_limit が 6 時間以上 12 時間未満のジョブ
cjob list --status QUEUED --format ids       # QUEUED ジョブの ID をコンマ区切りで出力

# 6 時間以上かかるキュー待ちジョブを一括保留にする
cjob hold $(cjob list --status QUEUED --time-limit 6h: --format ids)
```

表示件数がジョブ総数より少ない場合は、省略されていることを示すメッセージを標準エラー出力に表示する。`--format ids` 指定時は省略メッセージを表示しない。

command は長い場合に末尾を省略して表示する（例: 40文字で切り捨て）。

## 7. `cjob status` の動作

`GET /v1/jobs/{job_id}` を呼び出し、主要フィールドを整形して表示する。

```
$ cjob status 2
job_id:       2
type:         job
status:       RUNNING
command:      python main.py --alpha 0.2 --beta 16
cwd:          /home/jovyan/project-a/exp1
flavor:       cpu
cpu:          2
memory:       4Gi
gpu:          0
time_limit:   24h (残り 23h 24m)
created_at:   2026-03-23 12:35:00
dispatched_at: 2026-03-23 12:35:05
started_at:   2026-03-23 12:35:10
finished_at:  -
k8s_job_name: cjob-alice-2
node_name:    worker07
log_dir:      /home/jovyan/.cjob/logs/2
```

`time_limit` は `time_limit_seconds` を人間が読みやすい形式で表示する。ジョブが RUNNING の場合は残り時間も併記する。

sweep ジョブの場合は追加フィールドを表示する。

```
$ cjob status 3
job_id:         3
type:           sweep
status:         RUNNING
command:        python main.py --trial $CJOB_INDEX
cwd:            /home/jovyan/project-a
flavor:         cpu
cpu:            2
memory:         4Gi
gpu:            0
completions:    100
parallelism:    10
progress:       48/2/100 (succeeded/failed/total)
failed_indexes: 12,37
time_limit:     6h (残り 4h 32m)
created_at:     2026-03-23 12:35:00
dispatched_at:  2026-03-23 12:35:05
started_at:     2026-03-23 12:35:10
finished_at:    -
k8s_job_name:   cjob-alice-3
node_name:      worker07,worker08
log_dir:        /home/jovyan/.cjob/logs/3
```

`node_name` はジョブが実行されたノード名。通常ジョブでは単一のノード名を表示し、sweep ジョブでは実行に使用された全ノード名をカンマ区切りで表示する（Watcher が RUNNING 遷移時および sweep の進行状況変化時に累積記録する。詳細は [watcher.md](watcher.md) §4.3.1 参照）。

`last_error` はジョブが FAILED の場合にエラー理由を表示する。値が `null` の場合は行自体を表示しない。

```
$ cjob status 5
job_id:        5
type:          job
status:        FAILED
command:       echo hello
cwd:           /home/jovyan
flavor:        cpu
cpu:           1
memory:        1Gi
gpu:           0
time_limit:    1m
created_at:    2026-03-23 13:00:00
dispatched_at: -
started_at:    -
finished_at:   2026-03-23 13:00:01
k8s_job_name:  -
node_name:     -
log_dir:       /home/jovyan/.cjob/logs/5
last_error:    K8s API permanent error 403: admission webhook "validate-image.kyverno.io" denied the request
```

存在しない job_id を指定した場合はエラーメッセージを表示して終了する。

```
$ cjob status 999
エラー: job_id 999 が見つかりません。
```

### sweep ジョブのログ

sweep ジョブは `cjob logs <job_id>` で全タスクのログをインデックス昇順で連結表示する。各タスクの境界にヘッダー行を挿入する。

```
$ cjob logs 3
=== [index 0] ===
Training with alpha=0.1 ...
Done.
=== [index 1] ===
Training with alpha=0.2 ...
Done.
```

`--index <n>` で特定インデックスのタスクのログのみ表示する。

```
$ cjob logs 3 --index 2
Training with alpha=0.5 ...
Error: convergence failed
```

`--follow` は `--index` と組み合わせて使用する。`--follow` のみ（`--index` なし）の場合はエラーとし、`--index` の指定を促す。

ログディレクトリ構造:
- 通常ジョブ: `/home/jovyan/.cjob/logs/<job_id>/`
- sweep ジョブ: `/home/jovyan/.cjob/logs/<job_id>/<index>/`

## 8. CLI の設定

### 8.1 API エンドポイント

Submit API のエンドポイントは環境変数 `CJOB_API_URL` から読む。未設定時はデフォルト値を使用する。

```
# ※ CLI の実装は Rust（reqwest クレート等）で行う。以下は概念説明のための擬似コードである。

SUBMIT_API_URL = env("CJOB_API_URL")
              or "http://submit-api.cjob-system.svc.cluster.local:8080"
```

ログディレクトリのパスは CLI 側で保持せず、API から取得する。個別ジョブの `log_dir` は `GET /v1/jobs/{job_id}` から、ログベースディレクトリは `GET /v1/jobs` の `log_base_dir` から取得する。これにより CLI 側の設定とサーバー側の ConfigMap（`LOG_BASE_DIR`）の不整合を防ぐ。

### 8.2 ユーザー設定ファイル

ユーザー固有の設定は TOML 形式のファイルで管理する。`cjob config` サブコマンドで操作する。

#### 設定ファイルのパス

`$XDG_CONFIG_HOME/cjob/config.toml` に保存する。`XDG_CONFIG_HOME` が未設定の場合は `~/.config/cjob/config.toml` をデフォルトとする。

#### TOML スキーマ

```toml
[env]
exclude = ["SECRET_TOKEN", "JUPYTER_TOKEN"]
```

| テーブル | キー | 型 | 説明 |
|---|---|---|---|
| `env` | `exclude` | リスト | ジョブ投入時に除外する環境変数名のリスト |

設定ファイルが存在しない場合は全項目がデフォルト値（空）として扱われる。

#### `cjob config` サブコマンド

`cjob config` は認証不要のローカル操作である。

##### `cjob config list`

全設定を TOML 形式で表示する。設定ファイルが存在しない場合はデフォルト値を表示する。

```
$ cjob config list
[env]
exclude = [
    "SECRET_TOKEN",
    "JUPYTER_TOKEN",
]
```

##### `cjob config add <table> <key> <value>`

リスト型の設定に要素を追加する。既に存在する値を追加した場合は何もしない（重複なし）。

```bash
cjob config add env exclude MY_SECRET
```

##### `cjob config remove <table> <key> <value>`

リスト型の設定から要素を削除する。

```bash
cjob config remove env exclude MY_SECRET
```

##### `cjob config set <table> <key> <value>`

スカラー型の設定値を変更する。リスト型のキーに対して使用するとエラーになる。

> **【実装状況】未実装（将来対応予定）**。現状ではスカラー型の設定キーが存在しないため、本サブコマンドは未実装である。

##### `cjob config unset <table> <key>`

スカラー型の設定値を削除（デフォルトに戻す）する。リスト型のキーに対して使用するとエラーになる。

> **【実装状況】未実装（将来対応予定）**。`cjob config set` と同じ理由で未実装である。

##### バリデーション

未知のテーブル/キーの組み合わせはエラーとする。型に合わないサブコマンド（リスト型に `set`/`unset`、スカラー型に `add`/`remove`）もエラーとし、正しいコマンドを案内する。

```
$ cjob config set env exclude X
エラー: env.exclude はリスト型です。add / remove を使用してください

$ cjob config add unknown key value
エラー: 不明な設定: unknown.key
```

#### 環境変数の除外

`cjob add` / `cjob sweep` はジョブ投入前に設定ファイルを読み込み、`env.exclude` に含まれる環境変数を送信対象から除外する。設定ファイルが存在しない場合は従来どおり全環境変数を送信する。

## 9. `cjob cancel` の動作

job_id の指定形式をパースして job_id のリストに展開し、`POST /v1/jobs/cancel` を呼ぶ。

**sweep ジョブのキャンセル:** sweep ジョブをキャンセルすると、K8s Indexed Job 全体が削除され、進行中の全タスクが即座に中断される。部分的なキャンセル（特定インデックスのみ）はできない。

```
# ※ CLI の実装は Rust で行う。以下は概念説明のための擬似コードである。

fn parse_job_ids(expr) -> Vec<u32>:
    // "1-5,8,10-12" → [1, 2, 3, 4, 5, 8, 10, 11, 12]
    expr を ',' で分割して各パートを処理する
        '-' を含む場合: start..=end の連番を追加
        それ以外: その数値を追加
    重複除去して昇順ソートして返す

fn cmd_cancel(expr):
    job_ids = parse_job_ids(expr)
    if len(job_ids) == 1:
        POST /v1/jobs/{job_id}/cancel を呼ぶ
        "ジョブ {job_id}: {status}" を表示する
    else:
        POST /v1/jobs/cancel に job_ids を送る
        result を受け取り:
            cancelled があれば "キャンセルしました" を表示する
            skipped があれば "スキップしました（完了済みまたはキャンセル済み）" を表示する
            not_found があれば "見つかりませんでした" を表示する
```

## 10. `cjob delete` の動作

`--all` フラグがある場合は job_ids を省略して `POST /v1/jobs/delete` を呼ぶ。
それ以外は job_id の指定形式をパースして job_id のリストに展開してから呼ぶ。

```
# ※ CLI の実装は Rust で行う。以下は概念説明のための擬似コードである。

fn cmd_delete(expr, all: bool):
    if all:
        POST /v1/jobs/delete に空のリクエストを送る
    else:
        job_ids = parse_job_ids(expr)   // cancel と同じパース処理を共用
        POST /v1/jobs/delete に job_ids を送る

    result を受け取り:
        result.log_dirs の各パスに対応するログディレクトリを削除する
        deleted があれば "削除しました" を表示する
        skipped があれば:
            reason が "running" のジョブ → "実行中のため削除できませんでした。先に cjob cancel を実行してください"
            reason が "held" のジョブ → "保留中のため削除できませんでした。先に cjob cancel または cjob release を実行してください"
            reason が "deleting" のジョブ → "リセット処理中のため削除できませんでした"
            （API レスポンスの skipped[].reason に基づいて分岐する）
        not_found があれば "見つかりませんでした" を表示する
```

## 11. `cjob hold` の動作

QUEUED 状態のジョブを保留にし、Dispatcher による実行を停止する。

`--all` フラグがある場合は job_ids を省略して `POST /v1/jobs/hold` を呼ぶ（namespace 内の全 QUEUED ジョブを保留対象とする）。
それ以外は job_id の指定形式をパースして job_id のリストに展開してから呼ぶ。

```
# ※ CLI の実装は Rust で行う。以下は概念説明のための擬似コードである。

fn cmd_hold(expr, all: bool):
    if all:
        POST /v1/jobs/hold に空のリクエストを送る
    else:
        job_ids = parse_job_ids(expr)   // cancel と同じパース処理を共用
        POST /v1/jobs/hold に job_ids を送る

    result を受け取り:
        held があれば "保留しました" を表示する
        skipped があれば "スキップしました（QUEUED 以外）" を表示する
        not_found があれば "見つかりませんでした" を表示する
```

### 使用例

```bash
# 単体指定
cjob hold 5

# 範囲指定・複数指定
cjob hold 1-10
cjob hold 1,3,5
cjob hold 1-5,8,10-12

# QUEUED 状態のジョブを全て保留
cjob hold --all
```

## 12. `cjob release` の動作

保留中（HELD）のジョブをキューに戻し、Dispatcher による実行を再開する。

`--all` フラグがある場合は job_ids を省略して `POST /v1/jobs/release` を呼ぶ（namespace 内の全 HELD ジョブを解除対象とする）。
それ以外は job_id の指定形式をパースして job_id のリストに展開してから呼ぶ。

```
# ※ CLI の実装は Rust で行う。以下は概念説明のための擬似コードである。

fn cmd_release(expr, all: bool):
    if all:
        POST /v1/jobs/release に空のリクエストを送る
    else:
        job_ids = parse_job_ids(expr)   // cancel と同じパース処理を共用
        POST /v1/jobs/release に job_ids を送る

    result を受け取り:
        released があれば "キューに戻しました" を表示する
        skipped があれば "スキップしました（HELD 以外）" を表示する
        not_found があれば "見つかりませんでした" を表示する
```

### 使用例

```bash
# 単体指定
cjob release 5

# 範囲指定・複数指定
cjob release 1-10
cjob release 1,3,5

# HELD 状態のジョブを全て解除
cjob release --all
```

## 13. `cjob reset` の動作

1. `GET /v1/jobs` でジョブ一覧を取得し、レスポンスの `log_base_dir` を保持した上で以下の順で確認する
   - `DELETING` のジョブが1件でも存在する場合は「前回のリセット処理がまだ完了していません。しばらく待ってから再試行してください。」を表示して中止する
   - `QUEUED` / `DISPATCHING` / `DISPATCHED` / `RUNNING` / `HELD` のジョブが1件でも存在する場合は job_id を表示して中止する
2. 全ジョブが完了済みの場合はユーザーに確認プロンプトを表示する
3. y の場合のみ以下を順に実行する
   1. `log_base_dir` で取得したパスのログディレクトリを削除する（API 呼び出し前に削除することで、API 呼び出し後に CLI がクラッシュしても Watcher が counter をリセットした後の job_id=1 再利用時に log_dir が存在しない状態を保証する）
   2. `POST /v1/reset` を呼び出す（202 Accepted が返る）
4. リセット開始メッセージを表示して終了する（完了を待たない）

実際の K8s Job 削除・DB クリーンアップ・カウンターリセットは Watcher が非同期で処理する。
リセット完了前に `cjob add` を実行すると、Submit API は `DELETING` ジョブが存在するとして 409 を返し投入を拒否する。

**注意:** ステップ 1 の事前チェックとステップ 3-2 の `POST /v1/reset` の間にレースコンディションが存在する。ログ削除後に `POST /v1/reset` が 409 を返した場合（事前チェック後に別のクライアントが操作した等）、ログが消えたのにリセットが実行されない状態になりうる。CLI は単一ユーザーが使用する前提のため発生は極めて稀であり、発生した場合もジョブの DB レコードは保持されるため、次回の `cjob reset` で正常にリセットできる。

```
$ cjob reset
完了していないジョブがあるためリセットできません。
完了待ちのジョブ: 3, 7, 12

$ cjob reset   # 全ジョブ完了後
全 15 件のジョブとログを削除します。よろしいですか？ [y/N] y
リセットを開始しました。バックグラウンドでクリーンアップが完了するまでお待ちください。
```

## 14. `cjob usage` の動作

`GET /v1/usage` を呼び出し、直近 `FAIR_SHARE_WINDOW_DAYS` 日分の日別リソース使用状況を表示する。

表示単位は人間が読みやすいように変換する。

- CPU: ミリコア秒 → core·h（`/ 1000 / 3600`）
- メモリ: MiB 秒 → GiB·h（`/ 1024 / 3600`）
- GPU: 秒 → h（`/ 3600`）

GPU 列はクラスタ全体で GPU 使用実績がない場合（`total_gpu_seconds == 0`）は非表示とする。

```
$ cjob usage

Resource Usage (past 7 days)
──────────────────────────────────────────────────
  Date              CPU (core·h)    Mem (GiB·h)
  2026-03-23               24.0           48.0
  2026-03-24               12.5           25.0
  2026-03-25                8.0           16.0
  ────────────────────────────────────────────────
  Total                    44.5           89.0
```

使用実績がない場合は「使用実績がありません。」を表示する。

### Resource Quota の表示

レスポンスの `resource_quota` が `null` でない場合、使用状況テーブルの前に Resource Quota セクションをテーブル形式で表示する。

各列の意味:
- **Resource**: リソース種別（CPU / Memory / GPU）
- **Used**: 現在の使用量
- **Hard**: クォータ上限
- **Remaining**: 残り（`hard - used`）
- **Use%**: 使用率（`used / hard * 100`）、小数点以下1桁

単位変換:
- CPU: ミリコア → コア数、小数点以下1桁（例: `280.0`）
- メモリ: MiB → GiB、整数（例: `800Gi`）
- GPU: 個数のまま（例: `1`）

GPU 行は `hard_gpu == 0` の場合は非表示とする。

```
$ cjob usage

Resource Quota
──────────────────────────────────────────────────
  Resource       Used       Hard  Remaining    Use%
  CPU           280.0      300.0       20.0   93.3%
  Memory        800Gi     1250Gi      450Gi   64.0%
  GPU               1          4          3   25.0%

Resource Usage (past 7 days)
──────────────────────────────────────────────────
  Date              CPU (core·h)    Mem (GiB·h)
  2026-03-23               24.0           48.0
  2026-03-24               12.5           25.0
  2026-03-25                8.0           16.0
  ────────────────────────────────────────────────
  Total                    44.5           89.0
```

## 15. `cjob update` の動作

CLI バイナリのバージョン管理と更新を行う。バイナリは Submit API 経由で配布される。

### オプション

| オプション | 説明 |
|---|---|
| `--pre` | プレリリース版（ベータ版等）を含める |
| `--yes` / `-y` | 確認プロンプトをスキップする |
| `--list` | 利用可能なバージョン一覧を表示する（`--version` と排他） |
| `--version <version>` | 指定バージョンをインストールする（`--list` と排他） |

### デフォルト動作（最新安定版への更新）

1. `GET /v1/cli/version` で安定版の最新バージョン（`latest` ファイルの内容）を取得する
2. ローカルの CLI バージョン（`--version` で表示されるもの）と比較する
3. 同一バージョンであれば「すでに最新です」と表示して終了する
4. 新しいバージョンがある場合:
   1. 確認プロンプトを表示する（`--yes` で省略可）
   2. `GET /v1/cli/download?version=<version>` でバイナリをダウンロードする
   3. 現在の実行ファイルを新しいバイナリで置き換える（一時ファイル + atomic rename）
   4. 置き換え後にファイルに実行権限（`0o755`）を付与する
   5. 更新完了メッセージを表示する

### `--pre` 指定時

`GET /v1/cli/versions` で全バージョン一覧を取得し、プレリリースを含む最新バージョンを更新対象とする。

### `--list` 指定時

`GET /v1/cli/versions` で全バージョン一覧を取得し、一覧表示する。デフォルトでは安定版のみ、`--pre` 指定でプレリリース版も含む。現在インストール中のバージョンには `(current)` マーカー、latest バージョンには `(latest)` マーカーを付与する。

### `--version <version>` 指定時

指定バージョンを直接インストールする。確認プロンプト後、`GET /v1/cli/download?version=<version>` でダウンロードしてバイナリを置き換える。

### 使用例

```bash
# 安定版の最新に更新（デフォルト）
$ cjob update
更新しますか？ 1.2.0 → 1.3.0 [y/N] y
更新が完了しました。(1.3.0)

# ベータ版を含む最新に更新
$ cjob update --pre
更新しますか？ 1.2.0 → 1.3.1-beta.2 [y/N] y
更新が完了しました。(1.3.1-beta.2)

# 確認をスキップ
$ cjob update -y
更新が完了しました。(1.3.0)

# すでに最新の場合
$ cjob update
すでに最新バージョンです (1.3.0)

# 利用可能なバージョン一覧（安定版のみ）
$ cjob update --list
1.3.0 (latest)
1.2.0 (current)
1.1.0

# ベータ版を含む一覧
$ cjob update --list --pre
1.3.1-beta.2
1.3.1-beta.1
1.3.0 (latest)
1.2.0 (current)
1.1.0

# バージョン指定でインストール
$ cjob update --version 1.3.1-beta.1
更新しますか？ 1.2.0 → 1.3.1-beta.1 [y/N] y
更新が完了しました。(1.3.1-beta.1)
```

## 16. `cjob flavor` の動作

`GET /v1/flavors` を呼び出し、利用可能な ResourceFlavor の一覧とリソース上限を表示する。認証不要のエンドポイントを使用するため、ServiceAccount JWT がなくても実行できる。

### `cjob flavor list`

利用可能な flavor の一覧を表示する。デフォルト flavor は `*` でマークする。

```
$ cjob flavor list
NAME             GPU    NODES    DEFAULT
cpu              -      2          *
gpu-a100         yes    1
```

### `cjob flavor info <name>`

指定した flavor のリソース上限とタスクあたりの上限を表示する。

QUOTA は ClusterQueue の nominalQuota（flavor 全体で共有するリソース総量）。TASK LIMIT はタスクあたりのリソース上限で、`min(max_node_allocatable, nominalQuota)` で計算される。GPU 非対応 flavor では GPU 行を省略する。

```
$ cjob flavor info cpu
name:   cpu
GPU:    非対応

RESOURCE      QUOTA    TASK LIMIT
CPU             256           128
Memory       1000Gi       503.4Gi
```

GPU 対応 flavor の場合は GPU 行も表示する。

```
$ cjob flavor info gpu-a100
name:   gpu-a100
GPU:    対応

RESOURCE      QUOTA    TASK LIMIT
CPU              64            64
Memory        500Gi         500Gi
GPU               4             4
```

Watcher 未同期で quota 情報がない場合はメッセージを表示する。

```
$ cjob flavor info cpu
name:   cpu
GPU:    非対応

（リソース情報がまだ取得されていません）
```

存在しない flavor を指定した場合はエラーを表示する。

```
$ cjob flavor info xxx
Error: flavor 'xxx' は存在しません。利用可能な flavor: cpu, gpu-a100
```

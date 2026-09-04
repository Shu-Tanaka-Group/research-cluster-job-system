# CLI 設計

## 1. 基本コマンド

```bash
cjob add [--cpu <cpu>] [--memory <memory>] [--flavor <name>] [--gpu <N>] [--time-limit <duration>] [--image <image>] -- <command...>
cjob sweep -n <count> --parallel <n> [--flavor <name>] [--gpu <N>] [--time-limit <duration>] [--image <image>] -- <command...>
cjob list [--status <status>] [--flavor <name>] [--time-limit <range>] [--format ids] [--limit <n>] [--all] [--reverse]
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
cjob set <job-ids> [--cpu <cpu>] [--memory <memory>] [--flavor <name>] [--gpu <N>] [--time-limit <duration>] [--image <image>]
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

### 2.8 パラメータ変更

QUEUED / HELD ジョブのパラメータを変更する。指定したオプションのみ更新し、未指定の項目は現在値を維持する。

```bash
# flavor の変更
cjob set <job-ids> --flavor cpu-sub

# リソースの変更
cjob set <job-ids> --cpu 4 --memory 16Gi

# time-limit の変更
cjob set <job-ids> --time-limit 12h

# 複数パラメータを同時に変更
cjob set <job-ids> --flavor cpu-sub --cpu 4 --memory 16Gi --time-limit 12h

# カンマ区切り・範囲指定
cjob set 10,11,12 --flavor cpu-sub
cjob set 10-20,25,30 --cpu 8

# cjob list --format ids との連携
cjob set $(cjob list --status QUEUED --flavor cpu --format ids) --flavor cpu-sub
```

変更可能なステータスは QUEUED / HELD のみ。DISPATCHING 以降のジョブは K8s Job 作成済みのため変更不可（スキップ）。
オプションを1つも指定しなかった場合はエラーとなる。
バリデーションルールは `cjob add` と同じ（flavor 存在チェック、リソース上限、GPU 互換性、time-limit 範囲）。

### 2.9 ログ取得

```bash
# 完了後に確認
cjob logs <job-id>

# リアルタイム追跡
cjob logs --follow <job-id>
```

### 2.10 完了済みジョブの削除

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

1. `cjob add` と同様に `pwd`、export 済み環境変数、投入 Pod のイメージ名（`CJOB_IMAGE` → `JUPYTER_IMAGE`）を収集する（両方未設定でもエラーにはしない。§4「image の決まり方」参照）
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
| `--image <image>` | 任意 | Job Pod のコンテナイメージ。省略時は flavor 既定イメージまたは投入 Pod のイメージ（§4「image の決まり方」参照） |
| `-- <command>` | 必須 | 各タスクで実行するコマンド |

### `_INDEX_` プレースホルダー

コマンド中の `_INDEX_` は CLI が Submit API に送信する前に `$CJOB_INDEX` シェル変数に置換される。Job Pod 実行時に `CJOB_INDEX` 環境変数（= K8s の `JOB_COMPLETION_INDEX`）が展開され、各タスク固有のインデックス値となる。

- 0-origin（K8s の `JOB_COMPLETION_INDEX` と同一）
- 値の範囲: `0` 〜 `completions - 1`

ジョブとして Pod 内で実行されるスクリプトファイル内では `$CJOB_INDEX` 環境変数を直接参照できる。スクリプトファイルの中身はユーザーシェルによる展開を受けないため、`_INDEX_` プレースホルダーを使わずに `$CJOB_INDEX` をそのまま記述できる。

```bash
# run.sh（Pod の中で実行される）
echo "index is $CJOB_INDEX"
python main.py --trial $CJOB_INDEX
```

```bash
cjob sweep -n 10 --parallel 5 -- bash run.sh
```

**注意: `cjob sweep` を呼び出すラッパースクリプトの場合**

上記の「スクリプトファイル」は Job Pod 内で実行される側のスクリプト（`run.sh`）を指す。これに対して `cjob sweep` コマンド自体をシェルスクリプトに書いて `bash jobscript.sh` のようにユーザーシェルで実行する場合、スクリプトの中身はユーザーシェルの変数展開を受ける。このとき `$CJOB_INDEX` は未定義のため空文字列に展開され、`cjob sweep` に渡る引数が欠落する。ラッパースクリプトでは必ず `_INDEX_` を使うこと（または `'$CJOB_INDEX'` のようにシングルクォートで展開を抑止する）。

```bash
# jobscript.sh - 手元の bash で実行するラッパースクリプト
NUM_SEED=50
cjob sweep -n ${NUM_SEED} -- python train.py --seed _INDEX_       # OK

# NG: ${CJOB_INDEX} はユーザーシェルで展開され空文字列になる
# cjob sweep -n ${NUM_SEED} -- python train.py --seed ${CJOB_INDEX}
```

## 4. `cjob add` の動作

### 引数

| 引数 | 必須 | 説明 |
|---|---|---|
| `--cpu <cpu>` | 任意 | CPU リソース。デフォルト "1" |
| `--memory <memory>` | 任意 | メモリリソース。デフォルト "1Gi" |
| `--gpu <N>` | 任意 | GPU 数。デフォルト 0（GPU なし） |
| `--flavor <name>` | 任意 | ResourceFlavor 名（例: "cpu", "gpu-a100"）。省略時はサーバ側デフォルト |
| `--time-limit <duration>` | 任意 | 実行時間上限。省略時はサーバ側デフォルト |
| `--image <image>` | 任意 | Job Pod のコンテナイメージ。省略時は flavor 既定イメージまたは投入 Pod のイメージ（「image の決まり方」参照） |
| `-- <command>` | 必須 | 実行するコマンド |

### 動作

1. `pwd` を取得する
2. export 済み環境変数を収集する（`PATH` / `VIRTUAL_ENV` を含む）
3. 投入 Pod のイメージ名を環境変数 `CJOB_IMAGE` から取得する（未設定時は `JUPYTER_IMAGE` にフォールバック。両方未設定でもエラーにはせず、`fallback_image` を送らない）
4. `--` 以降の argv を shell-safe に連結して command を生成する
5. `--time-limit` が指定されていれば秒数に変換する（省略時は API のデフォルト値を使用）
6. ServiceAccount JWT と namespace を固定パスから読み取る
7. API にジョブ投入を行う（`--image` を指定した場合は `image`、手順 3 で取得できた場合は `fallback_image`、および `time_limit_seconds` フィールドを含む）
8. `job_id` を表示する

### image の決まり方

Job Pod のイメージは Submit API が次の優先順位で解決する（[api.md](api.md) §2.2）。CLI は「ユーザーの明示指定」と「投入 Pod のイメージ」を区別して送るだけで、解決自体は行わない。

```
--image  >  flavor の image  >  CJOB_IMAGE / JUPYTER_IMAGE
└ ユーザー明示 ┘  └ 管理者定義 ┘  └── 投入 Pod のイメージ ──┘
```

| CLI が送るフィールド | 送信元 |
|---|---|
| `image` | `--image` |
| `fallback_image` | `CJOB_IMAGE` → `JUPYTER_IMAGE` |

`CJOB_IMAGE` / `JUPYTER_IMAGE` が両方未設定でも CLI はエラー終了しない。flavor 既定イメージで解決できる場合があるためである。いずれからも解決できない場合は Submit API が 400 を返し、CLI はそのメッセージを表示する。

実際に使用されるイメージは `cjob status <job-id>` の `image` 行で確認できる（§7）。flavor ごとの既定イメージは `cjob flavor list` の IMAGE 列で確認できる（§17）。

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
| QUEUED / DISPATCHING / DISPATCHED | `--follow` なし: `Job {job_id} has not started yet. ({status})` と表示し `--follow` の使用を促して終了。`--follow` あり: 最大 5分待機（待機中は状態と経過時間を表示） |
| HELD | 保留中のためログなし。`Job {job_id} is held. (HELD)` と表示し、`cjob release` で解除を促す |
| RUNNING | ファイル生成後に tail -f で追跡（`--follow` 時） |
| SUCCEEDED / FAILED | ファイルを全量表示して終了 |
| CANCELLED | ファイルがあれば表示、なければ "No logs available" |
| DELETING | reset 処理中。ファイルがあれば表示、なければ `No logs available (reset in progress)` を表示して終了 |

ログファイルは PVC 上にあり、CLI が直接読む。ログディレクトリのパスは `GET /v1/jobs/{job_id}` で取得した `log_dir` を使用する。

### QUEUED / DISPATCHING / DISPATCHED 中の動作

`--follow` なしの場合、ジョブがまだ開始されていないことを通知し、`--follow` の使用を促して即座に終了する。

```
$ cjob logs 3
Job 3 has not started yet. (QUEUED)
Run `cjob logs --follow 3` to follow logs.
```

`--follow` ありの場合、`GET /v1/jobs/{job_id}` を数秒ごとにポーリングし、状態と経過時間を表示する。5分経過してもジョブが開始しない場合はタイムアウトメッセージを表示して終了する。

```
$ cjob logs --follow 3
Waiting for job 3 to start... (QUEUED) [0:00:12]
Waiting for job 3 to start... (DISPATCHING) [0:00:25]
Waiting for job 3 to start... (DISPATCHED) [0:00:48]
Job 3 has started. Following logs.
<log output>
```

```
$ cjob logs --follow 3   # 5分経過しても開始しない場合
Waiting for job 3 to start... (DISPATCHED) [5:00:00]
Timed out. Job is still in DISPATCHED state.
Run `cjob status 3` to check the status.
```

### `--follow` の終了条件

`--follow` モードは Ctrl-C によりユーザーが明示的に終了する。ジョブが `SUCCEEDED` / `FAILED` / `CANCELLED` に遷移しても自動終了しない。

ただし `--follow` 指定なし（通常の `cjob logs`）でジョブがすでに終了状態の場合は、ファイルを全量表示して終了する。

```
$ cjob logs --follow 3
<log output streaming>
^C      ← user interrupts with Ctrl-C
```

## 6. `cjob list` の動作

`GET /v1/jobs` を呼び出し、結果を表形式で表示する。デフォルトでは最新50件を JOB_ID 昇順で表示する。

```
$ cjob list
JOB_ID  TYPE   STATUS      FLAVOR      PROGRESS    COMMAND                              CREATED              FINISHED
51      job    SUCCEEDED   cpu         -           python main.py --alpha 0.1 --beta 16 2026-03-23 12:34     2026-03-23 12:37
52      job    RUNNING     cpu         -           python main.py --alpha 0.2 --beta 16 2026-03-23 12:35     -
53      sweep  RUNNING     gpu-a100    48/2/100    python main.py --trial $CJOB_INDEX   2026-03-23 12:35     -
54      sweep  SUCCEEDED   gpu-a100    98/2/100    python main.py --trial $CJOB_INDEX   2026-03-23 12:36     2026-03-23 13:00
(Showing 50 of 100 jobs. Use --all to show all.)
```

TYPE 列は通常ジョブが `job`、sweep ジョブが `sweep`。PROGRESS 列は sweep ジョブの場合に `成功数/失敗数/全体数` を表示し、通常ジョブは `-` を表示する。

オプション：

- `--status <status>`：指定したステータスのジョブのみ表示（例: `--status RUNNING`）
- `--flavor <name>`：指定した flavor のジョブのみ表示（例: `--flavor gpu-a100`）。API の `flavor` パラメータとして送信する
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
cjob list --flavor gpu-a100                   # gpu-a100 flavor のジョブのみ表示
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
image:        your-registry/cjob-jupyter:2.1.0
flavor:       cpu
cpu:          2
memory:       4Gi
gpu:          0
time_limit:   24h (23h 24m remaining)
created_at:   2026-03-23 12:35:00
dispatched_at: 2026-03-23 12:35:05
started_at:   2026-03-23 12:35:10
finished_at:  -
k8s_job_name: cjob-alice-2
node_name:    worker07
log_dir:      /home/jovyan/.cjob/logs/2
```

`image` は Submit API が投入時に解決した確定イメージ（`jobs.image`）。`--image` の明示指定・flavor 既定イメージ・投入 Pod のイメージのいずれで解決されたかによらず、実際に Job Pod で使われる値が表示される（§4「image の決まり方」参照）。

`time_limit` は `time_limit_seconds` を人間が読みやすい形式で表示する。ジョブが RUNNING の場合は残り時間も併記する。

sweep ジョブの場合は追加フィールドを表示する。

```
$ cjob status 3
job_id:         3
type:           sweep
status:         RUNNING
command:        python main.py --trial $CJOB_INDEX
cwd:            /home/jovyan/project-a
image:          your-registry/cjob-jupyter:2.1.0
flavor:         cpu
cpu:            2
memory:         4Gi
gpu:            0
completions:    100
parallelism:    10
progress:       48/2/100 (succeeded/failed/total)
failed_indexes: 12,37
time_limit:     6h (4h 32m remaining)
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

### 履歴情報（retry_count / retry_after / events）

`retry_count` は dispatcher の K8s API 一時障害 retry 回数、`retry_after` は次回 dispatch 解禁時刻である。両者とも初期値の場合（`retry_count == 0` かつ `retry_after` が `null`）は行自体を表示しない。どちらかが非初期値の場合は両行を表示し、`retry_after` が `null` のときは `-` と表示する。

`events` は直近の job_events を時系列昇順で最大 10 件表示する（[api.md](api.md) §4 参照）。events が 1 件もない場合はセクションごと省略する。表示可能な最大件数を超える古い events が存在する場合は、先頭に `... N earlier events` マーカーを出力する。

`retry_after` が未来の時刻になっている QUEUED ジョブは、直近イベントを見ることで理由を判別できる。`UNSCHEDULABLE` は「ノードに配置できないまま滞留したため Watcher が差し戻した」ことを意味し（[watcher.md](watcher.md) §3 ステップ 10 参照）、`retry_after` はそのバックオフの解除時刻である。`RETRY` / `DEFERRED` は数十秒で解消する一時的な差し戻しである。CLI 側の表示ロジックは event_type の文字列をそのまま出力するため、イベント種別の追加に伴う変更は不要である。

```
$ cjob status 7
job_id:        7
type:          job
status:        RUNNING
command:       python train.py
cwd:           /home/jovyan/project-b
flavor:        cpu
cpu:           2
memory:        4Gi
gpu:           0
time_limit:    6h (4h 12m remaining)
created_at:    2026-04-14 11:16:50
dispatched_at: 2026-04-14 11:20:25
started_at:    2026-04-14 11:20:30
finished_at:   -
k8s_job_name:  cjob-alice-7
node_name:     worker07
log_dir:       /home/jovyan/.cjob/logs/7
retry_count:   0
retry_after:   -
events:
  2026-04-14T11:17:00Z  DISPATCHED
  2026-04-14T11:20:15Z  DEFERRED
  2026-04-14T11:20:25Z  DISPATCHED
  2026-04-14T11:20:30Z  RUNNING
```

将来 `--events <N>` / `--events all` オプションで表示件数を変更できる余地を残している（現在は未実装）。`--json` 出力（未実装）は API レスポンスの `events` / `retry_count` / `retry_after` / `earlier_events_count` をそのまま返す想定である。

存在しない job_id を指定した場合はエラーメッセージを表示して終了する。

```
$ cjob status 999
Error: job_id 999 not found.
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
Error: env.exclude is a list setting. Use add / remove instead

$ cjob config add unknown key value
Error: unknown setting: unknown.key
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
        "Job {job_id}: {status}" を表示する
    else:
        POST /v1/jobs/cancel に job_ids を送る
        result を受け取り:
            cancelled があれば "Cancelled: [job_ids]" を表示する
            skipped があれば "Skipped (already completed or cancelled): [job_ids]" を表示する
            not_found があれば "Not found: [job_ids]" を表示する
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
        deleted があれば "Deleted: [job_ids]" を表示する
        skipped があれば:
            reason が "running" のジョブ → "Job {id}: cannot delete while running. Run cjob cancel first."
            reason が "held" のジョブ → "Job {id}: cannot delete while held. Run cjob cancel or cjob release first."
            reason が "deleting" のジョブ → "Job {id}: cannot delete during reset"
            （API レスポンスの skipped[].reason に基づいて分岐する）
        not_found があれば "Not found: [job_ids]" を表示する
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
        held があれば "Held: [job_ids]" を表示する
        skipped があれば "Skipped (not QUEUED): [job_ids]" を表示する
        not_found があれば "Not found: [job_ids]" を表示する
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
        released があれば "Released: [job_ids]" を表示する
        skipped があれば "Skipped (not HELD): [job_ids]" を表示する
        not_found があれば "Not found: [job_ids]" を表示する
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

## 13. `cjob set` の動作

QUEUED または HELD 状態のジョブについて、Dispatcher に渡すリソース要求・flavor・image・time limit を事後的に上書きする。1 つ以上のフィールドが指定されていない場合はエラーで終了する。

job_id の指定形式は `cjob cancel` と同じく単体・範囲・複数組み合わせに対応する。単体指定時は `POST /v1/jobs/{job_id}/set`、複数指定時は `POST /v1/jobs/set` を呼ぶ。

### 変更可能なフィールド

| オプション | 型 | 内容 |
|---|---|---|
| `--cpu <cpu>` | 文字列 | CPU 要求量（例: `4`, `2000m`） |
| `--memory <memory>` | 文字列 | メモリ要求量（例: `16Gi`, `16384Mi`） |
| `--gpu <N>` | 整数 | GPU 個数 |
| `--flavor <name>` | 文字列 | ResourceFlavor 名 |
| `--image <image>` | 文字列 | Job Pod のコンテナイメージ |
| `--time-limit <duration>` | 文字列 | 実行時間上限（例: `12h`, `30m`）。API には秒に換算して送る |

指定されなかったフィールドは元の値を保持する。値のパース（`parse_duration` など）は `cjob add` と同じユーティリティを共用する。

ただし image だけは例外で、`--flavor` の変更に伴って暗黙に変わりうる。詳細は「image の再解決」を参照。

### image の再解決

flavor を変更すると実行に適したイメージも変わるため、Submit API は次の規則で image を再解決する（[api.md](api.md) §11.1）。

| `--image` | `--flavor` | image の扱い |
|---|---|---|
| あり | あり / なし | 指定された値に更新する |
| なし | あり | 変更後 flavor に既定イメージがあればそれに更新し、なければ据え置く |
| なし | なし | 据え置く |

`cjob add --image` で明示指定したジョブの flavor を変更すると明示指定が失われる。維持したい場合は `--image` を同時に指定する。

image が変更された場合、CLI は単体指定・複数指定のいずれでも変更後のイメージを 1 行表示する。複数指定でも全ジョブに同じ `--flavor` / `--image` を適用するため、変更後のイメージは 1 種類に決まる。

### 対象ジョブの条件

API 側で以下の状態チェックを行う。

- `QUEUED` / `HELD` 以外の状態のジョブは `skipped` に分類される（RUNNING / DISPATCHING / DISPATCHED / SUCCEEDED / FAILED / CANCELLED / DELETING など、すでに K8s に引き渡されたまたは完了済みのジョブは変更不可）。
- 存在しない job_id は `not_found` に分類される。

### 動作

```
# ※ CLI の実装は Rust で行う。以下は概念説明のための擬似コードである。

fn cmd_set(expr, cpu, memory, gpu, flavor, image, time_limit):
    if すべてのパラメータが None:
        エラー終了: "specify at least one parameter to modify (--cpu, --memory, --gpu, --flavor, --image, --time-limit)"

    time_limit_seconds = time_limit を秒に変換（指定時のみ）

    job_ids = parse_job_ids(expr)   // cancel と同じパース処理を共用
    if len(job_ids) == 1:
        POST /v1/jobs/{job_id}/set にパラメータを送る
        "Job {job_id}: {status}" を表示する
        レスポンスの image が非 null なら "image: {image}" を表示する
    else:
        POST /v1/jobs/set に job_ids とパラメータを送る
        result を受け取り:
            modified があれば "Modified: [job_ids]" を表示する
            skipped があれば "Skipped (not QUEUED / HELD): [job_ids]" を表示する
            not_found があれば "Not found: [job_ids]" を表示する
            image が非 null なら "image: {image}" を表示する
```

image が変更されなかった場合、レスポンスの `image` は `null` となり表示は行われない。

```
$ cjob set 5 --flavor gpu
Job 5: QUEUED
image: your-registry/cjob-cuda:2.1.0

$ cjob set 10-20 --flavor gpu
Modified: [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
image: your-registry/cjob-cuda:2.1.0
```

### 使用例

```bash
# 単体指定で flavor のみ変更
cjob set 5 --flavor cpu-sub

# 複数指定でリソース要求と time limit をまとめて変更
cjob set 10,11,12 --cpu 4 --memory 16Gi --time-limit 12h

# 範囲 + 個別指定
cjob set 10-20,25,30 --cpu 8

# cjob list からの ID 注入（QUEUED のまま flavor を切り替える）
cjob set $(cjob list --status QUEUED --flavor cpu --format ids) --flavor cpu-sub

# flavor を変えつつイメージは明示指定したものに固定する
cjob set 5 --flavor gpu --image your-registry/cjob-cuda:2.1.0

# イメージだけ差し替える
cjob set 5 --image your-registry/cjob-cuda:2.2.0
```

## 14. `cjob reset` の動作

1. `GET /v1/jobs` でジョブ一覧を取得し、レスポンスの `log_base_dir` を保持した上で以下の順で確認する
   - `DELETING` のジョブが1件でも存在する場合は `"Previous reset is still in progress. Please wait and try again."` を表示して中止する
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
Cannot reset: there are incomplete jobs.
Pending jobs: 3, 7, 12

$ cjob reset   # 全ジョブ完了後
Delete all 15 jobs and their logs. Are you sure? [y/N] y
Reset started. Please wait for background cleanup to complete.
```

## 15. `cjob usage` の動作

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

使用実績がない場合は `"No usage data."` を表示する。

### Resource Quota の表示

レスポンスの `resource_quota` が `null` でない場合、使用状況テーブルの前に Resource Quota セクションをテーブル形式で表示する。

各列の意味:
- **Resource**: リソース種別（CPU / Memory / GPU / Jobs）
- **Used**: 現在の使用量
- **Hard**: クォータ上限
- **Remaining**: 残り（`hard - used`）
- **Use%**: 使用率（`used / hard * 100`）、小数点以下1桁

単位変換:
- CPU: ミリコア → コア数、小数点以下1桁（例: `280.0`）
- メモリ: MiB → GiB、整数（例: `800Gi`）
- GPU: 個数のまま（例: `1`）
- Jobs: 個数のまま（例: `10`）

GPU 行は `hard_gpu == 0` の場合は非表示とする。
Jobs 行は `hard_count` が `null` の場合は非表示とする。

```
$ cjob usage

Resource Quota
──────────────────────────────────────────────────
  Resource       Used       Hard  Remaining    Use%
  CPU           280.0      300.0       20.0   93.3%
  Memory        800Gi     1250Gi      450Gi   64.0%
  GPU               1          4          3   25.0%
  Jobs             10         50         40   20.0%

Resource Usage (past 7 days)
──────────────────────────────────────────────────
  Date              CPU (core·h)    Mem (GiB·h)
  2026-03-23               24.0           48.0
  2026-03-24               12.5           25.0
  2026-03-25                8.0           16.0
  ────────────────────────────────────────────────
  Total                    44.5           89.0
```

## 16. `cjob update` の動作

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
Update? 1.2.0 -> 1.3.0 [y/N] y
Update complete. (1.3.0)

# ベータ版を含む最新に更新
$ cjob update --pre
Update? 1.2.0 -> 1.3.1-beta.2 [y/N] y
Update complete. (1.3.1-beta.2)

# 確認をスキップ
$ cjob update -y
Update complete. (1.3.0)

# すでに最新の場合
$ cjob update
Already up to date (1.3.0)

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
Update? 1.2.0 -> 1.3.1-beta.1 [y/N] y
Update complete. (1.3.1-beta.1)
```

## 17. `cjob flavor` の動作

`GET /v1/flavors` を呼び出し、利用可能な ResourceFlavor の一覧とリソース上限を表示する。認証不要のエンドポイントを使用するため、ServiceAccount JWT がなくても実行できる。

### `cjob flavor list`

利用可能な flavor の一覧を表示する。デフォルト flavor は `*` でマークする。

```
$ cjob flavor list
NAME             GPU    NODES    IMAGE                              DEFAULT
cpu              -      2        -                                    *
gpu-a100         yes    1        your-registry/cjob-cuda:2.1.0
```

IMAGE 列はその flavor の既定イメージ（`RESOURCE_FLAVORS` の `image`）。設定されていない flavor では `-` を表示し、その flavor ではジョブ投入時に投入 Pod のイメージが使われる（§4「image の決まり方」参照）。

### `cjob flavor info <name>`

指定した flavor のリソース上限とタスクあたりの上限を表示する。

QUOTA は ClusterQueue の nominalQuota（flavor 全体で共有するリソース総量）。TASK LIMIT はタスクあたりのリソース上限で、`min(max_node_allocatable, nominalQuota)` で計算される。GPU 非対応 flavor では GPU 行を省略する。

```
$ cjob flavor info cpu
name:   cpu
GPU:    no
image:  -

RESOURCE      QUOTA    TASK LIMIT
CPU             256           128
Memory       1000Gi       503.4Gi
```

GPU 対応 flavor の場合は GPU 行も表示する。

```
$ cjob flavor info gpu-a100
name:   gpu-a100
GPU:    yes
image:  your-registry/cjob-cuda:2.1.0

RESOURCE      QUOTA    TASK LIMIT
CPU              64            64
Memory        500Gi         500Gi
GPU               4             4
```

`image` 行はその flavor の既定イメージ。設定されていない flavor では `-` を表示する。

Watcher 未同期で quota 情報がない場合はメッセージを表示する。

```
$ cjob flavor info cpu
name:   cpu
GPU:    no
image:  -

(Resource information is not available yet)
```

存在しない flavor を指定した場合はエラーを表示する。

```
$ cjob flavor info xxx
Error: flavor 'xxx' does not exist. Available flavors: cpu, gpu-a100
```

# CLI 設計

## 1. 基本コマンド

```bash
cjob add -- <command...>
cjob list
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
cjob reset
cjob logs <job-id>
cjob logs --follow <job-id>
```

## 2. `cjob add` の動作

1. `pwd` を取得する
2. export 済み環境変数を収集する（`PATH` / `VIRTUAL_ENV` を含む）
3. `JUPYTER_IMAGE` 環境変数からコンテナイメージ名を取得する
4. `--` 以降の argv を shell-safe に連結して command を生成する
5. ServiceAccount JWT と namespace を固定パスから読み取る
6. API にジョブ投入を行う（`image` フィールドを含む）
7. `job_id` を表示する

## 3. `cjob logs` の動作

`cjob logs` はログの閲覧に特化する。ログの削除は `cjob delete` または `cjob reset` が担う。

ジョブ状態によって以下のように動作する。

| 状態 | 動作 |
|---|---|
| QUEUED / DISPATCHING / DISPATCHED | ログファイル未生成のため最大 5分待機（待機中は状態と経過時間を表示） |
| RUNNING | ファイル生成後に tail -f で追跡（`--follow` 時） |
| SUCCEEDED / FAILED | ファイルを全量表示して終了 |
| CANCELLED | ファイルがあれば表示、なければ "No logs available" |
| DELETING | reset 処理中。ファイルがあれば表示、なければ "No logs available（reset 処理中）" を表示して終了 |

ログファイルは PVC 上（`/home/jovyan/.cjob/logs/<job_id>/`）にあり、CLI が直接読む。API を経由しない。

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

## 4. `cjob list` の動作

`GET /v1/jobs` を呼び出し、結果を表形式で表示する。デフォルトでは最新50件を JOB_ID 昇順で表示する。

```
$ cjob list
JOB_ID  STATUS      COMMAND                                    CREATED              FINISHED
51      SUCCEEDED   python main.py --alpha 0.1 --beta 16       2026-03-23 12:34     2026-03-23 12:37
52      RUNNING     python main.py --alpha 0.2 --beta 16       2026-03-23 12:35     -
53      QUEUED      python main.py --alpha 0.5 --beta 16       2026-03-23 12:35     -
（100件中最新の50件を表示。全件表示するには --all を使用してください）
```

オプション：

- `--status <status>`：指定したステータスのジョブのみ表示（例: `--status RUNNING`）
- `--limit <n>`：表示件数を最新 n 件に制限する（1 以上）。省略時はデフォルト50件
- `--all`：全件表示する
- `--reverse`：JOB_ID の降順で表示する

```bash
cjob list                    # 最新 50 件を昇順で表示
cjob list --all              # 全件を昇順で表示
cjob list --reverse          # 最新 50 件を降順で表示
cjob list --status RUNNING   # RUNNING の最新 50 件を表示
cjob list --limit 10         # 最新 10 件のみ表示
```

表示件数がジョブ総数より少ない場合は、省略されていることを示すメッセージを標準エラー出力に表示する。

command は長い場合に末尾を省略して表示する（例: 40文字で切り捨て）。

## 5. `cjob status` の動作

`GET /v1/jobs/{job_id}` を呼び出し、主要フィールドを整形して表示する。

```
$ cjob status 2
job_id:       2
status:       RUNNING
command:      python main.py --alpha 0.2 --beta 16
cwd:          /home/jovyan/project-a/exp1
created_at:   2026-03-23 12:35:00
dispatched_at: 2026-03-23 12:35:05
finished_at:  -
k8s_job_name: cjob-alice-2
log_dir:      /home/jovyan/.cjob/logs/2
```

存在しない job_id を指定した場合はエラーメッセージを表示して終了する。

```
$ cjob status 999
エラー: job_id 999 が見つかりません。
```

## 6. CLI の設定

Submit API のエンドポイントは環境変数 `CJOB_API_URL` から読む。未設定時はデフォルト値を使用する。

```
# ※ CLI の実装は Rust（reqwest クレート等）で行う。以下は概念説明のための擬似コードである。

SUBMIT_API_URL = env("CJOB_API_URL")
              ?? "http://submit-api.cjob-system.svc.cluster.local:8080"
```

## 7. `cjob cancel` の動作

job_id の指定形式をパースして job_id のリストに展開し、`POST /v1/jobs/cancel` を呼ぶ。

```
# ※ CLI の実装は Rust で行う。以下は概念説明のための擬似コードである。

fn parse_job_ids(expr) -> Vec<u32>:
    // "1-5,8,10-12" → [1, 2, 3, 4, 5, 8, 10, 11, 12]
    expr を ',' で分割して各パートを処理する
        '-' を含む場合: start..=end の連番を追加
        それ以外: その数値を追加
    重複除去して昇順ソートして返す
```

## 8. `cjob delete` の動作

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
        deleted の各 job_id に対応するログディレクトリ（/home/jovyan/.cjob/logs/<job_id>/）を削除する
        deleted があれば "削除しました" を表示する
        skipped があれば:
            reason が "running" のジョブ → "実行中のため削除できませんでした。先に cjob cancel を実行してください"
            reason が "deleting" のジョブ → "リセット処理中のため削除できませんでした"
            （API レスポンスの skipped[].reason に基づいて分岐する）
        not_found があれば "見つかりませんでした" を表示する
```

## 9. `cjob reset` の動作

1. `GET /v1/jobs` でジョブ一覧を取得し、以下の順で確認する
   - `DELETING` のジョブが1件でも存在する場合は「前回のリセット処理がまだ完了していません。しばらく待ってから再試行してください。」を表示して中止する
   - `QUEUED` / `DISPATCHING` / `DISPATCHED` / `RUNNING` のジョブが1件でも存在する場合は job_id を表示して中止する
2. 全ジョブが完了済みの場合はユーザーに確認プロンプトを表示する
3. y の場合のみ以下を順に実行する
   1. PVC 上のログディレクトリ（`/home/jovyan/.cjob/logs/`）を削除する（API 呼び出し前に削除することで、API 呼び出し後に CLI がクラッシュしても Watcher が counter をリセットした後の job_id=1 再利用時に log_dir が存在しない状態を保証する）
   2. `POST /v1/reset` を呼び出す（202 Accepted が返る）
4. リセット開始メッセージを表示して終了する（完了を待たない）

実際の K8s Job 削除・DB クリーンアップ・カウンターリセットは Watcher が非同期で処理する。
リセット完了前に `cjob add` を実行すると、Submit API は `DELETING` ジョブが存在するとして 409 を返し投入を拒否する。

```
$ cjob reset
完了していないジョブがあるためリセットできません。
完了待ちのジョブ: 3, 7, 12

$ cjob reset   # 全ジョブ完了後
全 15 件のジョブとログを削除します。よろしいですか？ [y/N] y
リセットを開始しました。バックグラウンドでクリーンアップが完了するまでお待ちください。
```

# CJob 設計書

## 1. 概要

本設計書は、オンプレ Kubernetes 環境上で動作する、**ユーザー向けジョブキューシステム `cjob`** の設計をまとめたものである。  
本システムは、研究計算・parameter sweep・バッチ計算を対象とし、**ユーザーに Kubernetes の Job / Pod / YAML を意識させずに**、シェルコマンドをそのままジョブとして投入できることを目的とする。

本システムは、以下の方針で設計する。

- ユーザー操作は `cjob add <job command>` を基本とする
- 実行環境は Kubernetes 上に構築する
- 実行単位は **1コマンド = 1 Kubernetes Job**
- ジョブ投入数が非常に多くなることを想定し、**前段メッセージキュー**を導入する
- Kubernetes 上の実行制御には **Kueue** を用いる
- ユーザーの作業ディレクトリと環境変数を可能な範囲でそのまま再現してジョブを実行する
- 将来的に Prefect 等の上位 orchestration 層を追加可能な構成とする

## 2. 提供したいジョブキューシステムの機能

### 2.1 ユーザー向け基本機能

本システムが提供する主要機能は次の通りである。

- シェルコマンドをジョブとして投入する
- 投入済みジョブの一覧を確認する
- 個別ジョブの状態を確認する
- ジョブをキャンセルする
- ジョブのログを確認する（リアルタイム追跡含む）
- parameter sweep をまとめて投入する
- 将来的に workflow engine から API 経由で利用できる

### 2.2 ジョブ投入時に再現したい情報

ユーザーがジョブを投入した時点の以下の情報をジョブ実行時に再現する。

- 作業ディレクトリ (`cwd`)
- export 済み環境変数（仮想環境の `PATH` / `VIRTUAL_ENV` を含む）
- 実行コマンド文字列

### 2.3 実行制御上の機能

- ジョブの一時保管
- ジョブの Kubernetes Job への変換
- Kueue による admission 制御
- namespace ごとの dispatch 数制御
- namespace ごとの ResourceQuota による大枠の公平化
- 実行状態の追跡
- Kubernetes Job / Pod 状態と内部状態 DB の整合

## 3. ジョブキューシステムの使用例

### 3.1 単一ジョブの投入

```bash
cjob add -- python main.py --alpha 0.1 --beta 16
```

### 3.2 シェルスクリプトの実行

```bash
cjob add -- bash run_experiment.sh case001
```

### 3.3 仮想環境を利用した実行

```bash
source /workspace/myenv/bin/activate
cjob add -- python main.py --config config.yaml
# PATH / VIRTUAL_ENV が export 済みのため Job Pod で venv が再現される
```

### 3.4 ジョブ一覧表示

```bash
cjob list
```

### 3.5 状態確認

```bash
cjob status <job-id>
```

### 3.6 キャンセル

```bash
cjob cancel <job-id>
```

### 3.7 ログ取得

```bash
# 完了後に確認
cjob logs <job-id>

# リアルタイム追跡
cjob logs --follow <job-id>

# ログ削除
cjob logs --delete <job-id>
cjob logs --delete-all
```

## 4. ジョブキューを動かす環境の前提

### 4.1 インフラ前提

本システムは次の前提で構築する。

- Kubernetes クラスタが存在する
- ユーザーごとに namespace が分離されている（手動作成・スクリプトで自動化）
- ユーザー namespace ごとに作業用 PVC が存在する
- ユーザー Pod はその PVC を `/workspace` に mount している
- 認証基盤として Keycloak + JupyterHub が既にある
- Kueue を Kubernetes クラスタに導入する
- RabbitMQ を前段メッセージキューとして導入する（新規デプロイ）
- 状態管理用に PostgreSQL を使用する（新規デプロイ）
- NFS subdir external provisioner を導入済み
- ジョブキューシステム専用ノードには `role=parallel-computing` ラベルが付与されている
- 想定規模：ユーザー数 20人・同時実行ジョブ数 300程度

### 4.2 実行環境前提

- **ジョブ投入を行う Pod とジョブを実行する Pod は同じ image を使う**
- image は固定し、ユーザーが指定しない（DockerHub に public push）
- ベース OS：Ubuntu 24.04
- PVC 名はユーザー名と一致している
- mount path は `/workspace` に固定する
- 実行 shell は `/bin/bash -lc` を基本とする
- 作業ディレクトリは `/workspace` 配下に限定する
- export 済み環境変数のみ再現対象とする（仮想環境の `PATH` / `VIRTUAL_ENV` を含む）
- shell function / alias / shell option は再現対象外とする
- ユーザーは `/workspace` 配下に Python 仮想環境を作成して管理する
- Job Pod と User Pod は同一 image のため、venv 内の C 拡張ライブラリ互換性が保たれる

### 4.3 スケジューリング前提

- Kubernetes Job が実行単位である
- Kueue は admission / queueing / fairness を担う
- ResourceQuota は namespace ごとの aggregate resource usage 制御に用いる
- ジョブ数が多くなるため、Kueue の前段にメッセージキューを置く
- Kueue に流す Job 数は dispatcher が制御する

## 5. 提供したい機能を実現するために必要な機能の一覧

本システムを実現するためには、次の機能が必要である。

### 5.1 CLI 機能

- `cjob add`
- `cjob sweep`
- `cjob list`
- `cjob status`
- `cjob cancel`
- `cjob logs`（`--follow` / `--delete` / `--delete-all` オプション含む）

### 5.2 submit 機能

- 現在の作業ディレクトリ取得
- export 済み環境変数取得
- コマンド文字列の保存
- ユーザー namespace 解決（ServiceAccount の namespace ファイルから取得）
- ジョブ ID 発行
- 内部 DB へのジョブ登録
- RabbitMQ へのメッセージ publish

### 5.3 前段 MQ 機能

- ジョブメッセージの永続化
- producer からの publish 確認
- dispatcher への配送
- manual ack / reject / requeue
- Dead Letter Queue による遅延 requeue

### 5.4 dispatcher 機能

- メッセージ consume
- dispatch budget の計算
- Kubernetes Job 生成
- Job 作成成功時 ack
- Job 作成失敗時の再試行 / requeue / fail
- DB 状態更新
- 起動時の DISPATCHING 状態リセット

### 5.5 Kubernetes 実行機能

- fixed image で Job を作成
- PVC を `/workspace` に mount
- `workingDir` に submit 時の cwd を設定
- `env` に submit 時の環境変数を注入
- command を `/bin/bash -lc "<saved command>"` で実行
- ログを PVC 上に tee で書き出し
- Kueue queue ラベルの付与

### 5.6 監視 / 状態同期機能

- Job / Pod 状態監視
- DB 状態更新
- 完了 / 失敗判定
- orphan Job 検出
- cancel 反映
- retry 可能なジョブの管理

### 5.7 parameter sweep 展開機能

- ファイル入力からの展開（必要なら）
- sweep 展開結果を logical job 群として登録

## 6. 必要な機能を実装する方針

### 6.1 全体方針

前段 MQ + dispatcher + Kueue + Kubernetes Job の構成を採用する。  
Argo Workflows は今回は採用しない。理由は以下の通り。

- 目的は workflow engine ではなく job queue system の構築である
- Argo は queued workflow を持てるが、Kubernetes CR を大量に作る点は変わらない
- 「提出済みだが未 materialize」の軽量保管には前段 MQ の方が適する
- 将来 Prefect を導入する場合も、前段 MQ + Job 実行基盤の方が組み合わせやすい

### 6.2 前段 MQ の実装方針

前段 MQ には **RabbitMQ** を採用する。  
Python からのアクセスには **Kombu** を用いる。

採用理由は以下の通り。

- durable queue を使える
- persistent message を使える
- publisher confirm がある
- consumer ack / nack / requeue がある
- Dead Letter Queue による遅延 requeue が実現できる
- Python 実装が安定している
- Celery のような高レベル task queue をそのまま使うより、今回の用途に適している

### 6.3 状態管理の実装方針

ジョブ状態の正本は **PostgreSQL** に保存する。  
RabbitMQ はメッセージ配送用であり、ジョブ状態の正本としては使わない。

理由:

- `list/status/cancel/logs` を実装しやすい
- RabbitMQ のみでは user-facing state 管理が難しい
- dispatch budget 判定に DB 状態を使える
- 再起動時の再整合がしやすい

### 6.4 実行制御の実装方針

dispatcher が DB と RabbitMQ を見ながら Job を materialize する。

- RabbitMQ: 新着通知
- PostgreSQL: pending / queued / dispatched の正本
- Kubernetes Job: 実行単位
- Kueue: 実行 admission 制御

### 6.5 ジョブ投入コンテキストの再現方針

submit 時に取得した以下を Job Pod に反映する。

- `cwd` → Kubernetes container `workingDir`
- `env` → Kubernetes container `env`（`PATH` / `VIRTUAL_ENV` を含む全 export 済み環境変数）
- `command` → `bash -lc "<command>"`

### 6.6 parameter sweep の実装方針

parameter sweep は **logical job を複数生成する方式**とする。  
Indexed Job は採用しない。

理由:

- 1コマンド = 1ジョブという UX 方針に合う
- 各ジョブの状態・キャンセル・再試行を個別に扱いやすい
- ログ・課金・進捗管理の粒度を揃えやすい

大量投入による Kueue / Job オブジェクト増加は、前段 MQ と dispatcher により制御する。

### 6.7 ログ取得方針

Job Pod のコマンドを tee でラップし、stdout / stderr を PVC 上に保存する。

- 保存先：`/workspace/.cjob/logs/<job_id>/stdout.log` および `stderr.log`
- CLI は User Pod 内から PVC 上のファイルを直接読む
- リアルタイム追跡は CLI が tail -f 相当の処理を行う
- ログの削除はユーザーが `cjob logs --delete` で明示的に行う

## 7. システム構成

### 7.1 論理構成

```text
User Pod (namespace: user-alice)
  └─ cjob CLI
       └─ HTTP + ServiceAccount JWT
            └─ Submit API (namespace: cjob-system)
                 ├─ PostgreSQL
                 └─ RabbitMQ

Dispatcher (namespace: cjob-system)
  ├─ RabbitMQ Consumer
  ├─ PostgreSQL
  └─ Kubernetes API
       └─ Job + Kueue LocalQueue (namespace: user-alice)

Watcher / Reconciler (namespace: cjob-system)
  ├─ Kubernetes API
  └─ PostgreSQL

Kubernetes Job Pod (namespace: user-alice)
  ├─ fixed image (DockerHub)
  ├─ PVC mounted at /workspace
  ├─ workingDir = cwd
  ├─ env = submit-time env
  └─ stdout/stderr → /workspace/.cjob/logs/<job_id>/
```

### 7.2 namespace 構成

```text
cjob-system      : Submit API / Dispatcher / Watcher / RabbitMQ / PostgreSQL
user-<username>    : User Pod / Job Pod / LocalQueue / ResourceQuota / PVC
```

### 7.3 主要コンポーネント

| コンポーネント | 種類 | Replica | namespace |
|---|---|---|---|
| Submit API | Deployment | 1 | cjob-system |
| Dispatcher | Deployment | 1 | cjob-system |
| Watcher / Reconciler | Deployment | 1 | cjob-system |
| RabbitMQ | StatefulSet | 1 | cjob-system |
| PostgreSQL | StatefulSet | 1 | cjob-system |
| Kubernetes Job | Job | - | user-\<username\> |

Dispatcher と Watcher は Replica 複数にすると二重 dispatch・二重更新が発生するため、1 固定とする。

## 8. RabbitMQ 設計

### 8.1 採用構成

以下の Exchange と Queue を使用する。

- Exchange（通常）: `cjob`（type: direct）
- Exchange（retry）: `cjob.retry`（type: direct）
- Queue（通常）: `cjob.submit`
- Queue（retry）: `cjob.retry`
- Routing key（通常）: `submit`
- Routing key（retry）: `retry`

### 8.2 Queue 設定

通常 Queue:

- durable = true
- message persistent
- manual ack
- prefetch_count = 1
- dead-letter-exchange: `cjob.retry`（失敗時の転送先）

retry Queue（遅延 requeue 用）:

- x-message-ttl: 30000（30秒後に通常 Queue へ戻す）
- x-dead-letter-exchange: `cjob`
- x-dead-letter-routing-key: `submit`

### 8.3 メッセージ内容

1メッセージ = 1 logical job とする。

```json
{
  "job_id": "01HXYZ...",
  "user": "alice",
  "namespace": "user-alice",
  "cwd": "/workspace/project-a/exp1",
  "command": "python main.py --alpha 0.1 --beta 16",
  "env": {
    "OMP_NUM_THREADS": "4",
    "PYTHONPATH": "/workspace/project-a",
    "VIRTUAL_ENV": "/workspace/myenv",
    "PATH": "/workspace/myenv/bin:/usr/local/bin:/usr/bin"
  },
  "resources": {
    "cpu": "2",
    "memory": "4Gi",
    "gpu": 0
  },
  "retry_count": 0,
  "max_retries": 5,
  "submitted_at": "2026-03-23T12:34:56Z"
}
```

### 8.4 RabbitMQ の役割

RabbitMQ は以下のみを担う。

- 新規ジョブの一時保管
- dispatcher への配送
- ack / nack による配送制御
- Dead Letter Queue による遅延 requeue

RabbitMQ はジョブ状態の正本ではない。

## 9. PostgreSQL 設計

### 9.1 `jobs` テーブル

```sql
CREATE TABLE jobs (
    job_id        TEXT PRIMARY KEY,
    "user"        TEXT NOT NULL,
    namespace     TEXT NOT NULL,
    command       TEXT NOT NULL,
    cwd           TEXT NOT NULL,
    env_json      JSONB NOT NULL DEFAULT '{}',
    cpu           TEXT NOT NULL,
    memory        TEXT NOT NULL,
    gpu           INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    k8s_job_name  TEXT,
    log_dir       TEXT,          -- /workspace/.cjob/logs/<job_id>
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    last_error    TEXT
);
```

### 9.2 `job_events` テーブル

```sql
CREATE TABLE job_events (
    id           BIGSERIAL PRIMARY KEY,
    job_id       TEXT NOT NULL REFERENCES jobs(job_id),
    event_type   TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 9.3 状態遷移

```text
QUEUED
  └─ DISPATCHING（Dispatcher がメッセージを取得した時点）
       ├─ DISPATCHED（Kubernetes Job 作成成功）
       │    └─ RUNNING（Watcher が Pod 実行中を検知）
       │         ├─ SUCCEEDED
       │         └─ FAILED
       ├─ QUEUED（再試行時：Dispatcher 再起動・requeue）
       └─ FAILED（バリデーションエラー・最大 retry 超過）
CANCELLED（任意のタイミングでユーザーがキャンセル）
```

## 10. Kueue 設計

### 10.1 ResourceFlavor

ジョブキューシステム専用ノード（`role=parallel-computing`）を対象とする。

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ResourceFlavor
metadata:
  name: parallel-computing-flavor
spec:
  nodeLabels:
    role: parallel-computing
```

### 10.2 ClusterQueue

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: cjob-cluster-queue
spec:
  namespaceSelector: {}
  resourceGroups:
    - coveredResources: ["cpu", "memory"]
      flavors:
        - name: parallel-computing-flavor
          resources:
            - name: cpu
              nominalQuota: "256"
              borrowingLimit: "0"
            - name: memory
              nominalQuota: "1000Gi"
              borrowingLimit: "0"
  queueingStrategy: BestEffortFIFO
  cohort: cjob-cohort
  preemption:
    reclaimWithinCohort: Never   # 実行中ジョブの強制終了を禁止
    withinClusterQueue: Never
```

`BestEffortFIFO` を採用する理由：`StrictFIFO` では1ユーザーの大量投入が全体を止める可能性があるため。

cohort を設定する理由：使われていないリソースを他ユーザーが借りて使えるようにするため。

preemption を禁止する理由：研究計算ではジョブが途中で強制終了されると結果が失われるケースが多いため。

### 10.3 LocalQueue

各 user namespace に作成する。

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  name: default
  namespace: user-alice
spec:
  clusterQueue: cjob-cluster-queue
```

### 10.4 ResourceQuota

各 user namespace に作成する安全網。

計算根拠：
- CPU: 256 core ÷ 20 user = 12.8 → 12 core
- memory: 1000 GiB ÷ 20 user = 50 GiB
- Job 数: dispatch_budget(30) より大きく設定 → 50

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: cjob-quota
  namespace: user-alice
spec:
  hard:
    count/jobs.batch: "50"
    requests.cpu: "12"
    requests.memory: "50Gi"
    limits.cpu: "12"
    limits.memory: "50Gi"
```

cohort により空きがあれば上限を超えて借り入れできる。ResourceQuota はあくまで安全網として機能する。

### 10.5 Kubernetes Job テンプレート

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  namespace: user-alice
  name: cjob-01xyz
  labels:
    kueue.x-k8s.io/queue-name: default
spec:
  ttlSecondsAfterFinished: 86400    # 完了後 24時間で Job / Pod を削除
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: worker
          image: <dockerhub-repo>/lab-runtime:latest
          workingDir: /workspace/project-a/exp1
          command: ["/bin/bash", "-lc"]
          args:
            - |
              LOG_DIR=/workspace/.cjob/logs/01xyz
              mkdir -p "${LOG_DIR}"
              exec > >(tee "${LOG_DIR}/stdout.log") \
                   2> >(tee "${LOG_DIR}/stderr.log" >&2)
              python main.py --alpha 0.1 --beta 16
          env:
            - name: OMP_NUM_THREADS
              value: "4"
            - name: PYTHONPATH
              value: "/workspace/project-a"
            - name: VIRTUAL_ENV
              value: "/workspace/myenv"
            - name: PATH
              value: "/workspace/myenv/bin:/usr/local/bin:/usr/bin"
          volumeMounts:
            - name: workspace
              mountPath: /workspace
          resources:
            requests:
              cpu: "2"
              memory: "4Gi"
            limits:
              cpu: "2"
              memory: "4Gi"
      volumes:
        - name: workspace
          persistentVolumeClaim:
            claimName: workspace
```

## 11. API 設計

CLI はこの API を呼ぶ薄いクライアントとして実装する。  
全エンドポイントで ServiceAccount JWT による認証・認可を行う（詳細は auth_policy.md 参照）。

### 11.1 POST /v1/jobs

ジョブを1件投入する。

#### request

```json
{
  "command": "python main.py --alpha 0.1 --beta 16",
  "cwd": "/workspace/project-a/exp1",
  "env": {
    "OMP_NUM_THREADS": "4",
    "PYTHONPATH": "/workspace/project-a"
  },
  "resources": {
    "cpu": "2",
    "memory": "4Gi",
    "gpu": 0
  }
}
```

#### response

```json
{
  "job_id": "01HXYZ...",
  "status": "QUEUED"
}
```

### 11.2 POST /v1/jobs/sweep

parameter sweep を投入する。

#### request

```json
{
  "command_template": "python main.py --alpha '{{alpha}}' --beta '{{beta}}'",
  "cwd": "/workspace/project-a/exp1",
  "env": {
    "OMP_NUM_THREADS": "4"
  },
  "resources": {
    "cpu": "2",
    "memory": "4Gi",
    "gpu": 0
  },
  "sweep": {
    "type": "grid",
    "parameters": {
      "alpha": ["0.1", "0.2", "0.5"],
      "beta": ["8", "16"]
    }
  }
}
```

#### response

```json
{
  "submission_id": "subm_01...",
  "job_count": 6,
  "status": "QUEUED"
}
```

### 11.3 GET /v1/jobs

ジョブ一覧を取得する。JWT の namespace に属するジョブのみ返す。

#### response

```json
{
  "jobs": [
    {
      "job_id": "01HXYZ...",
      "status": "RUNNING",
      "command": "python main.py --alpha 0.1 --beta 16",
      "created_at": "2026-03-23T12:34:56Z"
    }
  ]
}
```

### 11.4 GET /v1/jobs/{job_id}

個別ジョブの詳細を取得する。

#### response

```json
{
  "job_id": "01HXYZ...",
  "status": "SUCCEEDED",
  "namespace": "user-alice",
  "command": "python main.py --alpha 0.1 --beta 16",
  "cwd": "/workspace/project-a/exp1",
  "k8s_job_name": "cjob-01xyz",
  "log_dir": "/workspace/.cjob/logs/01HXYZ...",
  "created_at": "2026-03-23T12:34:56Z",
  "dispatched_at": "2026-03-23T12:35:02Z",
  "finished_at": "2026-03-23T12:37:10Z"
}
```

### 11.5 POST /v1/jobs/{job_id}/cancel

ジョブをキャンセルする。

#### response

```json
{
  "job_id": "01HXYZ...",
  "status": "CANCELLED"
}
```

## 12. CLI 設計

### 12.1 基本コマンド

```bash
cjob add -- <command...>
cjob sweep -- <command with placeholders...> --grid key=v1,v2,...
cjob list
cjob status <job-id>
cjob cancel <job-id>
cjob logs <job-id>
cjob logs --follow <job-id>
cjob logs --delete <job-id>
cjob logs --delete-all
```

### 12.2 `cjob add` の動作

1. `pwd` を取得する
2. export 済み環境変数を収集する（`PATH` / `VIRTUAL_ENV` を含む）
3. `--` 以降の argv を shell-safe に連結して command を生成する
4. ServiceAccount JWT と namespace を固定パスから読み取る
5. API にジョブ投入を行う
6. `job_id` を表示する

### 12.3 `cjob sweep` の動作

1. command template を受け取る
2. grid 展開を行う
3. logical job 群として API に送る

### 12.4 `cjob logs` の動作

ジョブ状態によって以下のように動作する。

| 状態 | 動作 |
|---|---|
| QUEUED / DISPATCHING | ログファイル未生成のため最大 5分待機 |
| DISPATCHED / RUNNING | ファイル生成後に tail -f で追跡（`--follow` 時） |
| SUCCEEDED / FAILED | ファイルを全量表示して終了 |
| CANCELLED | ファイルがあれば表示、なければ "No logs available" |

ログファイルは PVC 上（`/workspace/.cjob/logs/<job_id>/`）にあり、CLI が直接読む。API を経由しない。

### 12.5 CLI の設定

Submit API のエンドポイントは image に埋め込まれた設定ファイルから読む。環境変数でオーバーライド可能。

```python
# 優先順位: 環境変数 > 設定ファイル
SUBMIT_API_URL = os.environ.get(
    "LABJOB_API_URL",
    "http://submit-api.cjob-system.svc.cluster.local:8080"
)
```

## 13. Dispatcher 設計

### 13.1 役割

Dispatcher は前段 MQ と Kubernetes Job 実行基盤の橋渡しを担う。

- RabbitMQ からメッセージを受け取る
- namespace ごとの dispatch budget を確認する
- Job を作成できる場合のみ Kubernetes Job を作る
- 成功時 ack
- 失敗時 reject / requeue / fail
- DB 状態を更新する

### 13.2 dispatch budget

```text
dispatch_budget = namespace_dispatch_limit - active_k8s_jobs(namespace)

namespace_dispatch_limit = 30（ConfigMap で設定）

active_k8s_jobs(namespace) には以下を含む:
  - Kueue 待ち Job
  - admitted Job
  - running Job
```

### 13.3 再試行ポリシー

失敗シナリオごとに対処を分ける。

| シナリオ | 対処 | 再試行間隔 | 上限 |
|---|---|---|---|
| K8s API 一時障害 | DLQ + TTL で遅延 requeue | 30秒 | 5回 |
| dispatch budget 不足 | 待機リストに保留・定期再確認 | 10秒ごと | なし（budget 回復まで） |
| バリデーションエラー | 即 FAILED | なし | なし |
| 永続的 K8s エラー | 即 FAILED | なし | なし |

#### K8s API 一時障害の処理

```python
except TemporaryK8sError:
    if job.retry_count >= job.max_retries:
        db.update_status(job.id, "FAILED", error="max retries exceeded")
        message.reject(requeue=False)   # DLQ へ転送（キューから除去）
    else:
        job.retry_count += 1
        message.reject(requeue=False)   # retry Queue 経由で 30秒後に再投入
        db.record_event(job.id, "RETRY", {"count": job.retry_count})
```

#### dispatch budget 不足の処理

budget 不足の namespace のメッセージを保留しつつ、他 namespace のジョブを処理し続ける。

```python
class Dispatcher:
    def __init__(self):
        self.pending: dict[str, list] = {}  # namespace → 待機メッセージリスト
        self.check_interval = 10            # 秒

    def run(self):
        while True:
            self.retry_pending()
            message = self.consume_one(timeout=self.check_interval)
            if message:
                self.dispatch(message)

    def dispatch(self, message):
        job = parse(message)
        if calc_budget(job.namespace) <= 0:
            self.pending.setdefault(job.namespace, []).append(message)
            return
        # Job 作成処理...

    def retry_pending(self):
        for namespace, messages in list(self.pending.items()):
            budget = calc_budget(namespace)
            for message in messages[:budget]:
                self.dispatch(message)
                messages.remove(message)
```

### 13.4 ack / reject ルール

| 状況 | 処理 |
|---|---|
| Job 作成成功 | `ack` |
| K8s API 一時障害（retry_count < max_retries） | `reject(requeue=False)` → retry Queue 経由で再投入 |
| K8s API 一時障害（retry_count >= max_retries） | `reject(requeue=False)` + DB を `FAILED` |
| バリデーションエラー | `reject(requeue=False)` + DB を `FAILED` |
| dispatch budget 不足 | ack も reject もしない（unacked のまま保留） |

### 13.5 起動時の初期化処理

Dispatcher 再起動時に `DISPATCHING` で止まっているジョブを `QUEUED` に戻す。

```python
def on_startup():
    db.reset_stale_dispatching_jobs()
    # UPDATE jobs SET status = 'QUEUED' WHERE status = 'DISPATCHING'
```

## 14. Watcher / Reconciler 設計

### 14.1 役割

Watcher / Reconciler は Kubernetes 側の実行状態を DB に反映する。

- Job 状態の監視
- Pod 状態の監視
- `RUNNING` / `SUCCEEDED` / `FAILED` への遷移
- orphan Job 検出
- DB と Kubernetes のズレ修正

### 14.2 必要性

前段 MQ を導入すると、submission state と execution state が分かれる。  
そのため dispatcher だけでなく watcher が必要である。

### 14.3 最小アルゴリズム

1. Kubernetes Job 一覧を定期監視（または watch API を使用）
2. Job の `status.conditions` を解釈
3. `cjob.io/job-id` ラベルから対応する `job_id` を特定
4. DB 状態を更新

## 15. 実装に使用するパッケージ / 技術

### 15.1 Python パッケージ

- **Kombu**: RabbitMQ producer / consumer 実装用
- **FastAPI**: Submit API 実装用
- **SQLAlchemy**: PostgreSQL ORM / DB access
- **psycopg**: PostgreSQL ドライバ
- **kubernetes**: Kubernetes Job 作成 / 状態監視用
- **Pydantic**: API リクエスト / レスポンス定義用
- **Typer**: `cjob` CLI 実装用

### 15.2 ミドルウェア

- **RabbitMQ**
- **PostgreSQL**
- **Kubernetes**
- **Kueue**

## 16. 実装方針の詳細

### 16.1 submit の正本管理

ジョブ投入時は次の順で行う。

1. CLI が `cwd`、`env`、`command` を集める
2. CLI が ServiceAccount JWT と namespace を固定パスから読み取る
3. API が `job_id` を発行する
4. PostgreSQL に `QUEUED` で保存する（`log_dir` も同時に設定）
5. RabbitMQ に publish する
6. publisher confirm を待つ
7. 成功を返す

### 16.2 Dispatcher の動作アルゴリズム

起動時:

1. `DISPATCHING` 状態のジョブを `QUEUED` に戻す（再起動時の整合）

メインループ:

1. 待機リストの budget 再確認・再 dispatch
2. RabbitMQ から 1 メッセージ取得（タイムアウト: 10秒）
3. DB 上で `DISPATCHING` に更新
4. dispatch budget を確認（不足なら待機リストに追加して次のループへ）
5. Job を作成
6. 成功なら `DISPATCHED` に更新して `ack`
7. 一時障害なら retry_count をインクリメントして DLQ 経由で再投入
8. 永続障害・バリデーションエラーなら `FAILED` に更新して `reject`

### 16.3 Watcher の最小アルゴリズム

1. Kubernetes Job 一覧を監視
2. Job 状態を解釈
3. `cjob.io/job-id` ラベルで対応する `job_id` を特定
4. DB 状態を更新

## 17. 実装手順

以下の順番で実装する。

### Step 1: 基本インフラ準備

- RabbitMQ をデプロイする（StatefulSet + PVC）
- PostgreSQL をデプロイする（StatefulSet + PVC）
- Kueue を導入する
- ResourceFlavor / ClusterQueue を作成する
- namespace 作成スクリプトを整備する（LocalQueue / ResourceQuota / ServiceAccount 含む）
- fixed runtime image をビルドして DockerHub に push する

### Step 2: `cjob` CLI の最小実装

- `cjob add`
- `cjob list`
- `cjob status`
- `cjob cancel`

この段階では `cjob add` から API にジョブ定義を送れるようにする。

### Step 3: Submit API 実装

- ServiceAccount JWT による認証・認可
- `POST /v1/jobs`
- `GET /v1/jobs`
- `GET /v1/jobs/{job_id}`
- `POST /v1/jobs/{job_id}/cancel`

併せて PostgreSQL スキーマを作成する。

### Step 4: RabbitMQ Producer 実装

- Submit API 内で Kombu を用いた publish を実装する
- publisher confirm を有効にする
- durable queue・retry queue を宣言する

### Step 5: Dispatcher 実装

- 起動時初期化（DISPATCHING → QUEUED）
- RabbitMQ consumer 実装（prefetch_count=1）
- DB から状態を読み書き
- dispatch budget 計算
- Kubernetes Job 作成（tee ラップコマンド含む）
- 再試行ポリシー実装（DLQ・待機リスト）
- ack / reject 実装

### Step 6: Watcher / Reconciler 実装

- Job 状態監視
- Pod 状態監視
- DB 更新
- 失敗理由反映

### Step 7: ログ取得実装

- Job Pod のコマンドに tee ラップを追加（Step 5 で実施済み）
- `cjob logs`（完了後表示）
- `cjob logs --follow`（リアルタイム追跡）
- `cjob logs --delete` / `--delete-all`

### Step 8: parameter sweep 実装

- 複数 logical job を DB + RabbitMQ に投入
- 一括投入時の UX 改善

### Step 9: 運用機能追加

- metrics
- tracing
- dead-letter queue の監視
- cleanup policy

## 18. 初期実装のスコープ

初期実装では、以下に絞る。

- fixed image のみ
- CPU / memory ジョブのみ
- `cjob add`
- `cjob list`
- `cjob status`
- `cjob cancel`
- `cjob logs`（`--follow` 含む）
- 単純な `grid` sweep
- RabbitMQ 1 queue + retry queue
- PostgreSQL 1 DB
- namespace ごとの LocalQueue
- 1種類の runtime image

以下は後回しにできる。

- GPU 対応
- 複数 runtime class
- 高度な retry policy
- QoS / priority
- Web UI
- Prefect 連携
- Argo 連携
- artifact 管理

## 19. 将来拡張

将来的には次を追加可能である。

- Prefect から submit API を呼ぶ orchestration 連携
- Web UI
- retry failed only
- 実行履歴可視化
- queue priority
- 複数 ClusterQueue の使い分け
- GPU / highmem クラス
- 成果物管理
- ログ集約

## 20. 最終方針

本システムは、以下の構成を採用する。

- **ジョブ投入 UX**: `cjob add <job command>`
- **実行単位**: 1コマンド = 1 Kubernetes Job
- **前段キュー**: RabbitMQ（通常 Queue + retry Queue）
- **メッセージライブラリ**: Kombu
- **状態管理**: PostgreSQL
- **実行制御**: Dispatcher + Kueue
- **実行基盤**: Kubernetes Job
- **実行環境**: fixed image（Ubuntu 24.04 / DockerHub）+ namespace PVC mounted at `/workspace`
- **再現対象**: submit 時の `cwd` / exported env（仮想環境 PATH 含む）/ command
- **ログ保存**: PVC 上の `/workspace/.cjob/logs/<job_id>/`
- **ログ取得**: CLI が PVC を直接読む（API 経由なし）
- **認証・認可**: ServiceAccount JWT + TokenReview（詳細は auth_policy.md 参照）
- **大量投入対応**: 前段 MQ + dispatch budget により Job materialization を抑制する

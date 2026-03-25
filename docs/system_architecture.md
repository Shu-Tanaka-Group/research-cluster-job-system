# CJob 設計書

## 1. 概要

本設計書は、オンプレ Kubernetes 環境上で動作する、**ユーザー向けジョブキューシステム `cjob`** の設計をまとめたものである。  
本システムは、研究計算・バッチ計算を対象とし、**ユーザーに Kubernetes の Job / Pod / YAML を意識させずに**、シェルコマンドをそのままジョブとして投入できることを目的とする。

本システムは、以下の方針で設計する。

- ユーザー操作は `cjob add <job command>` を基本とする
- 実行環境は Kubernetes 上に構築する
- 実行単位は **1コマンド = 1 Kubernetes Job**
- ジョブ投入数が非常に多くなることを想定し、**DB スキャン型 Dispatcher** により dispatch を制御する
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
- namespace ごとの ResourceQuota による意図しない無制限消費の防止（安全網）
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
source /home/jovyan/myenv/bin/activate
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
```

### 3.8 完了済みジョブの削除

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

## 4. ジョブキューを動かす環境の前提

### 4.1 インフラ前提

本システムは次の前提で構築する。

- Kubernetes クラスタが存在する
- ユーザーごとに namespace が分離されている（手動作成・スクリプトで自動化）
- ユーザー namespace ごとに作業用 PVC が存在する
- ユーザー Pod はその PVC を `/home/jovyan` に mount している
- 認証基盤として Keycloak + JupyterHub が既にある
- Kueue を Kubernetes クラスタに導入する
- 状態管理用に PostgreSQL を使用する（新規デプロイ）
- NFS subdir external provisioner を導入済み
- ジョブキューシステム専用ノードには `role=parallel-computing` ラベルと `role=computing:NoSchedule` Taint が付与されている
- 想定規模：ユーザー数 20人・同時実行ジョブ数 300程度

### 4.2 実行環境前提

- **ジョブ投入を行う Pod とジョブを実行する Pod は同じ image を使う**
- image は User Pod の環境変数 `JUPYTER_IMAGE` から自動取得する（ユーザーが明示的に指定しない）
- JupyterHub の User Pod には `JUPYTER_IMAGE` に現在のコンテナイメージ名が設定されている
- `cjob` CLI は Rust で実装したシングルバイナリとして GitHub Releases で配布する
- ユーザーは CLI バイナリを各自のホームディレクトリ（例: `/home/jovyan/.local/bin/`）に配置する
- CLI は image には含めない
- ベース OS：Ubuntu 24.04
- PVC 名はユーザー名と一致している
- mount path は `/home/jovyan` に固定する
- 実行 shell は `/bin/bash -lc` を基本とする
- 作業ディレクトリは `/home/jovyan` 配下に限定する
- export 済み環境変数のみ再現対象とする（仮想環境の `PATH` / `VIRTUAL_ENV` を含む）
- shell function / alias / shell option は再現対象外とする
- ユーザーは `/home/jovyan` 配下に Python 仮想環境を作成して管理する
- Job Pod と User Pod は同一 image のため、venv 内の C 拡張ライブラリ互換性が保たれる

### 4.3 スケジューリング前提

- Kubernetes Job が実行単位である
- Kueue は admission / queueing / fairness を担う
- ResourceQuota は namespace ごとのバグ等による意図しない無制限消費を防ぐ安全網として用いる（公平化は Kueue の BestEffortFIFO が担う）
- Kueue に流す Job 数は Dispatcher が制御する

## 5. 提供したい機能を実現するために必要な機能の一覧

本システムを実現するためには、次の機能が必要である。

### 5.1 CLI 機能

- `cjob add`
- `cjob list`
- `cjob status`
- `cjob cancel`
- `cjob delete`
- `cjob reset`
- `cjob logs`（`--follow` オプション含む）

### 5.2 submit 機能

- 現在の作業ディレクトリ取得
- export 済み環境変数取得
- コンテナイメージ名取得（`JUPYTER_IMAGE` 環境変数から取得）
- コマンド文字列の保存
- ユーザー namespace 解決（ServiceAccount の namespace ファイルから取得）
- namespace ごとのジョブ総数上限チェック（QUEUED / DISPATCHING / DISPATCHED / RUNNING / CANCELLED の合計）
- ジョブ ID 発行
- 内部 DB へのジョブ登録（QUEUED 状態で保存）

### 5.3 Dispatcher 機能

- 定期的に DB をスキャンして QUEUED ジョブを取得
- dispatch budget の計算
- namespace 間の公平なスケジューリング（各 namespace の最古の QUEUED ジョブを優先）
- Kubernetes Job 生成
- Job 作成成功・失敗時の DB 状態更新
- K8s 一時障害時の遅延再試行（`retry_after` タイムスタンプで管理）
- 起動時の DISPATCHING 状態リセット

### 5.4 Kubernetes 実行機能

- submit 時に取得した image（`JUPYTER_IMAGE`）で Job を作成
- PVC を `/home/jovyan` に mount
- `workingDir` に submit 時の cwd を設定
- `env` に submit 時の環境変数を注入
- command を `/bin/bash -lc "<saved command>"` で実行
- ログを PVC 上に tee で書き出し
- Kueue queue ラベルの付与

### 5.5 監視 / 状態同期機能

- Job / Pod 状態監視
- DB 状態更新
- 完了 / 失敗判定
- orphan Job 検出
- cancel 反映
- retry 可能なジョブの管理

## 6. 必要な機能を実装する方針

### 6.1 全体方針

DB スキャン型 Dispatcher + Kueue + Kubernetes Job の構成を採用する。  
Argo Workflows は今回は採用しない。理由は以下の通り。

- 目的は workflow engine ではなく job queue system の構築である
- Argo は queued workflow を持てるが、Kubernetes CR を大量に作る点は変わらない

### 6.2 Dispatcher の実装方針

Dispatcher は PostgreSQL を定期的にスキャンして QUEUED ジョブを選択し、Kubernetes Job を作成する。
RabbitMQ は使用しない。

採用理由は以下の通り。

- 全ユーザーのジョブを常に俯瞰してスケジューリングできる（Slurm と同様の方式）
- budget 不足のユーザーのジョブが他ユーザーをブロックしない
- 各ユーザーの投入順（`created_at` 昇順）を保証したまま公平にスケジューリングできる
- DLQ・ack/nack・prefetch_count などの複雑な MQ 設定が不要になる
- K8s エラー時の再試行も DB の `retry_after` タイムスタンプで管理できる
- 想定規模（20ユーザー・数千件）では DB ポーリングの負荷は問題ない

### 6.3 状態管理の実装方針

ジョブ状態の正本は **PostgreSQL** に保存する。

理由:

- `list/status/cancel/logs` を実装しやすい
- dispatch budget 判定に DB 状態を使える
- 再起動時の再整合がしやすい

### 6.4 実行制御の実装方針

Dispatcher が DB をスキャンして Job を materialize する。

- PostgreSQL: 全ジョブ状態の正本・スケジューリングの判断基盤
- Kubernetes Job: 実行単位
- Kueue: 実行 admission 制御

### 6.5 ジョブ投入コンテキストの再現方針

submit 時に取得した以下を Job Pod に反映する。

- `cwd` → Kubernetes container `workingDir`
- `env` → Kubernetes container `env`（`PATH` / `VIRTUAL_ENV` を含む全 export 済み環境変数）
- `command` → `bash -lc "<command>"`

### 6.6 ログ取得方針

Job Pod のコマンドを tee でラップし、stdout / stderr を PVC 上に保存する。

- 保存先：`/home/jovyan/.cjob/logs/<job_id>/stdout.log` および `stderr.log`
- CLI は User Pod 内から PVC 上のファイルを直接読む
- リアルタイム追跡は CLI が tail -f 相当の処理を行う
- ログの削除は `cjob delete`（個別ジョブの削除時）および `cjob reset`（全件リセット時）のいずれかで行う

コンテナ内では stdout がパイプになるため tee がフルバッファリングモードで動作し、リアルタイム追跡の遅延が発生する。これを防ぐため以下の対策を講じる。

- tee に `stdbuf -oL` を前置してラインバッファリングに切り替える
- Job Pod の env に `PYTHONUNBUFFERED=1` を設定し Python の stdout バッファリングを無効化する

## 7. システム構成

### 7.1 論理構成

```text
User Pod (namespace: user-alice)
  └─ cjob CLI
       └─ HTTP + ServiceAccount JWT
            └─ Submit API (namespace: cjob-system)
                 └─ PostgreSQL（QUEUED 状態で登録）

Dispatcher (namespace: cjob-system)
  ├─ PostgreSQL（QUEUED ジョブをスキャン）
  └─ Kubernetes API
       └─ Job + Kueue LocalQueue (namespace: user-alice)

Watcher / Reconciler (namespace: cjob-system)
  ├─ Kubernetes API
  └─ PostgreSQL

Kubernetes Job Pod (namespace: user-alice)
  ├─ image = JUPYTER_IMAGE（User Pod と同一）
  ├─ PVC mounted at /home/jovyan
  ├─ workingDir = cwd
  ├─ env = submit-time env
  └─ stdout/stderr → /home/jovyan/.cjob/logs/<job_id>/
```

### 7.2 namespace 構成

```text
cjob-system      : Submit API / Dispatcher / Watcher / PostgreSQL
user-<username>    : User Pod / Job Pod / LocalQueue / ResourceQuota / PVC
```

### 7.3 主要コンポーネント

| コンポーネント | 種類 | Replica | namespace |
|---|---|---|---|
| Submit API | Deployment | 2以上推奨 | cjob-system |
| Dispatcher | Deployment | 1 | cjob-system |
| Watcher / Reconciler | Deployment | 1 | cjob-system |
| PostgreSQL | StatefulSet | 1 | cjob-system |
| Kubernetes Job | Job | - | user-\<username\> |

Dispatcher と Watcher は Replica 複数にすると二重 dispatch・二重更新が発生するため、1 固定とする。
Submit API は stateless（状態の正本は PostgreSQL・認証は K8s TokenReview に委譲・job_id 採番は DB でアトミック）であるため、Replica を増やしても安全である。Replica 2 以上を推奨する。

## 8. Dispatcher スケジューリング設計

### 8.1 スケジューリング方針

Dispatcher は PostgreSQL を定期的にスキャンし、以下の基準で dispatch するジョブを選択する。

1. **budget に余裕のある namespace のみ対象とする**（DISPATCHING + DISPATCHED + RUNNING < dispatch_limit）
2. **対象 namespace の中から各 namespace 最古の QUEUED ジョブを1件ずつ取得する**（`created_at` 昇順）
3. **各 namespace を Round-robin で処理する**

この方式により：
- budget を使い切ったユーザーのジョブが他ユーザーをブロックしない
- 同一ユーザーの投入順（`created_at` 昇順）は常に保証される
- 複数ユーザーが同時に QUEUED 状態でも公平に処理される

### 8.2 DB スキャンのクエリ方針

```sql
-- 各 namespace の最古の QUEUED ジョブを取得（budget に余裕がある namespace のみ）
SELECT DISTINCT ON (namespace) *
FROM jobs
WHERE status = 'QUEUED'
  AND (retry_after IS NULL OR retry_after <= NOW())
  AND namespace NOT IN (
    -- dispatch_limit に達した namespace を除外
    SELECT namespace FROM jobs
    WHERE status IN ('DISPATCHING', 'DISPATCHED', 'RUNNING')
    GROUP BY namespace HAVING COUNT(*) >= :dispatch_limit
  )
ORDER BY namespace, created_at ASC;
```

このクエリは `idx_jobs_namespace_status` インデックスにより効率化される。

### 8.3 再試行の管理

K8s API 一時障害時の再試行は `jobs.retry_after` タイムスタンプで管理する。
RabbitMQ の DLQ・TTL は不要。

```sql
-- 一時障害時: retry_after を設定して QUEUED に戻す
-- AND status = 'DISPATCHING' により CANCELLED を上書きしない
UPDATE jobs
SET retry_count = retry_count + 1,
    retry_after = NOW() + INTERVAL '30 seconds',  -- DISPATCH_RETRY_INTERVAL_SEC 秒後
    status = 'QUEUED'
WHERE namespace = :namespace
  AND job_id    = :job_id
  AND status    = 'DISPATCHING';   -- CANCELLED を上書きしない
```

`retry_after IS NULL OR retry_after <= NOW()` の条件で次回スキャン時に自動的に再試行される。

## 9. PostgreSQL 設計

### 9.1 `jobs` テーブル

job_id はユーザー（namespace）ごとに 1 から始まる連番とする。
グローバルな一意性は `(namespace, job_id)` の複合主キーで保証する。

```sql
CREATE TABLE jobs (
    job_id        INTEGER NOT NULL,
    "user"        TEXT NOT NULL,
    namespace     TEXT NOT NULL,
    command       TEXT NOT NULL,
    cwd           TEXT NOT NULL,
    env_json      JSONB NOT NULL DEFAULT '{}',
    cpu           TEXT NOT NULL,
    memory        TEXT NOT NULL,
    gpu           INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    retry_after   TIMESTAMPTZ,              -- K8s 一時障害時の再試行解禁時刻（NULL = 即時対象）
    k8s_job_name  TEXT,
    log_dir       TEXT,          -- /home/jovyan/.cjob/logs/<job_id>
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    last_error    TEXT,
    PRIMARY KEY (namespace, job_id)
);

-- k8s_job_name による高速検索用インデックス（orphan Job 検出・API レスポンス等で使用）
-- ※ Watcher のジョブ特定には cjob.io/job-id ラベルを使用する（k8s_job_name 照合は使用しない）
CREATE INDEX idx_jobs_k8s_job_name ON jobs (k8s_job_name);

-- Dispatcher の dispatch budget 計算を効率化するためのインデックス
CREATE INDEX idx_jobs_namespace_status ON jobs (namespace, status);
```

### 9.2 `user_job_counters` テーブル

ユーザーごとの job_id 採番カウンタ。reset 時に 1 に戻す。

```sql
CREATE TABLE user_job_counters (
    namespace   TEXT PRIMARY KEY,
    next_id     INTEGER NOT NULL DEFAULT 1
);
```

採番は Submit API がアトミックに行う。

```sql
-- 採番クエリ（競合防止のため RETURNING で採番と increment を同時に行う）
INSERT INTO user_job_counters (namespace, next_id)
VALUES (:namespace, 2)
ON CONFLICT (namespace) DO UPDATE
    SET next_id = user_job_counters.next_id + 1
RETURNING next_id - 1;   -- 発行された job_id
```

### 9.3 `job_events` テーブル

```sql
CREATE TABLE job_events (
    id           BIGSERIAL PRIMARY KEY,
    namespace    TEXT NOT NULL,
    job_id       INTEGER NOT NULL,
    event_type   TEXT NOT NULL,
    payload_json JSONB NOT NULL DEFAULT '{}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (namespace, job_id) REFERENCES jobs(namespace, job_id)
        ON DELETE CASCADE   -- jobs レコード削除時に job_events も連動削除
);
```

### 9.4 状態遷移

```text
QUEUED
  ├─ CANCELLED（ユーザーがキャンセル → Dispatcher が次回スキャン時にスキップ）
  └─ DISPATCHING（Dispatcher が DB スキャンで選択し DISPATCHING に更新した時点）
       ├─ CANCELLED（ユーザーがキャンセル → CAS 前ならスキップ、CAS 後なら Watcher が K8s Job 削除）
       ├─ DISPATCHED（Kubernetes Job 作成成功）
       │    ├─ CANCELLED（ユーザーがキャンセル → Watcher が K8s Job を削除）
       │    └─ RUNNING（Watcher が Pod 実行中を検知）
       │         ├─ SUCCEEDED
       │         ├─ FAILED
       │         └─ CANCELLED（ユーザーがキャンセル → Watcher が K8s Job を削除）
       ├─ QUEUED（再試行時：Dispatcher 再起動・K8s 一時障害後の retry_after 差し戻し）
       └─ FAILED（バリデーションエラー・最大 retry 超過）
CANCELLED（QUEUED / DISPATCHING / DISPATCHED / RUNNING の任意タイミングでユーザーがキャンセル）
CANCELLED / SUCCEEDED / FAILED
  └─ DELETING（POST /v1/reset 受付後、これら3状態から一括で遷移する・Watcher による K8s Job 削除と DB クリーンアップ待ち）
       └─ （削除完了後、Watcher が DB レコードを削除・カウンターをリセット）
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
  tolerations:
    - key: "role"
      operator: "Equal"
      value: "computing"
      effect: "NoSchedule"
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
            - name: memory
              nominalQuota: "1000Gi"
  queueingStrategy: BestEffortFIFO
  preemption:
    withinClusterQueue: Never   # 実行中ジョブの強制終了を禁止
```

`BestEffortFIFO` を採用する理由：空きリソースがあれば他ユーザーの idle quota を利用できる（1ユーザーが全コアを使える）ため、かつ `StrictFIFO` では1ユーザーの大量投入が全体を止める可能性があるため。単一 ClusterQueue 内でのユーザー間リソース共有は `cohort` ではなくこの `queueingStrategy` が担う。

`cohort` を設定しない理由：`cohort` は複数 ClusterQueue 間のリソース共有に使う仕組みであり、本設計の単一 ClusterQueue 構成では意味を持たないため削除する。将来 GPU 専用キューなど複数 ClusterQueue 構成に拡張する際に追加すること。

preemption を禁止する理由：研究計算ではジョブが途中で強制終了されると結果が失われるケースが多いため。

以上の設定により：`BestEffortFIFO` により空きリソースは他ユーザーが利用できる。`preemption.withinClusterQueue: Never` により実行中のジョブは強制終了されない。

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

空きリソースがあれば1ユーザーが全コアを使える方針とし、Kueue に公平性の調整を委ねる。
ResourceQuota はリソースを均等分配するためではなく、バグ等による意図しない無制限消費を防ぐための安全網として機能する。

設定根拠：
- CPU / memory：クラスタ総量と同値に設定し、Kueue の admission 制御に任せる
- Job 数：dispatch_limit(256) と `ttlSecondsAfterFinished`(86400秒=24時間) を考慮して設定する。SUCCEEDED/FAILED の K8s Job は Watcher が明示的に削除せず TTL 経過まで残るため、実行中ジョブ(最大256) と TTL ウィンドウ内の完了済みジョブの合計が ResourceQuota を超えないよう dispatch_limit の2倍以上に設定 → 600

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: cjob-quota
  namespace: user-alice
spec:
  hard:
    count/jobs.batch: "600"
    requests.cpu: "256"
    requests.memory: "1000Gi"
    limits.cpu: "256"
    limits.memory: "1000Gi"
```

### 10.5 Kubernetes Job テンプレート

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  namespace: user-alice
  name: cjob-alice-1    # cjob-<username>-<job_id> 形式
  labels:
    kueue.x-k8s.io/queue-name: default   # Dispatcher が KUEUE_LOCAL_QUEUE_NAME の値を動的に設定
    cjob.io/job-id: "1"          # job_id（Dispatcher が動的に設定）
    cjob.io/namespace: user-alice  # namespace（Dispatcher が動的に設定）
spec:
  ttlSecondsAfterFinished: 86400    # 完了後 24時間で Job / Pod を削除
  template:
    spec:
      restartPolicy: Never
      tolerations:
        - key: "role"
          operator: "Equal"
          value: "computing"
          effect: "NoSchedule"
      containers:
        - name: worker
          image: yusekiya/stg-jupyter:2.1.0   # Dispatcher が DB から取得した image を動的に設定
          workingDir: /home/jovyan/project-a/exp1
          command: ["/bin/bash", "-lc"]
          args:
            - |
              LOG_DIR=/home/jovyan/.cjob/logs/1
              mkdir -p "${LOG_DIR}"
              exec > >(stdbuf -oL tee "${LOG_DIR}/stdout.log") \
                   2> >(stdbuf -oL tee "${LOG_DIR}/stderr.log" >&2)
              python main.py --alpha 0.1 --beta 16
          env:
            - name: PYTHONUNBUFFERED
              value: "1"
            - name: OMP_NUM_THREADS
              value: "4"
            - name: PYTHONPATH
              value: "/home/jovyan/project-a"
            - name: VIRTUAL_ENV
              value: "/home/jovyan/myenv"
            - name: PATH
              value: "/home/jovyan/myenv/bin:/usr/local/bin:/usr/bin"
          volumeMounts:
            - name: workspace
              mountPath: /home/jovyan
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
            claimName: alice   # Dispatcher が DB から取得した user を動的に埋め込む
```

## 11. API 設計

CLI はこの API を呼ぶ薄いクライアントとして実装する。  
全エンドポイントで ServiceAccount JWT による認証・認可を行う（詳細は auth_policy.md 参照）。

### 11.0 共通エラーレスポンス仕様

全エンドポイントで共通して発生しうるエラーを以下に定義する。

| HTTP ステータス | 発生条件 | レスポンスボディ例 |
|---|---|---|
| 401 | JWT が無効・期限切れ・存在しない | `{ "detail": "Unauthorized" }` |
| 404 | 存在しない job_id、または他ユーザーの job_id | `{ "detail": "Job not found" }` |
| 409 | リセット処理中（`DELETING` ジョブが存在する namespace への投入） | `{ "detail": "リセット処理中のためジョブを投入できません。しばらく待ってから再試行してください" }` |
| 503 | DB 書き込み失敗など内部サービス一時不可 | `{ "detail": "Service temporarily unavailable" }` |

**404 の方針**：他ユーザーのジョブへのアクセスも 404 を返す。ジョブの存在自体を隠すことで情報漏洩を防ぐ。

**401 の方針**：TokenReview が失敗した場合（JWT 無効・期限切れ）に返す。レスポンスボディは固定文字列とし、詳細なエラー原因は含めない。

**レート制限の方針**：Submit API は各リクエストで K8s TokenReview API を呼ぶため、大量リクエストは K8s API サーバへの負荷につながりうる。ただし Submit API 自身の CPU/memory limit（500m / 512Mi）が事実上のスループット上限として機能するため、想定規模（20ユーザー）においては明示的なレート制限は不要と判断する。ユーザー数や利用規模が拡大する場合は `slowapi` 等による namespace ごとのレート制限を検討すること。

### 11.1 POST /v1/jobs

ジョブを1件投入する。

#### request

```json
{
  "command": "python main.py --alpha 0.1 --beta 16",
  "image": "yusekiya/stg-jupyter:2.1.0",
  "cwd": "/home/jovyan/project-a/exp1",
  "env": {
    "OMP_NUM_THREADS": "4",
    "PYTHONPATH": "/home/jovyan/project-a"
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
  "job_id": 1,
  "status": "QUEUED"
}
```

#### バリデーション

`resources.gpu > 0` の場合は 400 を返す。GPU 対応は初期スコープ外（§18 参照）であり、
将来 GPU 対応を追加する際にこのバリデーションを外す。

```json
{ "detail": "GPU ジョブは現在サポートされていません" }
```

`command` が空文字の場合は 400 を返す。

```json
{ "detail": "command は空にできません" }
```

namespace のジョブ総数（QUEUED / DISPATCHING / DISPATCHED / RUNNING / CANCELLED の合計）が
`MAX_QUEUED_JOBS_PER_NAMESPACE`（デフォルト 2000）に達している場合は 429 を返す。
CANCELLED ジョブを含めることで、cancel → 再投入の無制限サイクルによる DB 肥大化を防ぐ。
上限に達した場合は `cjob delete` で CANCELLED ジョブを削除してから再投入すること。

```json
{ "detail": "投入可能なジョブ数の上限（2000件）に達しています" }
```

namespace に `DELETING` 状態のジョブが1件でも存在する場合は 409 を返す（リセット処理中）。

```json
{ "detail": "リセット処理中のためジョブを投入できません。しばらく待ってから再試行してください" }
```

### 11.2 GET /v1/jobs

ジョブ一覧を取得する。JWT の namespace に属するジョブのみ返す。

#### クエリパラメータ

| パラメータ | 型 | 省略時の挙動 |
|---|---|---|
| `status` | 文字列（任意） | 全ステータスを返す |
| `limit` | 整数（任意） | 全件返す |

```
GET /v1/jobs
GET /v1/jobs?status=RUNNING
GET /v1/jobs?status=FAILED&limit=10
```

#### response

```json
{
  "jobs": [
    {
      "job_id": 1,
      "status": "RUNNING",
      "command": "python main.py --alpha 0.1 --beta 16",
      "created_at": "2026-03-23T12:34:56Z"
    }
  ]
}
```

### 11.3 GET /v1/jobs/{job_id}

個別ジョブの詳細を取得する。

#### response

```json
{
  "job_id": 1,
  "status": "SUCCEEDED",
  "namespace": "user-alice",
  "command": "python main.py --alpha 0.1 --beta 16",
  "cwd": "/home/jovyan/project-a/exp1",
  "k8s_job_name": "cjob-alice-1",
  "log_dir": "/home/jovyan/.cjob/logs/1",
  "created_at": "2026-03-23T12:34:56Z",
  "dispatched_at": "2026-03-23T12:35:02Z",
  "finished_at": "2026-03-23T12:37:10Z"
}
```

#### エラーレスポンス

存在しない job_id または他ユーザーの job_id の場合は 404 を返す。

```json
{ "detail": "Job not found" }
```

### 11.4 POST /v1/jobs/{job_id}/cancel

ジョブをキャンセルする。

| 状態 | API の処理 |
|---|---|
| `QUEUED` | DB を `CANCELLED` に更新する。Dispatcher が次回スキャン時に `CANCELLED` ならスキップする |
| `DISPATCHING` | DB を `CANCELLED` に更新する。CAS 更新の前にキャンセルが行われた場合は Dispatcher がスキップする。CAS 更新の後にキャンセルが行われた場合は K8s Job が作成されるが、Watcher が定期監視時に `CANCELLED` ジョブの K8s Job を削除する（`DISPATCHED` / `RUNNING` と同じ経路） |
| `DISPATCHED` / `RUNNING` | DB を `CANCELLED` に更新する。Watcher が定期監視時に `CANCELLED` ジョブの K8s Job を削除する |
| `SUCCEEDED` / `FAILED` / `CANCELLED` | 変更不要。`skipped` として返す |
| `DELETING` | reset 処理中のため変更不要。`skipped` として返す |

K8s Job 削除後、Watcher は DB の status が `CANCELLED` であることを確認した上で状態を維持する（`FAILED` に遷移させない）。

#### response

```json
{
  "job_id": 1,
  "status": "CANCELLED"
}
```

#### エラーレスポンス

存在しない job_id または他ユーザーの job_id の場合は 404 を返す。

```json
{ "detail": "Job not found" }
```

### 11.5 POST /v1/jobs/cancel

複数ジョブを一括キャンセルする。範囲指定・個別複数指定はCLI側で展開してから送る。

#### request

```json
{
  "job_ids": [1, 2, 3, 4, 5]
}
```

#### response

```json
{
  "cancelled":  [1, 2, 3],
  "skipped":    [4, 5],
  "not_found":  []
}
```

`skipped` は対象ジョブがすでに SUCCEEDED / FAILED / CANCELLED / DELETING の場合。

### 11.6 POST /v1/jobs/delete

完了済みジョブを削除する。範囲指定・個別複数指定は CLI 側で展開してから送る。

CANCELLED / SUCCEEDED / FAILED 状態のジョブのみ削除対象とする。
QUEUED / DISPATCHING / DISPATCHED / RUNNING 状態のジョブは削除せず `skipped` として返す。
DELETING 状態のジョブは Watcher によるリセットクリーンアップが進行中のため削除せず `skipped` として返す。

`job_ids` を省略した場合（`--all` 相当）は namespace 内の全完了済みジョブを削除対象とする。
カウンタのリセットは行わない。

#### request（個別指定）

```json
{
  "job_ids": [1, 2, 3]
}
```

#### request（全件削除）

```json
{}
```

#### response

```json
{
  "deleted":   [1, 2],
  "skipped":   [
    { "job_id": 3, "reason": "running" },
    { "job_id": 4, "reason": "deleting" }
  ],
  "not_found": []
}
```

`skipped` は対象ジョブが QUEUED / DISPATCHING / DISPATCHED / RUNNING / DELETING の場合。
`reason` の値は `"running"`（QUEUED / DISPATCHING / DISPATCHED / RUNNING）と `"deleting"`（DELETING）の2種類。
CLI は `reason` に基づいてメッセージを分岐する。QUEUED / DISPATCHING / DISPATCHED / RUNNING の場合は先に `cjob cancel` するよう促し、DELETING の場合はリセット処理中である旨を表示する（§12.8 参照）。

**§11.5（cancel）との設計上の違い：**
§11.5 の `skipped` は「すでに終了済み・処理済み」という単一の意味しか持たないため job_id のフラットなリストで十分である。一方 §11.6 の `skipped` は「実行中（cancel を促すべき）」と「DELETING（何もできない）」で CLI が取るべきアクションが根本的に異なる。また、CLI が事前に `GET /v1/jobs/{job_id}` で状態を確認してから分岐する方式はレース条件を生じさせるため採用しない。`reason` をレスポンスに含めることで、スキップ判定と理由取得を原子的に行える。

### 11.7 POST /v1/reset

ユーザーの全ジョブ履歴をリセットし、job_id の採番を 1 に戻す。

リセット可能条件：全ジョブが CANCELLED / SUCCEEDED / FAILED のいずれかであること。
以下のいずれかに該当する場合は 409 を返す。

- QUEUED / DISPATCHING / DISPATCHED / RUNNING のジョブが1件でも存在する（未完了ジョブあり）
- DELETING のジョブが1件でも存在する（前回の reset 処理がまだ完了していない）

条件を満たした場合、Submit API は全ジョブのステータスを `DELETING` に変更して即座に返す。
実際の K8s Job 削除・DB レコード削除・カウンターリセットは Watcher が非同期で実行する。
そのため、レスポンスが返った時点ではリセットはまだ完了していない。

job_id カウンターのリセット（`next_id = 1`）は Watcher が全 `DELETING` レコードの処理を完了した後に行う。
これにより reset 完了前に新規ジョブを投入しても job_id=1 は発行されず、K8s Job 名の衝突が起きない。

#### response（成功時・202 Accepted）

```json
{
  "status": "accepted"
}
```

#### response（実行中ジョブあり・409）

```json
{
  "message": "完了していないジョブがあるためリセットできません",
  "blocking_job_ids": [3, 7, 12]
}
```

#### response（リセット処理進行中・409）

```json
{
  "message": "リセット処理が進行中のため再実行できません。しばらく待ってから再試行してください"
}
```

## 12. CLI 設計

### 12.1 基本コマンド

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

### 12.2 `cjob add` の動作

1. `pwd` を取得する
2. export 済み環境変数を収集する（`PATH` / `VIRTUAL_ENV` を含む）
3. `JUPYTER_IMAGE` 環境変数からコンテナイメージ名を取得する
4. `--` 以降の argv を shell-safe に連結して command を生成する
5. ServiceAccount JWT と namespace を固定パスから読み取る
6. API にジョブ投入を行う（`image` フィールドを含む）
7. `job_id` を表示する

### 12.3 `cjob logs` の動作

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

#### QUEUED / DISPATCHING / DISPATCHED 中の待機フィードバック

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

#### `--follow` の終了条件

`--follow` モードは Ctrl-C によりユーザーが明示的に終了する。ジョブが `SUCCEEDED` / `FAILED` / `CANCELLED` に遷移しても自動終了しない。

ただし `--follow` 指定なし（通常の `cjob logs`）でジョブがすでに終了状態の場合は、ファイルを全量表示して終了する。

```
$ cjob logs --follow 3
<ログ出力中>
^C      ← ユーザーが Ctrl-C で終了
```

### 12.4 `cjob list` の動作

`GET /v1/jobs` を呼び出し、結果を表形式で表示する。

```
$ cjob list
JOB_ID  STATUS      COMMAND                                    CREATED
1       SUCCEEDED   python main.py --alpha 0.1 --beta 16       2026-03-23 12:34
2       RUNNING     python main.py --alpha 0.2 --beta 16       2026-03-23 12:35
3       QUEUED      python main.py --alpha 0.5 --beta 16       2026-03-23 12:35
```

オプション：

- `--status <status>`：指定したステータスのジョブのみ表示（例: `--status RUNNING`）
- `--limit <n>`：表示件数を n 件に制限する。省略時は全件表示

```bash
cjob list                    # 全件表示
cjob list --status RUNNING   # 実行中のみ表示
cjob list --status FAILED    # 失敗したもののみ表示
cjob list --limit 10         # 最新 10 件のみ表示
```

command は長い場合に末尾を省略して表示する（例: 40文字で切り捨て）。

### 12.5 `cjob status` の動作

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

### 12.6 CLI の設定

Submit API のエンドポイントは環境変数 `CJOB_API_URL` から読む。未設定時はデフォルト値を使用する。

```
# ※ CLI の実装は Rust（reqwest クレート等）で行う。以下は概念説明のための擬似コードである。

SUBMIT_API_URL = env("CJOB_API_URL")
              ?? "http://submit-api.cjob-system.svc.cluster.local:8080"
```

### 12.7 `cjob cancel` の動作

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

### 12.8 `cjob delete` の動作

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

### 12.9 `cjob reset` の動作

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

## 13. Dispatcher 設計

### 13.1 役割

Dispatcher は PostgreSQL をスキャンして QUEUED ジョブを選択し、Kubernetes Job を作成する。

- DB を定期スキャンして dispatch 対象ジョブを選択する
- namespace 間の公平なスケジューリングを行う
- dispatch budget を確認して K8s Job を作成する
- 成功・失敗時に DB 状態を更新する
- 起動時の DISPATCHING 状態リセット

Dispatcher のメインループは各スキャンサイクル完了時に `/tmp/liveness` ファイルをタッチする。Kubernetes の Liveness probe がこのファイルの最終更新時刻を確認し、ループ停止を検知して再起動できるようにする（deployment.md §13.4 参照）。

```text
dispatch_budget = namespace_dispatch_limit - active_jobs_in_db(namespace)

namespace_dispatch_limit = 256（ConfigMap: DISPATCH_BUDGET_PER_NAMESPACE で設定）

active_jobs_in_db(namespace) は PostgreSQL から取得する。
K8s API は参照しない。

対象ステータス:
  - DISPATCHING（Dispatcher が処理中）
  - DISPATCHED（K8s Job 作成済み・Kueue 待ち）
  - RUNNING（Pod 実行中）
```

**DB ベースを採用する理由：**

- Dispatcher が budget 計算のたびに K8s API を叩くと、K8s API の障害が Dispatcher 全体に波及するリスクがある
- Dispatcher 自身が DISPATCHING に更新してから Job を作るため、自分が投入したジョブは必ず DB に反映される
- Watcher の同期遅延により DB の状態が実態と数件ズレる場合があるが、研究計算の実行時間（数分〜数時間）に対して数秒〜10秒のズレは実用上無視できる
- ズレの方向は常に budget の過小評価（控えめに投入）であり、過大評価（投入しすぎ）にはならない

DB クエリは `idx_jobs_namespace_status` インデックスにより効率化される。

### 13.2 再試行ポリシー

失敗シナリオごとに対処を分ける。

| シナリオ | 対処 | 再試行間隔 | 上限 |
|---|---|---|---|
| K8s API 一時障害 | `retry_after` を設定して `QUEUED` に戻す | `DISPATCH_RETRY_INTERVAL_SEC` 秒後 | `DISPATCH_MAX_RETRIES` 回 |
| dispatch budget 不足 | 次回スキャンで再評価（自然に再試行） | `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` 秒ごと | なし（budget 回復まで） |
| バリデーションエラー | 即 FAILED | なし | なし |
| 永続的 K8s エラー | 即 FAILED | なし | なし |

#### K8s API 一時障害の処理

```python
# ※ 概念説明のための擬似コードである。

except TemporaryK8sError:
    # 現在の retry_count を取得して上限チェック（アトミック UPDATE の前に判断）
    # これにより FAILED 遷移が先行し、QUEUED を経由しなくなる
    current_count = db.get_retry_count(namespace, job_id)
    if current_count + 1 >= max_retries:
        # AND status='DISPATCHING' 条件により CANCELLED を上書きしない
        updated_rows = db.update_status(
            namespace, job_id, "FAILED",
            error="max retries exceeded", condition_status="DISPATCHING"
        )
        # updated_rows == 0 は cancel API が CANCELLED に更新済みのためスキップ
        return
    # 上限内なら retry_count・retry_after・status をアトミックに更新する（§8.3 参照）
    # AND status='DISPATCHING' 条件により CANCELLED を上書きしない
    updated_rows = db.increment_retry_and_set_queued(
        namespace, job_id,
        retry_after=now + int(os.environ["DISPATCH_RETRY_INTERVAL_SEC"])
    )
    if updated_rows == 0:
        return   # cancel API が CANCELLED に更新済み → スキップ
    db.record_event(namespace, job_id, "RETRY", {"count": current_count + 1})
```

`retry_after <= NOW()` になった時点で次回スキャン時に自動的に再 dispatch 対象となる。

### 13.3 dispatch ループ

```python
# ※ 概念説明のための擬似コードである。

class Dispatcher:
    def __init__(self):
        self.check_interval = int(os.environ["DISPATCH_BUDGET_CHECK_INTERVAL_SEC"])

    def run(self):
        while True:
            candidates = db.fetch_dispatchable_jobs()   # §8.2 のクエリ
            for job in candidates:
                self.dispatch(job)
            time.sleep(self.check_interval)

    def dispatch(self, job):
        # WHERE status='QUEUED' 条件付き UPDATE で CAS（Compare And Swap）
        # スキャン後・UPDATE 前に cancel API が CANCELLED に更新していた場合、
        # WHERE status='QUEUED' にマッチしないため updated_rows=0 となりスキップできる
        updated_rows = db.execute("""
            UPDATE jobs SET status = 'DISPATCHING'
            WHERE namespace = :namespace
              AND job_id    = :job_id
              AND status    = 'QUEUED'
        """, namespace=job.namespace, job_id=job.job_id)

        if updated_rows == 0:
            # cancel API が先に CANCELLED に更新していた → スキップ
            return

        # DISPATCHING への更新が確定したので続行
        try:
            k8s.create_job(job)
            # AND status='DISPATCHING' 条件により CANCELLED を上書きしない
            # updated_rows == 0 の場合は status が CANCELLED のまま維持され、
            # Watcher が次のサイクルで CANCELLED ジョブの K8s Job を削除する（§14.3 Step 5）
            db.update_status(
                job.namespace, job.job_id, "DISPATCHED", condition_status="DISPATCHING"
            )
        except TemporaryK8sError:
            # §13.2 の再試行処理
            ...
        except PermanentK8sError:
            # AND status='DISPATCHING' 条件により CANCELLED を上書きしない
            # updated_rows == 0 は cancel API が CANCELLED に更新済みのためスキップ
            db.update_status(
                job.namespace, job.job_id, "FAILED", condition_status="DISPATCHING"
            )
```

### 13.4 起動時の初期化処理

Dispatcher 再起動時に `DISPATCHING` で止まっているジョブを `QUEUED` に戻す。

```python
def on_startup():
    db.reset_stale_dispatching_jobs()
    # UPDATE jobs SET status = 'QUEUED', retry_after = NULL WHERE status = 'DISPATCHING'
```

## 14. Watcher / Reconciler 設計

### 14.1 役割

Watcher / Reconciler は Kubernetes 側の実行状態を DB に反映する。

- Job 状態の監視
- Pod 状態の監視
- `RUNNING` / `SUCCEEDED` / `FAILED` への遷移
- `CANCELLED` ジョブの K8s Job 削除
- `DELETING` ジョブの K8s Job 削除・DB レコード削除・カウンターリセット
- orphan Job 検出
- DB と Kubernetes のズレ修正

Watcher のメインループは各スキャンサイクル完了時に `/tmp/liveness` ファイルをタッチする。Kubernetes の Liveness probe がこのファイルの最終更新時刻を確認し、ループ停止を検知して再起動できるようにする（deployment.md §13.5 参照）。

Watcher は K8s Job の `cjob.io/namespace` ラベルから直接 namespace を取得するため、`JOB_NAMESPACE_PREFIX` 環境変数を必要としない（Dispatcher は Job 作成時に namespace を `user-<username>` 形式で構築する際に `JOB_NAMESPACE_PREFIX` を使用するが、Watcher は既存のラベルを読み取るのみで構築は行わない）。

### 14.2 必要性

Dispatcher が DB スキャンで Job を作成しても、その後の実行状態（RUNNING / SUCCEEDED / FAILED）は Kubernetes 側でのみ確定する。
Dispatcher だけでは K8s Job の完了・失敗を検知できないため、Watcher が必要である。

### 14.3 最小アルゴリズム

1. Kubernetes Job 一覧を定期監視（または watch API を使用）
2. Job の `status.conditions` を以下のルールで解釈する

   | K8s Job の `status.conditions` | DB status |
   |---|---|
   | `type: Complete, status: True` | `SUCCEEDED` |
   | `type: Failed, status: True` | `FAILED`（Pod の exit code 非0・起動失敗を含む） |
   | 条件なし・Pod が Running 中 | `RUNNING` |

3. `cjob.io/job-id` ラベルと `cjob.io/namespace` ラベルから対応する `job_id` を特定する（`k8s_job_name` による照合は使用しない）
4. DB 状態を更新する。ただし DB の status が `CANCELLED` または `DELETING` のジョブは上書きしない（K8s 側が完了・失敗していても DB の意図的な状態を維持する）
5. DB の status が `CANCELLED` のジョブに対応する K8s Job が存在する場合は削除する（K8s Job 削除後も DB の status は `CANCELLED` のまま維持する）
6. DB の status が `DELETING` のジョブを二フェーズで処理する

   **フェーズ 1（削除要求）：**
   対応する K8s Job が存在する場合は削除する（`propagation_policy="Background"` で Pod も連動削除）

   **フェーズ 2（完了確認とクリーンアップ）：**
   次以降のスキャンサイクルで、namespace 内の全 `DELETING` ジョブについて対応する K8s Job が K8s 上に存在しないことを確認する。全件の K8s Job が消滅していた場合、以下を**単一トランザクション**で実行する。

   1. `jobs` テーブルから該当 namespace の全レコードを削除する（`job_events` は `ON DELETE CASCADE` で連動削除される）
   2. `user_job_counters` の `next_id` を 1 にリセットする

   （トランザクション内で途中クラッシュした場合はすべてロールバックされ、次のサイクルで再実行される）

   （`propagation_policy="Background"` の削除は非同期で完結するため、フェーズ 1 と同一サイクルでフェーズ 2 を実行してはならない）

7. `cjob.io/job-id` ラベルに対応する DB レコードが存在しない K8s Job（orphan Job）は削除する

## 15. 実装に使用するパッケージ / 技術

### 15.1 Python パッケージ

- **FastAPI**: Submit API 実装用
- **SQLAlchemy**: PostgreSQL ORM / DB access
- **psycopg**: PostgreSQL ドライバ
- **kubernetes**: Kubernetes Job 作成 / 状態監視用
- **Pydantic**: API リクエスト / レスポンス定義用

### 15.2 ミドルウェア

- **PostgreSQL**
- **Kubernetes**
- **Kueue**

### 15.3 Rust クレート（cjob CLI）

- **clap**: CLI 引数パース
- **reqwest**: HTTP クライアント（Submit API との通信）
- **tokio**: 非同期ランタイム（`--follow` のリアルタイムログ追跡）
- **serde / serde_json**: JSON シリアライズ・デシリアライズ

## 16. 実装方針の詳細

### 16.1 submit の正本管理

ジョブ投入時は次の順で行う。

1. CLI が `cwd`、`env`、`command`、および `JUPYTER_IMAGE` 環境変数から `image` を集める
2. CLI が ServiceAccount JWT と namespace を固定パスから読み取る
3. API が `job_id` を発行する
4. PostgreSQL に `QUEUED` で保存する（`log_dir` も同時に設定）
5. 成功を返す

### 16.2 Dispatcher の動作アルゴリズム

起動時:

1. `DISPATCHING` 状態のジョブを `QUEUED` に戻す（再起動時の整合）

メインループ（`DISPATCH_BUDGET_CHECK_INTERVAL_SEC` 秒ごとにスキャン）:

1. DB から dispatch 対象ジョブを取得する（§8.2 のクエリ）
   - budget に余裕がある namespace のみ対象
   - `retry_after IS NULL OR retry_after <= NOW()` の条件を満たすジョブのみ
   - 各 namespace 最古の QUEUED ジョブを1件ずつ（`created_at` 昇順）
2. 取得したジョブを順に dispatch する
2.1. `WHERE status='QUEUED'` 条件付き UPDATE で `DISPATCHING` に CAS 更新する
2.5. 更新行数が 0（スキャン後に cancel API が先に `CANCELLED` へ更新済み）ならスキップ
3. Job を作成（`claimName` には job の user を使用）
4. 成功なら `DISPATCHED` に更新（`AND status='DISPATCHING'` 条件付き）
5. 一時障害なら `retry_count` をインクリメントして `retry_after` を設定して `QUEUED` に戻す（`AND status='DISPATCHING'` 条件付き・§8.3 参照）
6. 永続障害・バリデーションエラーなら `FAILED` に更新（`AND status='DISPATCHING'` 条件付き）
7. `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` 秒スリープして次のスキャンへ

### 16.3 Watcher の最小アルゴリズム

1. Kubernetes Job 一覧を監視
2. Job の `status.conditions` を以下のルールで解釈する

   | K8s Job の `status.conditions` | DB status |
   |---|---|
   | `type: Complete, status: True` | `SUCCEEDED` |
   | `type: Failed, status: True` | `FAILED`（Pod の exit code 非0・起動失敗を含む） |
   | 条件なし・Pod が Running 中 | `RUNNING` |

3. `cjob.io/job-id` ラベルと `cjob.io/namespace` ラベルで対応する `job_id` を特定する（`k8s_job_name` による照合は使用しない）
4. DB 状態を更新する。ただし DB の status が `CANCELLED` または `DELETING` のジョブは上書きしない（K8s 側が完了・失敗していても DB の意図的な状態を維持する）
5. DB の status が `CANCELLED` のジョブに対応する K8s Job が存在する場合は削除する（K8s Job 削除後も DB の status は `CANCELLED` のまま維持する）
6. DB の status が `DELETING` のジョブを二フェーズで処理する

   **フェーズ 1（削除要求）：**
   対応する K8s Job が存在する場合は削除する（`propagation_policy="Background"` で Pod も連動削除）

   **フェーズ 2（完了確認とクリーンアップ）：**
   次以降のスキャンサイクルで、namespace 内の全 `DELETING` ジョブについて対応する K8s Job が K8s 上に存在しないことを確認する。全件の K8s Job が消滅していた場合、以下を**単一トランザクション**で実行する。

   1. `jobs` テーブルから該当 namespace の全レコードを削除する（`job_events` は `ON DELETE CASCADE` で連動削除される）
   2. `user_job_counters` の `next_id` を 1 にリセットする

   （トランザクション内で途中クラッシュした場合はすべてロールバックされ、次のサイクルで再実行される）

   （`propagation_policy="Background"` の削除は非同期で完結するため、フェーズ 1 と同一サイクルでフェーズ 2 を実行してはならない）

7. `cjob.io/job-id` ラベルに対応する DB レコードが存在しない K8s Job（orphan Job）は削除する

## 17. 実装手順

以下の順番で実装する。

### Step 1: 基本インフラ準備

- PostgreSQL をデプロイする（StatefulSet + PVC）
- Kueue を導入する
- ResourceFlavor / ClusterQueue を作成する
- namespace 作成スクリプトを整備する（LocalQueue / ResourceQuota / ServiceAccount 含む）

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
- `POST /v1/jobs/cancel`
- `POST /v1/jobs/delete`
- `POST /v1/reset`

併せて PostgreSQL スキーマを作成する。

### Step 4: Dispatcher 実装

- 起動時初期化（DISPATCHING → QUEUED）
- DB スキャン実装（§8.2 のクエリ）
- dispatch budget 計算
- Kubernetes Job 作成（tee ラップコマンド含む）
- 再試行ポリシー実装（`retry_after` タイムスタンプ方式）

### Step 5: Watcher / Reconciler 実装

- Job 状態監視
- Pod 状態監視
- DB 更新
- 失敗理由反映
- DELETING 状態の処理（K8s Job 削除・DB クリーンアップ・カウンターリセット）

### Step 6: ログ取得・削除・リセット実装

- Job Pod のコマンドに tee ラップを追加（Step 4 で実施済み）
- `cjob logs`（完了後表示）
- `cjob logs --follow`（リアルタイム追跡）
- `cjob delete`
- `cjob reset`

### Step 7: 運用機能追加

- metrics
- tracing
- cleanup policy

## 18. 初期実装のスコープ

初期実装では、以下に絞る。

- CPU / memory ジョブのみ
- `cjob add`
- `cjob list`
- `cjob status`
- `cjob cancel`
- `cjob delete`
- `cjob reset`
- `cjob logs`（`--follow` 含む）
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
- **CLI 実装言語**: Rust（シングルバイナリ・GitHub Releases で配布）
- **実行単位**: 1コマンド = 1 Kubernetes Job
- **スケジューリング**: DB スキャン型 Dispatcher（公平スケジューリング・投入順保証）
- **状態管理**: PostgreSQL
- **job_id 採番**: ユーザー（namespace）ごとの連番（1, 2, 3...）
- **K8s Job 名**: `cjob-<username>-<job_id>`（グローバルに一意）
- **実行制御**: Dispatcher + Kueue
- **実行基盤**: Kubernetes Job
- **実行環境**: `JUPYTER_IMAGE` 環境変数で指定された image（User Pod と同一）+ namespace PVC mounted at `/home/jovyan`
- **再現対象**: submit 時の `cwd` / exported env（仮想環境 PATH 含む）/ command
- **ログ保存**: PVC 上の `/home/jovyan/.cjob/logs/<job_id>/`
- **ログ取得**: CLI が PVC を直接読む（API 経由なし）・閲覧のみ・削除は delete / reset が担う
- **キャンセル**: 単体・範囲指定（1-10）・個別複数指定（1,3,5）・組み合わせに対応
- **削除**: `cjob delete` で完了済みジョブを個別削除（実行中ジョブは削除不可・cancel を促す。reset 処理中の DELETING ジョブも削除不可）
- **リセット**: `cjob reset` で全ジョブ履歴・ログを削除し job_id を 1 から採番し直す（全ジョブ完了時のみ実行可能）
- **認証・認可**: ServiceAccount JWT + TokenReview（詳細は auth_policy.md 参照）
- **大量投入対応**: dispatch budget + DB スキャン型スケジューリングにより Job materialization を抑制する。投入上限（`MAX_QUEUED_JOBS_PER_NAMESPACE`）は QUEUED / DISPATCHING / DISPATCHED / RUNNING / CANCELLED の合計でカウントし、cancel → 再投入サイクルによる DB 肥大化を防ぐ

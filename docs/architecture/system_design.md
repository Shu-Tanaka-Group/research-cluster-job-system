# システム設計

## 1. 提供したい機能を実現するために必要な機能の一覧

本システムを実現するためには、次の機能が必要である。

### 1.1 CLI 機能

- `cjob add`
- `cjob sweep`
- `cjob list`
- `cjob status`
- `cjob cancel`
- `cjob hold`
- `cjob release`
- `cjob delete`
- `cjob usage`
- `cjob reset`
- `cjob logs`（`--follow` / `--index` オプション含む）
- `cjob flavor`
- `cjob update`

### 1.2 submit 機能

- 現在の作業ディレクトリ取得
- export 済み環境変数取得（ユーザー設定ファイルの `env.exclude` で指定された変数を除外）
- コンテナイメージ名取得（`CJOB_IMAGE` 環境変数から取得、未設定時は `JUPYTER_IMAGE` にフォールバック）
- コマンド文字列の保存
- ユーザー namespace 解決（ServiceAccount の namespace ファイルから取得）
- namespace ごとのジョブ総数上限チェック（QUEUED / DISPATCHING / DISPATCHED / RUNNING / HELD / CANCELLED の合計）
- ジョブ ID 発行
- 内部 DB へのジョブ登録（QUEUED 状態で保存）

### 1.3 Dispatcher 機能

- 定期的に DB をスキャンして QUEUED ジョブを取得
- dispatch budget の計算
- namespace 間の公平なスケジューリング（各 namespace の最古の QUEUED ジョブを優先）
- Kubernetes Job 生成
- Job 作成成功・失敗時の DB 状態更新
- K8s 一時障害時の遅延再試行（`retry_after` タイムスタンプで管理）
- 起動時の DISPATCHING 状態リセット

### 1.4 Kubernetes 実行機能

- submit 時に取得した image（`CJOB_IMAGE` → `JUPYTER_IMAGE`）で Job を作成
- PVC を `${WORKSPACE_MOUNT_PATH}`（デフォルト `/home/jovyan`）に mount
- `workingDir` に submit 時の cwd を設定
- `env` に submit 時の環境変数を注入
- command を `/bin/bash -lc "<saved command>"` で実行
- ログを PVC 上に tee で書き出し
- Kueue queue ラベルの付与

### 1.5 監視 / 状態同期機能

- Job / Pod 状態監視
- DB 状態更新
- 完了 / 失敗判定
- orphan Job 検出
- cancel 反映
- retry 可能なジョブの管理

## 2. 使用パッケージ / 技術

### 2.1 Python パッケージ

- **FastAPI**: Submit API 実装用
- **SQLAlchemy**: PostgreSQL ORM / DB access
- **psycopg**: PostgreSQL ドライバ
- **kubernetes**: Kubernetes Job 作成 / 状態監視用
- **Pydantic**: API リクエスト / レスポンス定義用

### 2.2 ミドルウェア

- **PostgreSQL**
- **Kubernetes**
- **Kueue**

### 2.3 Rust クレート（cjob CLI）

- **clap**: CLI 引数パース
- **reqwest**: HTTP クライアント（Submit API との通信）
- **tokio**: 非同期ランタイム（`--follow` のリアルタイムログ追跡）
- **serde / serde_json**: JSON シリアライズ・デシリアライズ

## 3. 必要な機能を実装する方針

### 3.1 全体方針

DB スキャン型 Dispatcher + Kueue + Kubernetes Job の構成を採用する。
Argo Workflows は今回は採用しない。理由は以下の通り。

- 目的は workflow engine ではなく job queue system の構築である
- Argo は queued workflow を持てるが、Kubernetes CR を大量に作る点は変わらない

### 3.2 Dispatcher の実装方針

Dispatcher は PostgreSQL を定期的にスキャンして QUEUED ジョブを選択し、Kubernetes Job を作成する。
RabbitMQ は使用しない。

採用理由は以下の通り。

- 全ユーザーのジョブを常に俯瞰してスケジューリングできる（Slurm と同様の方式）
- budget 不足のユーザーのジョブが他ユーザーをブロックしない
- 各ユーザーの投入順（`created_at` 昇順）を保証したまま公平にスケジューリングできる
- DLQ・ack/nack・prefetch_count などの複雑な MQ 設定が不要になる
- K8s エラー時の再試行も DB の `retry_after` タイムスタンプで管理できる
- 想定規模（数十〜百数十ユーザー・数千〜数万件）でも DB ポーリングの負荷は問題ない（[performance.md](performance.md) §6 参照）

### 3.3 状態管理の実装方針

ジョブ状態の正本は **PostgreSQL** に保存する。

理由:

- `list/status/cancel/logs` を実装しやすい
- dispatch budget 判定に DB 状態を使える
- 再起動時の再整合がしやすい

### 3.4 実行制御の実装方針

Dispatcher が DB をスキャンして Job を materialize する。

- PostgreSQL: 全ジョブ状態の正本・スケジューリングの判断基盤
- Kubernetes Job: 実行単位
- Kueue: 実行 admission 制御

### 3.5 ジョブ投入コンテキストの再現方針

submit 時に取得した以下を Job Pod に反映する。

- `cwd` → Kubernetes container `workingDir`
- `env` → Kubernetes container `env`（`PATH` / `VIRTUAL_ENV` を含む export 済み環境変数。ユーザー設定の `env.exclude` で除外された変数は含まない）
- `command` → `bash -lc "<command>"`

### 3.6 ログ取得方針

Job Pod のコマンドを tee でラップし、stdout / stderr を PVC 上に保存する。

- 保存先：`${LOG_BASE_DIR}/<job_id>/stdout.log` および `stderr.log`（`LOG_BASE_DIR` はデフォルト `/home/jovyan/.cjob/logs`）
- CLI は User Pod 内から PVC 上のファイルを直接読む（ログパスは API から取得）
- リアルタイム追跡は CLI が tail -f 相当の処理を行う
- ログの削除は `cjob delete`（個別ジョブの削除時）および `cjob reset`（全件リセット時）のいずれかで行う

リアルタイム追跡の遅延を防ぐため、Job Pod の env に `PYTHONUNBUFFERED=1` を設定し Python の stdout バッファリングを無効化する。他の言語を使用する場合はユーザー側で適宜フラッシュを制御する。

ユーザーコマンド終了後、tee のプロセス置換が書き込みを完了する前に Pod が終了するのを防ぐため、コマンド実行後に `exec >&- 2>&-` で stdout/stderr の fd を閉じ、`wait` で tee プロセスの終了を待つ。

### 3.7 ResourceFlavor による計算リソースの分類

クラスタ内の計算ノードは用途やハードウェア特性に応じて **ResourceFlavor** で分類する。たとえば汎用 CPU ノード、A100 GPU ノード、H100 GPU ノードをそれぞれ異なる flavor として管理することで、ユーザーはジョブの実行先を明示的に選択できる。

#### 設計方針

- **Kueue の ResourceFlavor 機構を活用する**: 各 flavor は Kueue の `ResourceFlavor` オブジェクトに対応し、`nodeLabels` によって対象ノードが決定される。Dispatcher は flavor の `label_selector` を K8s Job の `nodeSelector` として設定し、Kueue がそれにマッチする ResourceFlavor を選択してノードにスケジュールする
- **flavor 定義は設定で管理する**: ConfigMap `RESOURCE_FLAVORS` に JSON 配列として定義する。各 flavor は名前（`name`）、ノード選択用ラベルセレクタ（`label_selector`）、GPU リソース名（`gpu_resource_name`、任意）を持つ。flavor の追加・変更はコード変更なしに設定変更のみで対応できる
- **ユーザーは `--flavor` オプションで指定する**: `cjob add --flavor gpu-a100 --gpu 1 -- python train.py` のように実行先 flavor を指定する。省略時は `DEFAULT_FLAVOR`（デフォルト: `cpu`）が使用される
- **バリデーションは flavor 単位で行う**: Submit API はジョブのリソース要求を指定 flavor 内のノードに限定して検証する。flavor にないリソース（CPU flavor への GPU 要求など）は拒否する
- **GPU リソース名は flavor ごとに定義する**: `nvidia.com/gpu`（NVIDIA）、`amd.com/gpu`（AMD）など、異なるベンダーの GPU リソース名を flavor 定義の `gpu_resource_name` で指定する。Dispatcher は K8s Job マニフェスト生成時にこの値を使用する
- **DRF（Dominant Resource Fairness）はクラスタ全体で計算する**: flavor 間で dispatch 優先度を分離せず、namespace ごとのリソース消費をクラスタ全体の容量に対して正規化する。これにより flavor を跨いだ公平性が維持される

#### flavor 定義の設定例

```json
[
  {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
  {"name": "gpu-a100", "label_selector": "cjob.io/flavor=gpu-a100", "gpu_resource_name": "nvidia.com/gpu"},
  {"name": "gpu-h100", "label_selector": "cjob.io/flavor=gpu-h100", "gpu_resource_name": "nvidia.com/gpu"}
]
```

flavor の詳細設計は [resources.md](resources.md)、Kueue との連携は [kueue.md](kueue.md)、flavor 追加の運用手順は [../deployment.md](../deployment.md) §16.3 を参照。

## 4. システム構成

### 4.1 論理構成

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
  ├─ image = User Pod と同一（CJOB_IMAGE → JUPYTER_IMAGE の順で取得）
  ├─ PVC mounted at ${WORKSPACE_MOUNT_PATH}（デフォルト /home/jovyan）
  ├─ workingDir = cwd
  ├─ env = submit-time env
  └─ stdout/stderr → ${LOG_BASE_DIR}/<job_id>/
```

### 4.2 namespace 構成

```text
cjob-system        : Submit API / Dispatcher / Watcher / PostgreSQL
<user-namespace>   : User Pod / Job Pod / LocalQueue / ResourceQuota / PVC
```

ユーザー namespace は任意の名前を使用できる（例: `user-alice`, `lab-physics`）。
識別はラベル `cjob.io/user-namespace=true` で行い、ユーザー名は namespace のアノテーション `cjob.io/username` から取得する。

### 4.3 主要コンポーネント

| コンポーネント | 種類 | Replica | namespace |
|---|---|---|---|
| Submit API | Deployment | 2以上推奨 | cjob-system |
| Dispatcher | Deployment | 1 | cjob-system |
| Watcher / Reconciler | Deployment | 1 | cjob-system |
| PostgreSQL | StatefulSet | 1 | cjob-system |
| Kubernetes Job | Job | - | \<user-namespace\> |

Dispatcher と Watcher は Replica 複数にすると二重 dispatch・二重更新が発生するため、1 固定とする。
Submit API は stateless（状態の正本は PostgreSQL・認証は K8s TokenReview に委譲・job_id 採番は DB でアトミック）であるため、Replica を増やしても安全である。Replica 2 以上を推奨する。

#### 各コンポーネントの役割

**cjob CLI**
ユーザーが User Pod 内で操作するコマンドラインツール。ジョブの投入・一覧・状態確認・キャンセル・ログ閲覧などを Submit API への HTTP リクエストとして送信する。Rust 製シングルバイナリとして配布し、image には含めない。`cjob update` コマンドで Submit API 経由でセルフアップデートできる。

**Submit API**
CLI からのリクエストを受け付け、ジョブを PostgreSQL に QUEUED 状態で登録する。ServiceAccount JWT を K8s TokenReview API で検証し、操作が自分の namespace のジョブに限定されることを保証する。状態を持たない（stateless）ため Replica を複数にできる。

**PostgreSQL**
全ジョブ状態の正本（Single Source of Truth）。ジョブのメタデータ・状態・実行履歴を管理する。Dispatcher のスケジューリング判断・Submit API のバリデーション・CLI の表示はすべてここを参照する。

**Dispatcher**
PostgreSQL を定期スキャンして QUEUED ジョブを選択し、Kubernetes Job を作成する。dispatch budget と公平スケジューリング（per-namespace Round-robin）を制御する。Replica 1 固定（複数にすると二重 dispatch が発生するため）。

**Watcher / Reconciler**
Kubernetes API を定期監視し、Job / Pod の実行状態を PostgreSQL に反映する。SUCCEEDED / FAILED への遷移検知・CANCELLED ジョブの K8s Job 削除・reset 時の DELETING クリーンアップを担う。Dispatcher と同じく Replica 1 固定。

**Kubernetes Job / Job Pod**
Dispatcher が作成する実行単位。Job Pod はユーザーの投入時の環境（image・cwd・env・command）を再現して実行し、stdout / stderr を PVC 上のログディレクトリに書き出す。User Pod と同一 image を使用する。

**Kueue**
Kubernetes Job の admission 制御を担う。ClusterQueue でクラスタ全体のリソース上限を管理し、BestEffortFIFO により空きリソースをユーザー間で公平に利用できるようにする。preemption は無効化しており実行中ジョブを強制終了しない。

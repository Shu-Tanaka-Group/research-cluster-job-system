# リソース設計

本ドキュメントは計算リソース（CPU・メモリ・GPU）およびジョブ数に関する制限・設定を説明する。パス設定やキュー名などリソースに直接関係しない ConfigMap キーは対象外とし、それぞれの設計書で扱う。

## 1. ResourceQuota

各 user namespace に作成する安全網。

空きリソースがあれば1ユーザーが全コアを使える方針とし、公平性の調整は Kueue の BestEffortFIFO と Dispatcher の DRF スケジューリングに委ねる。
ResourceQuota はリソースを均等分配するためではなく、バグ等による意図しない無制限消費を防ぐための安全網として機能する。

設定根拠：
- CPU / memory：クラスタ総量より少し大きめに設定し、Kueue の admission 制御に任せる。Job Pod（最大 dispatch_limit 分）に加えてユーザーが使用している他の計算リソース（ジョブ投入用Podやデータ解析用Podなど）の分も余裕として含める
- Job 数：dispatch_limit(32) と `ttlSecondsAfterFinished`(300秒=5分) を考慮して設定する。SUCCEEDED/FAILED の K8s Job は Watcher が明示的に削除せず TTL 経過まで残るため、実行中ジョブ(最大32) と TTL ウィンドウ内の完了済みジョブの合計が ResourceQuota を超えないよう余裕を持たせて設定 → 50。TTL を短縮したことで通常運用では quota に到達する可能性は極めて低い。sweep 機能（1 Job で数百〜数千タスクを実行可能）があるため、Job 数の上限を抑えても実質的な計算能力は制限されない
- GPU：GPU ノードの総 GPU 数に合わせて設定する。`"0"` に設定するとそのユーザーは GPU ジョブを実行できない。GPU を使わないユーザーには `"0"` を設定するか、GPU 関連の項目を省略する

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: computing-quota
  namespace: user-alice
spec:
  hard:
    count/jobs.batch: "50"
    requests.cpu: "300"
    requests.memory: "1250Gi"
    limits.cpu: "300"
    limits.memory: "1250Gi"
    requests.nvidia.com/gpu: "4"
    limits.nvidia.com/gpu: "4"
```

## 2. リソース制限まとめ

### ジョブ数に関する制限

| 制限 | 設定箇所 | 値 | 管理主体 | 適用単位 | 制限対象 |
|---|---|---|---|---|---|
| `MAX_QUEUED_JOBS_PER_NAMESPACE` | ConfigMap | 500 | Submit API | ユーザーごと | PostgreSQL の `jobs` テーブルへの登録数（QUEUED / DISPATCHING / DISPATCHED / HELD / CANCELLED の合計）。RUNNING は `DISPATCH_BUDGET_PER_NAMESPACE` で上限が管理されるためカウント対象外 |
| `DISPATCH_BUDGET_PER_NAMESPACE` | ConfigMap | 32 | Dispatcher | ユーザー × flavor ごと | DB 上の active ジョブ数（DISPATCHING + DISPATCHED + RUNNING の合計）を `(namespace, flavor)` 単位で制限する。ある flavor が上限に達しても他の flavor の dispatch は継続される |
| `DISPATCH_BATCH_SIZE` | ConfigMap | 50 | Dispatcher | サイクルごと（全体） | 1回の dispatch サイクルで dispatch するジョブの総数上限。namespace 間でラウンドロビン・DRF 優先で公平に分配される |
| `DISPATCH_FETCH_MULTIPLIER` | ConfigMap | 10 | Dispatcher | サイクルごと（全体） | SQL 候補取得数の倍率。`DISPATCH_BATCH_SIZE × DISPATCH_FETCH_MULTIPLIER` 件を余剰取得し、隙間充填・ResourceQuota フィルタ通過後に `DISPATCH_BATCH_SIZE` 件へ絞り込む。DRF 優先の namespace のジョブがフィルタで全滅しても他 namespace の候補が dispatch されることを保証する |
| `DISPATCH_ROUND_SIZE` | ConfigMap | 1 | Dispatcher | サイクルごと（namespace あたり） | ラウンドロビンと DRF のバランスを制御する。値が小さいとラウンドロビン主導（均等配分）、`DISPATCH_BUDGET_PER_NAMESPACE` と同値にすると DRF 主導（消費量ベースの優先制御）になる。詳細は [dispatcher.md](dispatcher.md) §1.2 調整指針を参照 |
| `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` | ConfigMap | 10 | Dispatcher / Watcher | 全体 | Dispatcher と Watcher のメインループ実行間隔（秒） |
| `DISPATCH_RETRY_INTERVAL_SEC` | ConfigMap | 30 | Dispatcher | ジョブごと | K8s API 一時障害時の再試行待機時間（秒） |
| `DISPATCH_MAX_RETRIES` | ConfigMap | 5 | Dispatcher | ジョブごと | K8s API 一時障害時の最大再試行回数。超過時はジョブを FAILED に遷移させる |
| `TTL_SECONDS_AFTER_FINISHED` | ConfigMap | 300 (5分) | Dispatcher | ジョブごと | K8s Job の `ttlSecondsAfterFinished` に設定する値。完了した K8s Job がこの秒数後に自動削除される |
| `count/jobs.batch` | ResourceQuota | 50 | Kubernetes | ユーザーごと | K8s 上に存在する `batch/v1 Job` オブジェクトの総数。実行中 + TTL 待ち完了済みジョブの合計が対象 |

4つの制限は独立したレイヤーで機能する。

```
cjob add → DB 登録（MAX_QUEUED_JOBS_PER_NAMESPACE: 500件上限）
              ↓
Dispatcher がスキャン → dispatch_budget チェック（DISPATCH_BUDGET_PER_NAMESPACE: flavor ごとに 32件上限）
                      → 候補を余剰取得（DISPATCH_BATCH_SIZE × DISPATCH_FETCH_MULTIPLIER）
                      → 隙間充填・ResourceQuota フィルタ通過後に DISPATCH_BATCH_SIZE 件へ絞り込み（50件/サイクル上限）
              ↓
K8s Job を作成 → count/jobs.batch チェック（50件上限）
```

`count/jobs.batch` を 50 に設定する理由：dispatch_budget の上限で動作していても、SUCCEEDED/FAILED になった K8s Job が TTL（5分）が切れるまで K8s 上に残り続けるため、実行中 + TTL 待ち完了済みジョブの合計を吸収できるよう設定している。TTL 300秒（5分）では長時間〜中程度のジョブで TTL 待ちの蓄積がほとんど発生せず、50 に対して大幅な余裕がある。短時間ジョブが大量に回転して一時的に quota に達した場合は TTL 経過で自然に回復し、Dispatcher の retry により自動復旧する。

**`count/jobs.batch` と flavor-aware budget の関係:** dispatch_budget は `(namespace, flavor)` 単位で 32 件であるため、namespace あたりの理論上の最大 active ジョブ数は `32 × flavor 数`（2 flavor で 64）となり、`count/jobs.batch`（50）を超過する場合がある。Dispatcher は `namespace_resource_quotas` テーブルの `hard_count` / `used_count` を用いて `count/jobs.batch` の残りジョブ数をプレチェックし、超過する dispatch を抑制する（[dispatcher.md](dispatcher.md) §2.5 参照）。

### CPU・メモリに関する制限

| 制限 | 設定箇所 | 値 | 管理主体 | 適用単位 | 制限対象 |
|---|---|---|---|---|---|
| ResourceQuota `requests.cpu` / `limits.cpu` | ResourceQuota（user namespace） | 300 | Kubernetes | ユーザーごと | namespace 内の全 Pod（Job Pod + User Pod）が要求・使用できる CPU の合計上限 |
| ResourceQuota `requests.memory` / `limits.memory` | ResourceQuota（user namespace） | 1250Gi | Kubernetes | ユーザーごと | namespace 内の全 Pod が要求・使用できるメモリの合計上限 |
| ClusterQueue `nominalQuota` CPU | ClusterQueue | 256 | Kueue | クラスタ全体 | Kueue が Job Pod に割り当てるクラスタ全体の CPU 上限。ユーザー間で共有される |
| ClusterQueue `nominalQuota` memory | ClusterQueue | 1000Gi | Kueue | クラスタ全体 | Kueue が Job Pod に割り当てるクラスタ全体のメモリ上限。ユーザー間で共有される |
| `CPU_LIMIT_BUFFER_MULTIPLIER` | ConfigMap | 1.0 | Dispatcher | ジョブごと | CPU limit に適用する乗数。`1.0` で request == limit（デフォルト）。`1.05` 等に設定すると CPU limit のみを request の 1.05 倍にし、システムプロセスによる CFS throttling を軽減する。request は変更しない |

ResourceQuota と ClusterQueue nominalQuota の違い：ResourceQuota は User Pod を含む namespace 内の全 Pod を対象とした上限（バグ等による無制限消費を防ぐ安全網）。ClusterQueue nominalQuota は Kueue が Job Pod の admission を判断するための上限であり、実際の実行スケジューリングを制御する。User Pod は Kueue を経由しないため ClusterQueue の制御対象外である。

### 隙間充填に関する設定

| 設定 | 設定箇所 | 値 | 管理主体 | 適用単位 | 説明 |
|---|---|---|---|---|---|
| `GAP_FILLING_ENABLED` | ConfigMap | true | Dispatcher | 全体 | 隙間充填ロジックの有効/無効。false にすると従来動作 |
| `GAP_FILLING_STALL_THRESHOLD_SEC` | ConfigMap | 300 (5分) | Dispatcher | ジョブごと | DISPATCHED からの経過秒数がこの値を超えたジョブを滞留とみなす |

隙間充填の詳細は [dispatcher.md](dispatcher.md) §2.4 を参照。

### DISPATCHED 滞留ガードに関する設定

| 設定 | 設定箇所 | 値 | 管理主体 | 適用単位 | 説明 |
|---|---|---|---|---|---|
| `WATCHER_DISPATCH_TIMEOUT_SEC` | ConfigMap | 1800 (30分) | Watcher | ジョブごと | DISPATCHED からの経過秒数がこの値を超えても RUNNING に遷移しないジョブを配置不能とみなし、K8s Job を削除して QUEUED に差し戻す。`GAP_FILLING_STALL_THRESHOLD_SEC`（300 秒）より十分に長く設定する |
| `WATCHER_DISPATCH_BACKOFF_MAX_SEC` | ConfigMap | 7200 (2時間) | Watcher | ジョブごと | 差し戻し時に設定する `retry_after` の指数バックオフの上限秒数 |

DISPATCHED 滞留ガードの詳細は [watcher.md](watcher.md) §3 ステップ 10 を参照。

### per-node bin-packing プレチェックに関する設定

| 設定 | 設定箇所 | 値 | 管理主体 | 適用単位 | 説明 |
|---|---|---|---|---|---|
| `NODE_BIN_PACKING_ENABLED` | ConfigMap | true | Dispatcher | 全体 | per-node bin-packing プレチェックの有効/無効。false にするとプレチェックをスキップし、Kueue の flavor 単位 nominalQuota のみで admit する従来動作になる |

per-node bin-packing プレチェックの詳細は [dispatcher.md](dispatcher.md) §2.6 を参照。

### Fair sharing に関する設定

| 設定 | 設定箇所 | 値 | 管理主体 | 適用単位 | 説明 |
|---|---|---|---|---|---|
| `FAIR_SHARE_WINDOW_DAYS` | ConfigMap | 7 | Dispatcher | 全体 | DRF の消費量集計に使用するスライディングウィンドウの日数。直近 N 日分の日別消費量を合計して dominant share を計算する |
| `USAGE_RETENTION_DAYS` | ConfigMap | 7 | Dispatcher | 全体 | `namespace_daily_usage` の保持日数。`FAIR_SHARE_WINDOW_DAYS` と独立しており、DRF 以外の用途で消費量データを参照する場合に長い期間を設定できる |

DRF 正規化に使用するクラスタ全体のリソース容量は、`node_resources` テーブル（[database.md](database.md) §6）から `SUM()` で動的に取得する。従来の `CLUSTER_TOTAL_CPU_MILLICORES` / `CLUSTER_TOTAL_MEMORY_MIB` / `CLUSTER_TOTAL_GPUS` は廃止された。`node_resources` の CPU・memory は DaemonSet Pod の request 分を差し引いた effective allocatable である（[watcher.md](watcher.md) §1.1 参照）。

日別リソース消費量の詳細は [database.md](database.md) §5、namespace の weight は [database.md](database.md) §4、DRF によるスケジューリングの詳細は [dispatcher.md](dispatcher.md) §1.1・§1.2 を参照。

### ResourceFlavor 定義に関する設定

| 設定 | 設定箇所 | 値 | 管理主体 | 適用単位 | 説明 |
|---|---|---|---|---|---|
| `RESOURCE_FLAVORS` | ConfigMap | JSON 配列 | Watcher / Submit API | 全体 | ResourceFlavor の定義リスト。各要素は `name`（flavor 名）、`label_selector`（K8s ノード取得用ラベルセレクタ）、`gpu_resource_name`（GPU リソース名、省略可）、`image`（既定コンテナイメージ、省略可）を持つ。Watcher がノード同期時に使用し、Submit API が flavor バリデーションと image 解決に使用する |
| `DEFAULT_FLAVOR` | ConfigMap | `cpu` | Submit API | 全体 | ユーザーが `--flavor` を省略した場合に使用されるデフォルトの flavor 名。`RESOURCE_FLAVORS` 内のいずれかの flavor 名と一致している必要がある |
| `NODE_RESOURCE_SYNC_INTERVAL_SEC` | ConfigMap | 300 (5分) | Watcher | 全体 | ノードリソース同期間隔（秒）。Watcher のメインループの N サイクルに 1 回実行する |
| `CLUSTER_QUEUE_NAME` | ConfigMap | `cjob-cluster-queue` | Watcher / cjobctl | 全体 | ClusterQueue の名前。Watcher が nominalQuota 同期時に、cjobctl が `cluster` サブコマンド（`show-quota` / `set-quota` / `flavor-usage`）で使用する。ClusterQueue は cluster-scoped であり namespace で分離できないため、同一クラスタに複数の CJob インスタンスを置く場合はここで名前を分ける |
| `RESOURCE_QUOTA_NAME` | ConfigMap | `computing-quota` | Watcher | 全体 | user namespace から読み取る ResourceQuota オブジェクトの名前。Watcher が ResourceQuota 同期時に使用する |
| `RESOURCE_QUOTA_SYNC_INTERVAL_SEC` | ConfigMap | 10 | Watcher | 全体 | ResourceQuota 同期間隔（秒）。Watcher のメインループの N サイクルに 1 回実行する。`NODE_RESOURCE_SYNC_INTERVAL_SEC` とは独立した間隔で動作する |
| `USER_NAMESPACE_LABEL` | ConfigMap | `cjob.io/user-namespace=true` | Watcher | 全体 | ユーザー namespace を識別するラベルセレクタ。Watcher が ResourceQuota 同期時にユーザー namespace の一覧取得に使用する |

#### `RESOURCE_FLAVORS` の設定例

```json
[
  {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
  {"name": "gpu-a100", "label_selector": "cjob.io/flavor=gpu-a100", "gpu_resource_name": "nvidia.com/gpu", "image": "your-registry/cjob-cuda:2.1.0"},
  {"name": "gpu-h100", "label_selector": "cjob.io/flavor=gpu-h100", "gpu_resource_name": "nvidia.com/gpu", "image": "your-registry/cjob-cuda:2.1.0"}
]
```

各フィールドの意味:

| フィールド | 必須 | 説明 |
|---|---|---|
| `name` | 必須 | flavor 名。Kueue ResourceFlavor 名・DB の `jobs.flavor` / `node_resources.flavor` と一致させる |
| `label_selector` | 必須 | K8s ノードの label selector。全 flavor で共通キー `cjob.io/flavor` を使用し、値に flavor 名を設定する。Kueue ResourceFlavor の `nodeLabels` と一致させる |
| `gpu_resource_name` | 任意 | GPU リソースの K8s リソース名（例: `nvidia.com/gpu`、`amd.com/gpu`）。省略時はその flavor を GPU なし flavor として扱い、`gpu > 0` のジョブ投入を拒否する |
| `image` | 任意 | その flavor で実行するジョブの既定コンテナイメージ。省略時は投入元 User Pod のイメージを使用する（[api.md](api.md) §2.2 の image 解決順序を参照）。設定する場合は前提条件（[prerequisites.md](prerequisites.md) §2.1）と Kyverno の許可パターン（[deployment.md](../deployment.md) §14）を満たす必要がある |

flavor の `name` は Kueue ResourceFlavor の `metadata.name` と一致させる。これにより `cjobctl cluster set-quota --flavor <name>` で指定する名前と DB の flavor 値が統一され、変換処理が不要になる。

#### `RESOURCE_FLAVORS` のスキーマ制約

本節の表が `RESOURCE_FLAVORS` スキーマの正本である。`cjobctl config set`（Rust、[cjobctl.md](cjobctl.md) §5.5）とサーバ側の `FlavorDefinition`（Python）はいずれもこの定義に従う。スキーマを変更する場合は本節を先に更新し、両方の実装を同期させる。

| # | 制約 | 検証箇所 |
|---|---|---|
| 1 | トップレベルが JSON 配列であり、1 つ以上の要素を持つこと | `cjobctl config set` / サーバ起動時（配列であること） |
| 2 | 各要素が JSON オブジェクトであること | `cjobctl config set` / サーバ起動時 |
| 3 | `name` / `label_selector` が存在し、文字列かつ空文字でないこと | `cjobctl config set` / サーバ起動時（存在と型のみ） |
| 4 | `gpu_resource_name` を指定する場合、文字列かつ空文字でないこと（`null` は省略と同義） | `cjobctl config set` / サーバ起動時（型のみ） |
| 5 | `image` を指定する場合、文字列かつ空文字でないこと（`null` は省略と同義） | `cjobctl config set` / サーバ起動時（型のみ） |
| 6 | 上表にないフィールドを含まないこと | `cjobctl config set` / サーバ起動時 |
| 7 | `name` が重複していないこと | `cjobctl config set` |
| 8 | `label_selector` が `key=value` 形式であること（`=` はちょうど 1 個、両辺が非空） | `cjobctl config set` |
| 9 | `DEFAULT_FLAVOR` がいずれかの `name` と一致すること | `cjobctl config set`（`RESOURCE_FLAVORS` 設定時・`DEFAULT_FLAVOR` 設定時の両方） |

**未知フィールドの拒否**: サーバ側の `FlavorDefinition` は `extra="forbid"` を設定しており、上表にないフィールドを含む定義では Submit API / Dispatcher / Watcher が起動に失敗する。これは `gpu_resouce_name` のような任意フィールドのタイポが黙って無視され、GPU flavor が GPU 非対応として扱われる事故を防ぐためである。

制約 7・8・9 は起動時に検出されない（pydantic のモデル単体では表現できない）ため、`cjobctl config set` 側での検証が唯一の防御線となる。ConfigMap を `cjobctl` を経由せず `kubectl` で直接編集した場合はこれらの検証を通らない点に注意する。

ノードリソース同期の詳細は [watcher.md](watcher.md) §1.1、DB テーブル定義は [database.md](database.md) §6 を参照。

### sweep に関する制限

| 制限 | 設定箇所 | 値 | 管理主体 | 適用単位 | 制限対象 |
|---|---|---|---|---|---|
| `MAX_SWEEP_COMPLETIONS` | ConfigMap | 1000 | Submit API | sweep ジョブごと | `completions`（タスク数）の上限 |

### 実行時間に関する制限

| 制限 | 設定箇所 | 値 | 管理主体 | 適用単位 | 制限対象 |
|---|---|---|---|---|---|
| `DEFAULT_TIME_LIMIT_SECONDS` | ConfigMap | 86400 (24h) | Submit API | ジョブごと | `time_limit_seconds` 省略時に適用されるデフォルト実行時間上限 |
| `MAX_TIME_LIMIT_SECONDS` | ConfigMap | 604800 (7d) | Submit API | ジョブごと | ユーザーが指定できる `time_limit_seconds` の最大値 |
| 制限時間の強制 | Watcher（reconcile サイクル） | DB の `time_limit_seconds` | Watcher | ジョブごと | ジョブの `started_at`（Watcher が RUNNING を観測した時刻）からの実行時間上限。超過を検知すると Watcher が K8s Job を削除し、FAILED（`time limit exceeded`）に遷移させる（[watcher.md](watcher.md) §3 ステップ 9 参照）。Kueue の admission 待ち時間（suspend 期間）も、admit 後にノードの空き待ちで Pod が Pending のまま滞留した時間も含まれない |


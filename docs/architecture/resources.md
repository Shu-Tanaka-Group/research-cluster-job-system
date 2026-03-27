# リソース設計

## 1. ResourceQuota

各 user namespace に作成する安全網。

空きリソースがあれば1ユーザーが全コアを使える方針とし、公平性の調整は Kueue の BestEffortFIFO と Dispatcher の DRF スケジューリングに委ねる。
ResourceQuota はリソースを均等分配するためではなく、バグ等による意図しない無制限消費を防ぐための安全網として機能する。

設定根拠：
- CPU / memory：クラスタ総量より少し大きめに設定し、Kueue の admission 制御に任せる。Job Pod（最大 dispatch_limit 分）に加えてユーザーが使用している他の計算リソース（ジョブ投入用Podやデータ解析用Podなど）の分も余裕として含める
- Job 数：dispatch_limit(256) と `ttlSecondsAfterFinished`(10800秒=3時間) を考慮して設定する。SUCCEEDED/FAILED の K8s Job は Watcher が明示的に削除せず TTL 経過まで残るため、実行中ジョブ(最大256) と TTL ウィンドウ内の完了済みジョブの合計が ResourceQuota を超えないよう余裕を持たせて dispatch_limit の2倍以上に設定 → 600

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: computing-quota
  namespace: user-alice
spec:
  hard:
    count/jobs.batch: "600"
    requests.cpu: "300"
    requests.memory: "1250Gi"
    limits.cpu: "300"
    limits.memory: "1250Gi"
```

## 2. リソース制限まとめ

### ジョブ数に関する制限

| 制限 | 設定箇所 | 値 | 管理主体 | 適用単位 | 制限対象 |
|---|---|---|---|---|---|
| `MAX_QUEUED_JOBS_PER_NAMESPACE` | ConfigMap | 2000 | Submit API | ユーザーごと | PostgreSQL の `jobs` テーブルへの登録数（QUEUED / DISPATCHING / DISPATCHED / RUNNING / CANCELLED の合計） |
| `DISPATCH_BUDGET_PER_NAMESPACE` | ConfigMap | 256 | Dispatcher | ユーザーごと | DB 上の active ジョブ数（DISPATCHING + DISPATCHED + RUNNING の合計）。上限に達すると Dispatcher が新規 dispatch を停止する |
| `DISPATCH_BATCH_SIZE` | ConfigMap | 50 | Dispatcher | サイクルごと（全体） | 1回の dispatch サイクルで取得するジョブの総数上限。namespace 間でラウンドロビン・DRF 優先で公平に分配される |
| `DISPATCH_ROUND_SIZE` | ConfigMap | 1 | Dispatcher | サイクルごと（namespace あたり） | ラウンドロビンの 1 ラウンドで各 namespace から取得するジョブ数。5 に設定すると各 namespace から 5 件ずつ交互に取得する |
| `count/jobs.batch` | ResourceQuota | 600 | Kubernetes | ユーザーごと | K8s 上に存在する `batch/v1 Job` オブジェクトの総数。実行中 + TTL 待ち完了済みジョブの合計が対象 |

4つの制限は独立したレイヤーで機能する。

```
cjob add → DB 登録（MAX_QUEUED_JOBS_PER_NAMESPACE: 2000件上限）
              ↓
Dispatcher がスキャン → dispatch_budget チェック（DISPATCH_BUDGET_PER_NAMESPACE: 256件上限）
                      → batch_size チェック（DISPATCH_BATCH_SIZE: 50件/サイクル上限）
              ↓
K8s Job を作成 → count/jobs.batch チェック（600件上限）
```

`count/jobs.batch` を 600 に設定する理由：dispatch_budget の上限（256件）で動作していても、SUCCEEDED/FAILED になった K8s Job が TTL（3時間）が切れるまで K8s 上に残り続けるため、実行中 + TTL 待ち完了済みジョブの合計を吸収できるよう dispatch_limit の2倍以上に設定している。

### CPU・メモリに関する制限

| 制限 | 設定箇所 | 値 | 管理主体 | 適用単位 | 制限対象 |
|---|---|---|---|---|---|
| ResourceQuota `requests.cpu` / `limits.cpu` | ResourceQuota（user namespace） | 300 | Kubernetes | ユーザーごと | namespace 内の全 Pod（Job Pod + User Pod）が要求・使用できる CPU の合計上限 |
| ResourceQuota `requests.memory` / `limits.memory` | ResourceQuota（user namespace） | 1250Gi | Kubernetes | ユーザーごと | namespace 内の全 Pod が要求・使用できるメモリの合計上限 |
| ClusterQueue `nominalQuota` CPU | ClusterQueue | 256 | Kueue | クラスタ全体 | Kueue が Job Pod に割り当てるクラスタ全体の CPU 上限。ユーザー間で共有される |
| ClusterQueue `nominalQuota` memory | ClusterQueue | 1000Gi | Kueue | クラスタ全体 | Kueue が Job Pod に割り当てるクラスタ全体のメモリ上限。ユーザー間で共有される |

ResourceQuota と ClusterQueue nominalQuota の違い：ResourceQuota は User Pod を含む namespace 内の全 Pod を対象とした上限（バグ等による無制限消費を防ぐ安全網）。ClusterQueue nominalQuota は Kueue が Job Pod の admission を判断するための上限であり、実際の実行スケジューリングを制御する。User Pod は Kueue を経由しないため ClusterQueue の制御対象外である。

### 隙間充填に関する設定

| 設定 | 設定箇所 | 値 | 管理主体 | 適用単位 | 説明 |
|---|---|---|---|---|---|
| `GAP_FILLING_ENABLED` | ConfigMap | true | Dispatcher | 全体 | 隙間充填ロジックの有効/無効。false にすると従来動作 |
| `GAP_FILLING_STALL_THRESHOLD_SEC` | ConfigMap | 300 (5分) | Dispatcher | ジョブごと | DISPATCHED からの経過秒数がこの値を超えたジョブを滞留とみなす |

隙間充填の詳細は [dispatcher.md](dispatcher.md) §2.4 を参照。

### Fair sharing に関する設定

| 設定 | 設定箇所 | 値 | 管理主体 | 適用単位 | 説明 |
|---|---|---|---|---|---|
| `FAIR_SHARE_WINDOW_DAYS` | ConfigMap | 7 | Dispatcher | 全体 | DRF の消費量集計に使用するスライディングウィンドウの日数。直近 N 日分の日別消費量を合計して dominant share を計算する |

DRF 正規化に使用するクラスタ全体のリソース容量は、`node_resources` テーブル（[database.md](database.md) §6）から `SUM()` で動的に取得する。従来の `CLUSTER_TOTAL_CPU_MILLICORES` / `CLUSTER_TOTAL_MEMORY_MIB` / `CLUSTER_TOTAL_GPUS` は廃止された。

日別リソース消費量の詳細は [database.md](database.md) §5、namespace の weight は [database.md](database.md) §4、DRF によるスケジューリングの詳細は [dispatcher.md](dispatcher.md) §1.1・§1.2 を参照。

### ノードリソース同期に関する設定

| 設定 | 設定箇所 | 値 | 管理主体 | 適用単位 | 説明 |
|---|---|---|---|---|---|
| `NODE_LABEL_SELECTOR` | ConfigMap | `cluster-job=true` | Watcher | 全体 | ノードリソース取得時の label selector。Kueue ResourceFlavor の `nodeLabels` と一致させる |
| `NODE_RESOURCE_SYNC_INTERVAL_SEC` | ConfigMap | 300 (5分) | Watcher | 全体 | ノードリソース同期間隔（秒）。Watcher のメインループの N サイクルに 1 回実行する |

ノードリソース同期の詳細は [watcher.md](watcher.md) §1.1、DB テーブル定義は [database.md](database.md) §6 を参照。

### 実行時間に関する制限

| 制限 | 設定箇所 | 値 | 管理主体 | 適用単位 | 制限対象 |
|---|---|---|---|---|---|
| `DEFAULT_TIME_LIMIT_SECONDS` | ConfigMap | 86400 (24h) | Submit API | ジョブごと | `time_limit_seconds` 省略時に適用されるデフォルト実行時間上限 |
| `MAX_TIME_LIMIT_SECONDS` | ConfigMap | 604800 (7d) | Submit API | ジョブごと | ユーザーが指定できる `time_limit_seconds` の最大値 |
| `activeDeadlineSeconds` | K8s Job spec | DB の `time_limit_seconds` | Kubernetes | ジョブごと | Pod が Running になってからの実行時間上限。超過時に K8s が Job を終了し、Watcher が FAILED（`time limit exceeded`）に遷移させる |

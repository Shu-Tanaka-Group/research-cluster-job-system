# パフォーマンス分析

## 1. コンポーネント別の負荷特性

| コンポーネント | 処理内容 | 負荷の支配要因 |
|---|---|---|
| Submit API | `cjob add` の受付、DB INSERT | ジョブ投入頻度。stateless で水平スケール可能（replicas で対応） |
| DB (PostgreSQL) | 全部品からの読み書き | 行数は少なく（数百〜数千件）、インデックスもあるため問題になりにくい |
| Dispatcher | DB スキャン → K8s Job 作成 | K8s API 呼び出し回数。シリアル実行のため 1 サイクルの処理時間に律速される |
| Kueue | admit 判断 → Pod スケジュール | Dispatcher の dispatch ペースに律速される |
| Watcher | K8s Job の状態監視 → DB 更新 | ポーリング間隔と active ジョブ数に比例 |

## 2. ボトルネック分析

### 2.1 通常の研究計算ワークロード（長時間ジョブ中心）

実行時間が数十分〜数時間のジョブが中心の場合、**Dispatcher** がボトルネックになりやすい。

- K8s Job 作成を `dispatch_one` で 1 件ずつシリアルに実行（各呼び出しで数百ミリ秒〜数秒）
- 1 サイクル最大 `DISPATCH_BATCH_SIZE`（50）件の上限
- サイクル間隔 `DISPATCH_BUDGET_CHECK_INTERVAL_SEC`（10 秒）

ただし、想定規模（同時アクティブユーザー数 20〜30 程度）では 50 件/10 秒のスループットで十分であり、実用上の問題にはなりにくい。

**改善オプション（必要になった場合）:**

| 方法 | 効果 | トレードオフ |
|---|---|---|
| `DISPATCH_BATCH_SIZE` を増やす | 1 サイクルの処理件数が増加 | K8s API への瞬間的な負荷が増加 |
| `DISPATCH_BUDGET_CHECK_INTERVAL_SEC` を短くする | サイクル間隔が短縮 | DB・K8s API へのポーリング頻度が増加 |
| K8s Job 作成の並列化 | スループットが大幅に向上 | 実装変更が必要。エラーハンドリングが複雑化 |

### 2.2 高頻度短時間ジョブワークロード

time_limit が数分程度のジョブが大量に高速回転する場合、**Watcher** がボトルネックになる。

ジョブのライフサイクルが速く回転するため、RUNNING → SUCCEEDED の遷移が高頻度に発生する。Watcher の状態検知が遅れると以下の連鎖が起きる。

```
Watcher の検知遅延（最大 10 秒）
  → DB 上のジョブが RUNNING のまま残る
  → Dispatcher の budget 計算で active ジョブ数が過大評価される
  → 新しいジョブが dispatch されない
  → スループット低下
```

**改善オプション（必要になった場合）:**

| 方法 | 効果 | トレードオフ |
|---|---|---|
| ポーリング間隔を短くする | 検知遅延が縮小（例: 3〜5 秒） | K8s API への負荷が増加 |
| Watch API への移行 | 状態変化を即座に検知。K8s API 負荷も軽減 | コネクション管理（再接続・resourceVersion）の実装が必要 |
| Informer パターンの採用 | Watch API + ローカルキャッシュで最も効率的 | 実装が複雑。Python より Go（client-go）の方が成熟したライブラリがある |

言語の変更（Python → Go 等）自体はほとんど効果がない。ボトルネックは CPU 処理速度ではなく I/O（K8s API のポーリング間隔とネットワーク遅延）であるため。

## 3. Watch API と Informer パターン

### 3.1 現在の方式（ポーリング）

```
Watcher → K8s API: list_job_for_all_namespaces()（10 秒ごと）
K8s API → Watcher: 全 Job のリスト（毎回全件取得）
```

変化がなくても毎回全件取得するため、active ジョブ数が増えると応答サイズも増大する。

### 3.2 Watch API

K8s の Watch API は HTTP ロングコネクションで状態変化のイベントをストリーミングする。

```
Watcher → K8s API: watch（1 回だけ接続）
K8s API → Watcher: 「Job A が RUNNING になりました」（イベント、即座）
K8s API → Watcher: 「Job B が Complete になりました」（イベント、即座）
```

- 状態変化を即座に検知（ポーリング遅延なし）
- K8s API への負荷が大幅に軽減（差分のみ受信）
- コネクション切断時のリカバリ（再接続 + re-list）の実装が必要

Python の `kubernetes` ライブラリの `watch.stream()` で実装可能。

### 3.3 Informer パターン

Kubernetes コントローラや Prometheus が使用するパターン。Watch API の上位互換。

```
1. 起動時に list で全件取得 → ローカルキャッシュに保存
2. Watch API で差分イベントを受信 → キャッシュを更新
3. ロジックはキャッシュに対して動作（K8s API を直接叩かない）
4. 接続切断時は自動で re-list + Watch 再開
```

- K8s API への負荷が最小（初回 list + 以降は Watch のみ）
- ローカルキャッシュへのアクセスはネットワーク遅延なし
- Go の `client-go` ライブラリが最も成熟した実装を持つ。Python の `kubernetes` ライブラリにも簡易的な Informer 実装があるが成熟度は劣る

### 3.4 Prometheus との比較

Prometheus は各 Pod の `/metrics` エンドポイントを直接 HTTP scrape する方式であり、K8s API はサービスディスカバリ（Pod 一覧の取得）にのみ使用する。Prometheus が K8s API に大きな負荷をかけないのは、メトリクスの取得先が K8s API ではなく各 Pod であることと、サービスディスカバリに Watch API（Informer パターン）を使用しているためである。

Watcher が必要とする情報（Job の `status.conditions`、`status.active`）は K8s API にしか存在しないため、Prometheus のように Pod に直接問い合わせる方式は適用できない。Prometheus から学べる教訓は「Watch API / Informer パターンを使うことで K8s API への負荷を最小化できる」という点である。

## 4. K8s スケーラビリティの制約

### 4.1 ボトルネックの本質

CJob のスケーラビリティを律速するのは Dispatcher や Watcher ではなく、**K8s 上に同時に存在する Job オブジェクトの数**である。K8s Job は 1 件につき Job オブジェクト + Pod オブジェクトが etcd に格納されるため、同時存在数が増えると以下が問題になる。

| 要因 | 影響 | スケールアウトの可否 |
|---|---|---|
| etcd の write 負荷 | Job/Pod の作成・状態更新は全て etcd への write | Raft 合意が必要なためノード追加では改善しない |
| kube-controller-manager (Job controller) | 全 Job の状態遷移を処理 | シングルリーダーのためスケールアウト不可 |
| Kueue controller | 全 Workload の admission 判断 | シングルリーダーのためスケールアウト不可 |
| kube-apiserver | list/watch リクエストの処理 | レプリカ追加で水平スケール可能 |

kube-apiserver はレプリカ数の増加で対応できるが、etcd の write と シングルリーダーの controller はスケールアウトが効かないため、**同時存在 Job 数の上限は K8s の構造的な制約**である。

### 4.2 同時存在 Job 数の見積もり

同時に K8s 上に存在する Job オブジェクト数は、active Job と TTL 待ちの完了済み Job の合計である。

```
同時存在 Job 数 = (同時アクティブユーザー数 × DISPATCH_BUDGET_PER_NAMESPACE)
               + (TTL ウィンドウ内の完了済み Job 数)
```

`ttlSecondsAfterFinished` を短縮すると完了済み Job の滞留は減るが、active Job 数は変わらない。

| 同時アクティブユーザー数 | dispatch_budget | active Job 数 | 安全性 |
|---|---|---|---|
| 20 | 256 | 5,120 | 運用可能（上限付近） |
| 30 | 256 | 7,680 | 厳しい |
| 50 | 256 | 12,800 | 非現実的 |
| 50 | 50 | 2,500 | 余裕 |

K8s の標準構成では、同時存在 Job 数 5,000〜10,000 程度が実用的な上限の目安である。

### 4.3 Watch API 移行による改善効果

Watch API に移行すると、Watcher の `list_job_for_all_namespaces()` による全件取得がなくなり、API Server と etcd の read 負荷が大幅に軽減される。ただし、ボトルネックの本質である etcd の write 負荷やシングルリーダー controller の処理能力は改善しない。

Watch API 移行により同時アクティブユーザー数の上限は 1.5 倍程度（20-30 → 30-45）に伸びる見込みだが、2 倍以上の改善は期待できない。

### 4.4 スパコンのジョブスケジューラとの比較

Slurm 等のスパコン向けスケジューラが大量ジョブを扱えるのは、アーキテクチャが根本的に異なるためである。

| | スパコン (Slurm 等) | CJob (K8s) |
|---|---|---|
| 1 ジョブのオーバーヘッド | メモリ上のレコード 1 件 | etcd 上に Job + Pod オブジェクト |
| 実行開始 | プロセスを直接 fork/exec | Pod 作成 → コンテナランタイム起動 |
| スケジューリング | スケジューラが直接ノードを割り当て | Dispatcher → K8s Job → Kueue → kube-scheduler → kubelet |
| 大量タスクの手段 | job array（1 件 = 数万タスク） | なし（1 件 = 1 タスク） |

スパコンではジョブ 1 件のオーバーヘッドが桁違いに小さいため、1 core × 10,000 ジョブの parameter sweep も日常的に実行される。K8s は汎用コンテナオーケストレーションとして設計されており、大量の短命ジョブを高速に回すユースケースは本質的に不得意である。

### 4.5 1 Job N Pod 構成による etcd 負荷の軽減

K8s の `batch/v1 Job` は `completions` と `parallelism` フィールドにより、1 つの Job オブジェクトから複数の Pod を段階的に実行できる。例えば `completions: 100, parallelism: 10` なら、同時に最大 10 Pod が実行され、1 つ完了するたびに次の Pod が起動し、合計 100 個完了するまで繰り返す。

これにより etcd 上の Job オブジェクト数を大幅に削減できる（100 タスクを 100 Job ではなく 1 Job で表現）。ただし以下の課題がある。

| 課題 | 内容 |
|---|---|
| コマンドの分岐 | 全 Pod が同一のコンテナ spec を持つため、Indexed Job（`completionMode: Indexed`）を使い Pod 内でインデックスに応じてコマンドを分岐させる仕組みが必要 |
| 失敗の分離 | `backoffLimit` に達すると Job 全体が Failed になる。個別タスクの成功・失敗を独立に扱えない |
| time_limit の粒度 | `activeDeadlineSeconds` は Job 全体に適用される。タスクごとに異なる time_limit を設定できない |
| ログの分離 | 複数タスクのログを 1 Job 内で分離する仕組みが必要 |
| キャンセルの粒度 | 個別タスクだけをキャンセルできない |
| Kueue の admit | Kueue は admit 時に `parallelism` 分のリソースをまとめて確保しようとするため、個々の Pod 単位で段階的に admit されない |

これらの課題から、1 Job N Pod 構成を汎用的に適用するのは困難であり、parameter sweep のような同一スペック・同一 time_limit のタスク群に限定するのが妥当である。

### 4.6 parameter sweep 機能による負荷軽減

スパコンの job array に相当する parameter sweep 機能は `cjob sweep` として実装済みである（[cli.md](cli.md) §3、[api.md](api.md) §2.1、[dispatcher.md](dispatcher.md) §3、[watcher.md](watcher.md) §4 参照）。K8s Indexed Job（`completionMode: Indexed`）を使用し、大量の小タスクを少ない Job オブジェクト数で実行できる。

**実現された効果：**

- etcd 上の Job オブジェクト数の削減（例: 1,000 タスク → 1 Job）
- `dispatch_budget` の消費が 1 件で済むため、budget 枠を効率的に使える
- Kueue への Workload 数が減り、admission 処理の負荷が軽減される
- `backoffLimitPerIndex: 0` により個別タスクの失敗が Job 全体に波及しない

**パフォーマンス特性：**

- Indexed Job は K8s Job controller が Pod を段階的に作成するため、Dispatcher の K8s API 呼び出しは 1 回で済む
- Watcher はポーリングサイクルごとに `status.completedIndexes` / `status.failedIndexes` を取得して DB を更新する。タスク数が多い場合でもポーリングの負荷は通常ジョブと同等（Job オブジェクト 1 件の status を読むだけ）
- `parallelism` の値が大きい場合、Kueue が一度に大量のリソースを確保しようとするため、admit までの待機時間が長くなる可能性がある

**インセンティブ設計：**

sweep 機能の導入後、`MAX_QUEUED_JOBS_PER_NAMESPACE` や `DISPATCH_BUDGET_PER_NAMESPACE` を引き下げることで、個別投入よりも sweep を使うインセンティブが生まれる。sweep では 1 件の投入枠で数百タスクを表現できるため、投入上限が厳しくなってもユーザーの実質的なキャパシティは減らない。

導入順序が重要であり、sweep 機能の実装が先、投入上限の引き下げが後でなければならない。sweep 機能がない状態で上限を下げると、ユーザーが単純に不便になるだけである。

### 4.7 dispatch_budget 削減によるスケーラビリティ改善

同時アクティブユーザー数が多い環境では、`DISPATCH_BUDGET_PER_NAMESPACE` を下げることで同時存在 Job 数を抑制できる。

アクティブユーザーが多い環境では、1 ユーザーがクラスタ全体を占有する必要はなく、公平に分け合うのが通常の運用形態である。そのため dispatch_budget の引き下げはリソース利用効率の低下を意味しない。

ただし、アクティブユーザーが少ない時間帯には、dispatch_budget が低いと 1 ユーザーがクラスタ全体を使い切れず遊休リソースが発生する。この問題はアクティブユーザー数に応じた dispatch_budget の動的調整で対処可能だが、実装の複雑さが増す。

## 5. 現時点での推奨

同時アクティブユーザー数 20〜30 程度、ジョブ実行時間が数十分以上のワークロードでは、現在のポーリング方式で十分なパフォーマンスが得られる。以下の状況が発生した場合に改善を検討する。

| 状況 | 対応 |
|---|---|
| QUEUED ジョブの dispatch が追いつかない | `DISPATCH_BATCH_SIZE` の増加、サイクル間隔の短縮 |
| 短時間ジョブの回転が遅い | Watcher のポーリング間隔短縮、Watch API への移行検討（§4.3） |
| 同時アクティブユーザー数の増加 | `DISPATCH_BUDGET_PER_NAMESPACE` の引き下げ（§4.7）、Watch API 移行（§4.3） |
| 大量の小タスク（parameter sweep）| `cjob sweep` を使用（§4.6、実装済み）。completions / parallelism の調整で負荷を制御 |
| K8s API への負荷が問題になる | Informer パターンの採用を検討（§3.3） |

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

ただし、研究室規模（5〜20 ユーザー）では 50 件/10 秒のスループットで十分であり、実用上の問題にはなりにくい。

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

## 4. 現時点での推奨

研究室規模（5〜20 ユーザー、ジョブ実行時間が数十分以上）では、現在のポーリング方式で十分なパフォーマンスが得られる。以下の状況が発生した場合に改善を検討する。

| 状況 | 対応 |
|---|---|
| QUEUED ジョブの dispatch が追いつかない | `DISPATCH_BATCH_SIZE` の増加、サイクル間隔の短縮 |
| 短時間ジョブの回転が遅い | Watcher のポーリング間隔短縮、Watch API への移行検討 |
| K8s API への負荷が問題になる | Informer パターンの採用を検討 |

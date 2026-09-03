# 未リリース移行手順

本ファイルは **次回リリース向け** の移行手順を記載する作業ファイルである。リリース時にバージョン名（例: `v1.16.0.md`）にリネームし、新しい `unreleased.md` を作成する（[versioning.md](../versioning.md) 参照）。

[標準移行手順](../migration.md) に加えて次回リリース固有の移行手順がある場合は以下に追記する。

## デプロイ順序（Watcher を Dispatcher より先に）

> 関連: issue #207 / PR #211

制限時間の強制が Dispatcher（`activeDeadlineSeconds` の付与）から Watcher（`started_at` 起点の強制）へ移ったため、**Watcher を Dispatcher より先にデプロイすること**。

逆順にすると、Dispatcher が `activeDeadlineSeconds` を付けない Job を作成する一方で Watcher 側の強制がまだ有効になっておらず、その間に投入されたジョブは制限時間が一切効かない状態になる。

[標準移行手順](../migration.md) のデプロイ順序（`watcher` → `dispatcher` → `submit-api`）どおりに実施すれば問題ない。

## ロールアウト時に実行中のジョブに残る旧挙動

> 関連: issue #207 / PR #211

制限時間の強制方式が K8s Job の `activeDeadlineSeconds` から Watcher による `started_at` 起点の強制に変更された（[watcher.md](../architecture/watcher.md) §3 ステップ 9 参照）。

本バージョンより前に Dispatcher が作成した K8s Job は `activeDeadlineSeconds` を保持したままであり、ロールアウト後もその値で K8s 側から終了させられる。したがって、ロールアウト時点で実行中・DISPATCHED のジョブには旧挙動（Kueue の admit 時点からの計測、すなわち Pod の Pending 滞留時間を含む計測）が残る。

- 対象ジョブは Watcher の新ロジックからも二重に監視されるが、`started_at` 起点の判定は `activeDeadlineSeconds` による終了より必ず後になるため、実害はない（K8s が先に終了させ、Watcher が `DeadlineExceeded` を `time limit exceeded` にマップする既存経路で FAILED になる）
- 旧挙動を残したくない場合は、ロールアウト前に実行中ジョブの完了を待つか、対象ジョブを `cjob cancel` して再投入する

新しく作成される K8s Job には `activeDeadlineSeconds` が付与されないため、ロールアウト以降に投入されたジョブは新挙動になる。

## `cjob-config` への DISPATCHED 滞留ガード設定の追加

> 関連: issue #208 / PR #212

`cjob-config` ConfigMap に新しい標準キーが 2 つ追加される。

| キー | デフォルト | 用途 |
|---|---|---|
| `WATCHER_DISPATCH_TIMEOUT_SEC` | `"1800"` | DISPATCHED のまま RUNNING に遷移しないジョブを配置不能とみなし、K8s Job を削除して QUEUED に差し戻すまでの秒数 |
| `WATCHER_DISPATCH_BACKOFF_MAX_SEC` | `"7200"` | 差し戻し時に設定する `retry_after` の指数バックオフ上限秒数 |

`kubectl apply -k overlays/<env>` で base の ConfigMap が反映された後、以下のいずれかを実行すること。

- base の ConfigMap をそのまま使っている場合: 追加作業は不要
- 独自 overlay で `cjob-config` の内容をパッチしている場合: overlay 側の ConfigMap patch に上記 2 キーを追記してから apply する。値を明示しない場合でも Python 側のデフォルトで動作するが、`cjobctl config show` の出力と一致させるため ConfigMap にも載せることを推奨する

`WATCHER_DISPATCH_TIMEOUT_SEC` は隙間充填の滞留閾値（`GAP_FILLING_STALL_THRESHOLD_SEC`、デフォルト 300 秒）より十分に長く設定すること。短くすると、隙間充填が大型ジョブを起動させる前に滞留ガードが差し戻してしまう。

## DB スキーマの更新（`jobs.unschedulable_count`）を Step 4 より先に実行する

> 関連: issue #208 / PR #212

`jobs` テーブルに `unschedulable_count INTEGER NOT NULL DEFAULT 0` が追加される。[標準移行手順](../migration.md) の Step 5（`cjobctl db migrate`）で冪等に適用され、既存行はデフォルト値 0 で埋まるため追加のデータ移行は不要である。

ただし本バージョンでは、**Step 5 を Step 4（K8s リソースの適用）より先に実行すること**。新しい Watcher はこのカラムに書き込み、新しい Dispatcher は滞留ジョブ検知クエリでこのカラムを参照するため、カラムが無い状態で新しいコンポーネントが起動すると reconcile サイクルと dispatch サイクルが SQL エラーで失敗し続ける。

`ADD COLUMN ... DEFAULT 0` は旧コードから見ると未参照のカラムが増えるだけなので、先に適用しても旧バージョンのコンポーネントには影響しない。

```bash
# Step 3 で cjobctl をビルドした後、Step 4 の前に実行する
cjobctl db migrate
```

## Grafana ダッシュボードの再インポート

> 関連: issue #208 / PR #212

`k8s/base/grafana/dashboard-user.json` に「配置待ちバックオフ中」パネル（Row 3）を追加し、「Flavor 別キュー使用状況」テーブルの幅を 24 → 18 に変更した。新パネルは `jobs.unschedulable_count` を参照するため、**DB スキーマ更新（上記）の後に**再インポートすること。

1. Grafana UI の `Dashboards > Import` から更新後の JSON をアップロードする
2. 既存ダッシュボードを上書きする（同一 UID）
3. データソース変数（`${DS_PROMETHEUS}` / `${DS_CJOB_DB}`）を環境に合わせて選択する


## `RESOURCE_FLAVORS` の事前確認（未知フィールドの拒否）

> 関連: issue #209

サーバ側の `FlavorDefinition` に `extra="forbid"` が導入され、`RESOURCE_FLAVORS` の flavor 定義に未知フィールドが含まれていると **Submit API / Dispatcher / Watcher が起動に失敗する**（従来は黙って無視されていた）。許可されるフィールドは `name` / `label_selector` / `gpu_resource_name` / `image` の 4 つのみである（[resources.md](../architecture/resources.md) の「`RESOURCE_FLAVORS` のスキーマ制約」参照）。

**Step 4（K8s リソースの適用）より前に**、現在の設定に未知フィールドが混入していないか確認すること。

```bash
cjobctl config show | grep -A 20 RESOURCE_FLAVORS
```

`gpu_resouce_name` のようなタイポを含む定義が見つかった場合は、修正してから適用する。

```bash
# 修正した JSON をファイルに用意してから
cjobctl config set RESOURCE_FLAVORS --from-file flavors.json
```

なお、新しい `cjobctl config set RESOURCE_FLAVORS` は構造チェック（未知フィールド・`name` の重複・`label_selector` の `key=value` 形式・`DEFAULT_FLAVOR` との整合）を行うため、修正時点で誤りがあればその場で拒否される。`cjobctl` のビルドは標準移行手順の Step 3 で完了しているため、この修正は Step 3 と Step 4 の間で実施できる。

## `CJOB_IMAGE` の役割変更（`cjob` / `cjobctl` の更新が必須）

> 関連: issue #210

flavor ごとの既定コンテナイメージ（`RESOURCE_FLAVORS` の `image`）を導入したことに伴い、Job Pod のイメージ解決順序が変わった（[api.md](../architecture/api.md) §2.2）。

```
--image  >  flavor の image  >  CJOB_IMAGE / JUPYTER_IMAGE
└ ユーザー明示 ┘  └ 管理者定義 ┘  └── 投入 Pod のイメージ ──┘
```

`CJOB_IMAGE` は「ユーザーによるイメージの上書き手段」ではなく「投入 Pod のイメージ名を CLI に伝える環境変数」に役割が変わった。**既定イメージが設定された flavor では `CJOB_IMAGE` による上書きは効かなくなる**。ユーザーによる上書きは `cjob add --image` / `cjob sweep --image` / `cjob set --image` に一本化された。

影響を受けるのは「既定イメージを設定した flavor に対して `CJOB_IMAGE` で上書きしていた」運用のみである。既定イメージを設定しない限り従来どおりの動作となるため、この移行手順は既定イメージを導入する場合にのみ関係する。

### 必要な作業

1. **`cjob` CLI の更新（必須）**

   旧 CLI は `image` を必ず送るため Submit API 側で最優先に採用され、**flavor 既定イメージが一切適用されない**。ジョブ投入自体は従来どおり成功するため障害としては現れず、「既定イメージを設定したのに効かない」という形で顕在化する。[標準移行手順](../migration.md) の CLI 配布手順に従い、ユーザーに `cjob update` を案内すること。

2. **`cjobctl` の更新（`image` を設定する場合は必須）**

   `cjobctl config set` の `RESOURCE_FLAVORS` 構造バリデーションは許可フィールドをホワイトリストで持つ。旧 `cjobctl` は `image` を未知フィールドとして拒否するため、`image` を含む定義を適用できない。ビルドは標準移行手順の Step 3 で完了する。

3. **flavor 既定イメージの設定（任意）**

   既定イメージを使う場合のみ実施する。設定前に [operations.md](../operations.md) §8.4.1 の確認事項（Kyverno 許可パターンとの一致、投入 Pod と同一 base であること）を満たしているか確認すること。

   ```bash
   cjobctl config set RESOURCE_FLAVORS --from-file flavors.json
   cjobctl system restart submit-api
   ```

   ```json
   [
     {"name": "cpu", "label_selector": "cjob.io/flavor=cpu"},
     {"name": "gpu", "label_selector": "cjob.io/flavor=gpu", "gpu_resource_name": "nvidia.com/gpu", "image": "your-registry/cjob-cuda:2.1.0"}
   ]
   ```

   image 解決は Submit API のみが行うため、この設定を反映するために再起動が必要なのは submit-api だけである（Dispatcher は `jobs.image` の確定値をそのまま使う）。

DB スキーマの変更はない（`jobs.image` は NOT NULL のまま、確定値が保存される）。既存ジョブの `jobs.image` も変更されない。

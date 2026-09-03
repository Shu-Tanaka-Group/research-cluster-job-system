---
name: deploy-runbook
description: 前回リリースからの変更を対象に、実環境へのデプロイ手順（runbook）を構築する。git diff から必要作業を導出し、docs/migration/unreleased.md とマージ済み PR の Post-apply actions と突き合わせて記載漏れを検出したうえで、実行順序を確定したチェックリストを出力する。複数 PR をまとめてデプロイするとき、リリース準備時の記載漏れ確認（versioning.md Step 4）に使う。
---

# デプロイ手順（runbook）の構築

前回リリースタグから現在までの変更について、実環境の管理者が実施すべき作業を洗い出し、実行順序を確定した runbook を組み立てる skill。

**方針:**

- **導出を先に、記録の集約を後に行う。** `docs/migration/unreleased.md` とマージ済み PR の `## Post-apply actions` を集約するだけでは、両方に書き忘れられた作業を検出できない。実際の `git diff` から必要作業を機械的に導出し、記録と突き合わせることではじめて記載漏れが見つかる。
- **順序の逸脱が最大の failure mode である。** 標準手順（[migration.md](../../../docs/migration.md)）の Step 番号順に読むと、`unreleased.md` に書かれた順序の逆転（例: 「Step 5 を Step 4 より先に」）を見落とす。この skill は最終的な実行順序を 1 本のリストに確定させる。
- **条件付きステップは yes/no に解決して出す。** 「`cli/` に変更がある場合」のような条件を残したまま渡さず、diff を見て「該当なし・スキップ」まで判定する。
- **報告と生成のみを行う。** `kubectl` / `docker` / `cjobctl` の実行はしない。

## 引数

```
$ARGUMENTS
```

- 引数なし: 直近のリリースタグ `..HEAD` を対象とする
- `<tag>`: そのタグ `..HEAD` を対象とする
- `<from>..<to>`: 指定した範囲を対象とする

範囲を確定したら、対象範囲と含まれるコミット数・PR 数をユーザーに提示してから作業を進める。

## Step 1: 対象範囲の確定

```bash
git tag --sort=-creatordate | head -3   # 直近のリリースタグ
cat VERSION                              # 現在のバージョン
git log --oneline <range>                # 範囲内のコミット
```

タグは `v` 接頭辞なし（`1.15.0` 形式）である点に注意する。

## Step 2: 必要作業の導出（記録を読む前に実施する）

`git diff --name-only <range>` の結果を以下の表に照合し、必要作業を洗い出す。**この段階では `unreleased.md` や PR 本文を読まない**（記録に引きずられると漏れを検出できなくなるため）。

| 変更パス | 導出される作業 | 標準手順 |
|---|---|---|
| `server/src/cjob/api/` | submit-api のイメージ build & push、再起動 | Step 2, 4 |
| `server/src/cjob/dispatcher/` | dispatcher のイメージ build & push、再起動 | Step 2, 4 |
| `server/src/cjob/watcher/` | watcher のイメージ build & push、再起動 | Step 2, 4 |
| `server/src/cjob/*.py`（`config.py` / `models.py` / `metrics.py` / `db.py` / `resource_utils.py` 等の共有モジュール） | 3 コンポーネントすべての build & push、再起動 | Step 2, 4 |
| `ctl/src/` | cjobctl のビルド | Step 3.1 |
| `cli/src/` | cjob CLI のビルド **と配布**（`cjobctl cli deploy`） | Step 3.2, 6 |
| `k8s/base/configmap-postgres-schema.yaml`、`ctl/src/cmd/db_migrate.rs`、`server/src/cjob/models.py` のスキーマ変更 | DB スキーマ更新（`cjobctl db migrate`）。cjobctl のビルドが前提になる | Step 3.1, 5 |
| `k8s/base/configmap-cjob-config.yaml` | ConfigMap キーの追加・デフォルト値変更 → overlay への反映要否の判断 | Step 1, 4 |
| `k8s/base/{submit-api,dispatcher,watcher}/deployment.yaml` | Deployment 定義の変更（env 注入・resources・probe 等） | Step 4 |
| `k8s/base/postgres/` | PostgreSQL の StatefulSet / Service の変更。データ層のためロールアウト時の停止時間とバックアップを個別に検討する | Step 4・要個別判断 |
| `k8s/base/` 直下のその他（`namespace.yaml` / `pvc-*.yaml` / `secret-*.yaml` / `kustomization.yaml`） | Kustomize での反映。Secret はテンプレートのみで実値は環境側にあるため、キー追加時は環境の Secret 更新が別途必要 | Step 4 |
| `k8s/base/rbac-*.yaml` | RBAC の変更 | Step 4 |
| `k8s/base/networkpolicy-*.yaml` | NetworkPolicy の変更 | Step 4 |
| `k8s/base/prometheus-operator/` | ServiceMonitor / PodMonitor の変更 | Step 4 |
| `k8s/base/grafana/*.json` | **Grafana ダッシュボードの再インポート**（Kustomize 管理外、手動作業） | 手動 |
| `docs/architecture/kueue.md` | Kueue リソース（ResourceFlavor / ClusterQueue）の手動更新の要否確認 | 手動 |
| `docs/deployment.md` | ノードラベル・Taint・Kyverno ポリシー等、デプロイ前提の変更確認 | 手動 |

Kyverno ポリシーは Kustomize 管理外のため、変更があれば個別適用が必要になる。

**表に該当しない変更パスの扱い:** `server/src/`・`cli/`・`ctl/`・`k8s/` 配下で表のどの行にも当てはまらない変更があれば、表の穴とみなして diff の内容を読み、運用影響の有無を判断する。判断した結果は報告に含める（表を後から拡充するため）。テストコード（`server/tests/`・`*/src/**/tests`）と設計書（`docs/`・`docs_en/`）のみの変更は運用作業を伴わない。

導出結果は「作業」「根拠となる変更パス」「標準手順の該当 Step」の 3 列で内部的に保持する。

## Step 3: 記録の収集

導出が終わってから、記録側を読む。

1. `docs/migration/unreleased.md` を読み、節ごとに「どの作業について書かれているか」を抽出する
2. 範囲内にマージされた PR を列挙し、各本文の `## Post-apply actions` と `Closes #<issue>` を取得する

```bash
gh pr list --state merged --base main --limit 30 --json number,title,mergedAt,body
```

範囲の起点タグの日時より後にマージされた PR に絞る。マージコミットのメッセージ（`Merge pull request #N from ...`）から PR 番号を拾う方法でもよい。

PR 本文は **起草時点の情報** であり、後続の PR が前提を覆している場合がある。記録同士や記録と diff が食い違うときは、`unreleased.md` と実 diff を優先する。

## Step 4: 突き合わせ（この skill の中核）

導出結果（Step 2）と記録（Step 3）を突き合わせ、以下を分類する。

- **`[GAP]`** — 導出されたが、`unreleased.md` にも PR の Post-apply actions にも現れない作業。記載漏れの疑い
- **`[STALE]`** — 記録にあるが、導出で裏付けが取れない作業。既に解消済み、または記述が古い疑い
- **`[CONFLICT]`** — `unreleased.md` と PR 本文、または記録同士で内容が食い違っている

**GAP の判定で誤検出しないための除外規則:**

- [migration.md](../../../docs/migration.md) に記載済みの標準手順は、`unreleased.md` に再掲されていなくても GAP ではない（`.claude/solve-overrides.md` の方針: 「`docs/migration.md` に記載済みの標準手順は再掲しない」）。GAP になるのは **標準手順から逸脱する作業**、または **標準手順では条件付きで、条件成立の判断材料が記録にない作業**
- テストコード・設計書のみの変更は運用作業を伴わない

**報告前の検証（必須）:**

`[GAP]` / `[STALE]` / `[CONFLICT]` を報告する前に、該当ファイルの diff を `git diff <range> -- <path>` で実際に読み、判定が正しいことを確認する。パス名のマッチだけで判断しない（例: `deployment.yaml` の変更がコメント修正のみなら運用影響はない）。検証で破棄した件数も報告に含める。

## Step 5: 実行順序の確定

標準手順の Step 1→7 を基準に、`unreleased.md` に書かれた順序制約を適用して最終順序を確定する。

確認すべき順序制約の型:

- **Step 間の逆転**（例: 「DB スキーマ更新を K8s リソース適用より先に」）— 新コードが未追加のカラムを参照する場合などに発生する
- **コンポーネント間の順序**（例: 「Watcher を Dispatcher より先に」）— データを生産する側を先にデプロイする
- **手動作業の位置**（例: 「Grafana の再インポートは DB スキーマ更新の後」）

順序制約は `unreleased.md` の複数の節に分散していることが多い。**すべての節を読んでから**確定すること。逸脱がある場合は理由を併記する。

## Step 6: runbook の出力

以下の構成でユーザーに提示する。ファイルには書き出さない（リリース PR の本文や作業メモに貼れる形式で出力する）。

```
## 対象範囲
<tag>..<ref>（コミット N 件 / PR M 件: #X, #Y）

## 突き合わせ結果
- GAP: N 件 / STALE: N 件 / CONFLICT: N 件 / 検証で破棄: N 件
（0 件なら「記載漏れなし」と明記する）

### [GAP] <作業の 1 行要約>
- 根拠: `<変更パス>`（`git diff` で確認）
- 記録: unreleased.md・PR 本文のいずれにも記載なし
- 対応: `docs/migration/unreleased.md` に追記が必要

## デプロイ手順

- [ ] 1. リポジトリの更新と差分確認
      根拠: ...
- [ ] 2. イメージのビルドと push（watcher / dispatcher のみ。submit-api は変更なし）
      根拠: `server/src/cjob/watcher/`、`server/src/cjob/dispatcher/` に変更
- [ ] 3. cjobctl のビルド
      根拠: `ctl/src/cmd/db_migrate.rs` に変更
- [ ] 4. DB スキーマの更新（`cjobctl db migrate`）
      ⚠ 標準手順の Step 5 だが、Step 4 より先に実行する
      根拠: unreleased.md「DB スキーマの更新を Step 4 より先に実行する」
- ...

## スキップするステップ
- Step 6（cjob CLI の配布）: `cli/` に変更がないため不要

## 注意事項
<unreleased.md に記載された、ロールアウト時の挙動やロールバック時の制約>
```

各項目には必ず**根拠**（どの変更パス / `unreleased.md` のどの節 / どの PR）を併記する。管理者が判断を再現できるようにするため。

## 注意事項

- この skill は runbook をリポジトリにコミットしない。移行手順の正本は `docs/migration/unreleased.md` である
- `[GAP]` が見つかった場合、修正先は `docs/migration/unreleased.md`（および `docs_en/migration/unreleased.md`）である。PR 本文は過去の記録なので書き換えない
- リリース準備時は [versioning.md](../../../docs/versioning.md) Step 4「移行手順の記載漏れ確認」の実施手段としてこの skill を使える。両者は重複ではなく、versioning.md が「何を確認するか」、この skill が「どう確認するか」を担う
- ファイル列挙・内容検索には Glob / Grep ツールを使う。bash の `find` / `grep` を使わない
- 実環境への接続（`kubectl` / `cjobctl` の実行）は行わない。`gh` と `git` の読み取り操作のみを使う

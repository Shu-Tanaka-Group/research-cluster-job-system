# solve overrides for cjob (research-cluster-job-system)

`/solve` skill の達成基準・権限境界に対するプロジェクト固有の追記。skill 側の基底規約を override するものではなく、delta を追加するためのファイル。

## ドキュメント整合の対象

本プロジェクトでは `docs/` 内の設計書が正本（Single Source of Truth）であり、コードは設計書から導出される成果物として扱う。設計書とコードが矛盾する場合は設計書を正とする。

設計に関わる変更は以下を同コミットで実施する。

- 該当する `docs/` 配下の設計書を **先に更新** し、更新後の設計書に基づいて実装する
- 設計書を更新した後、変更内容に関連するキーワードで `docs/` 全体を Grep し、同一情報が他の設計書にも記載されていないか確認する。該当箇所があれば同時に更新する
- 当該変更にプロジェクト固有の移行手順（実環境管理者の作業、例: ConfigMap 更新・DB スキーマ更新）が必要な場合は `docs/migration/unreleased.md` に追記する。`docs/migration.md` に記載済みの標準手順は再掲しない。単なる変更点の記録には用いない
- `docs/` を変更した場合、PR 作成までに `/translate-docs` skill を実行して英語版を `docs_en/` に反映し、コミットに含める

設計書一覧は `CLAUDE.md` の「開発ドキュメント一覧」を参照。

## テスト実行規約

詳細は `docs/testing.md` を参照。`/solve` の達成基準「テスト緑」では着手前と完了後の両方で以下を実行し、回帰がないことを確認する。

### Python (server)

- 通常実行: `cd server && uv run python -m pytest tests/ -v`
- 統合テスト込み（Docker 必須）: `cd server && uv run --extra integration python -m pytest -v`
- `uv run pytest` ではなくエントリポイント問題を避けるため `uv run python -m pytest` を使う

### Rust (cli)

- `cd cli && cargo test`

### Rust (cjobctl)

- `cd ctl && cargo test`

## 自動化モード stop conditions

以下の変更が必要と判明した時点で自動化を中止し、`/solve` skill の中止プロトコルに従って issue にコメントを残す。

- `cjob` / `cjobctl` の CLI インターフェース変更（引数、サブコマンド、出力フォーマット）
- submit 時の再現ルール変更（捕捉する環境変数、シェル起動方式、エスケープ方式、`cwd` の扱い等）
- コンポーネントの RBAC 変更
- 既存コンポーネントへの新しい役割の割り当て

検知タイミングは issue 読み込み時に限らない。設計書更新中・実装中に初めて判明した場合も、判明した時点で即座に中止する。

**自動化して問題ない変更の例**（網羅ではない。stop conditions に該当しなければ列挙されていなくても自動化モードで進めてよい）:

- 複数コンポーネントに跨る変更
- API / DB スキーマの変更（マイグレーション必要を含む）
- 設定ファイルの項目追加・変更、および構造的変更（マイグレーションで吸収可能）
- 設計書の更新を伴う変更
- 実環境テストが必要な変更
- ジョブログの保存先・フォーマット変更
- エラーメッセージの変更
- ジョブ ID 採番ルールの変更（ユーザーインターフェースが変わらない範囲）

## Post-apply actions

PR 本文の Post-apply actions 節には、その変更で実環境管理者が PR マージ後に実施する必要がある作業を列挙する。該当しないものは省略する。

- ConfigMap の更新（`k8s/` 配下の設定変更時）
- コンポーネント（API / Dispatcher / Watcher）のビルドと再起動
- `cjob` / `cjobctl` のビルド・配布（CLI 関連変更時）
- DB スキーマの更新（マイグレーション必要時）

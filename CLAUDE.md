# CLAUDE.md

このファイルは、Claude Code (claude.ai/code) がこのリポジトリで作業する際のガイダンスを提供する。

## プロジェクト概要

CJob は、オンプレ Kubernetes 環境上で動作するユーザー向けジョブキューシステムである。
研究計算・parameter sweep・バッチ計算を対象とし、ユーザーに Kubernetes の Job / Pod / YAML を意識させずに、シェルコマンドをそのままジョブとして投入できる。

### 技術スタック

- **CLI**: Rust（Clap）
- **API**: FastAPI + Pydantic
- **DB**: PostgreSQL（SQLAlchemy + psycopg）
- **実行基盤**: Kubernetes Job + Kueue
- **実行環境**: fixed image（Ubuntu 24.04）+ namespace PVC（`/home/jovyan`）
- **認証**: ServiceAccount JWT + TokenReview（Keycloak は JupyterHub ログイン時のみ）

### アーキテクチャ

```
User Pod (JupyterHub) → cjob CLI → Submit API → PostgreSQL
                                                        ↓
                                               Dispatcher → Kubernetes Job (Kueue)
                                                        ↓
                                               Watcher/Reconciler → DB 状態同期
```

- システムコンポーネントは `cjob-system` namespace に配置
- ユーザーごとに `user-<username>` namespace が分離されている
- job_id はユーザー（namespace）ごとの連番（1, 2, 3...）
- ジョブログは PVC 上（`/home/jovyan/.cjob/logs/<job_id>/`）に保存し、CLI が直接読む
- submit 時の `cwd`・export 済み環境変数（`PATH` / `VIRTUAL_ENV` 含む）・コマンドを Job Pod で再現

### CLI コマンド

```bash
cjob add -- <command>                # ジョブ投入
cjob list                            # 一覧表示
cjob status <job-id>                 # 状態確認
cjob cancel <job-id>                 # キャンセル（範囲: 1-10, 複数: 1,3,5, 組み合わせ対応）
cjob logs <job-id>                   # ログ表示（--follow / --delete / --delete-all）
cjob usage                           # リソース使用状況の表示
cjob reset                           # 全ジョブ履歴・ログ削除、job_id を 1 に戻す
```

## プロジェクト情報管理の基本方針

- 記述は人間が読みやすい形式で書く
- プロジェクト情報は適切な粒度に分割し、`docs/` ディレクトリ内に開発ドキュメントとして整備する
- 開発ドキュメントは Markdown 形式で作成する
- この CLAUDE.md ファイルには最小限の記述のみ含める
- 新しい実装や重要な決定があった場合は開発ドキュメントを更新する

## 開発ドキュメント一覧

- システムアーキテクチャ（インデックス）: docs/system_architecture.md
  - 機能要件: docs/architecture/requirements.md
  - 環境前提: docs/architecture/prerequisites.md
  - システム設計: docs/architecture/system_design.md
  - PostgreSQL 設計: docs/architecture/database.md
  - Kueue 設計: docs/architecture/kueue.md
  - リソース設計: docs/architecture/resources.md
  - API 設計: docs/architecture/api.md
  - CLI 設計: docs/architecture/cli.md
  - 管理 CLI（cjobctl）設計: docs/architecture/cjobctl.md
  - Dispatcher 設計: docs/architecture/dispatcher.md
  - Watcher 設計: docs/architecture/watcher.md
  - 実装計画: docs/architecture/implementation.md
  - パフォーマンス分析: docs/architecture/performance.md
- 認証・認可ガイドライン: docs/auth_policy.md
- デプロイガイドライン: docs/deployment.md
- ビルド手順: docs/build.md
- テスト: docs/testing.md
- Git 運用規則: docs/git_conventions.md
- 運用ガイド: docs/operations.md

## Git 操作

- プッシュ前には毎回必ずユーザーの確認を取ること
- コミット・ブランチ作成・PR 作成時は [Git 運用規則](docs/git_conventions.md) に従うこと。

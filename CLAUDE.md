# CLAUDE.md

このファイルは、Claude Code (claude.ai/code) がこのリポジトリで作業する際のガイダンスを提供する。

## プロジェクト概要

CJob は、オンプレ Kubernetes 環境上で動作するユーザー向けジョブキューシステムである。
研究計算・parameter sweep・バッチ計算を対象とし、ユーザーに Kubernetes の Job / Pod / YAML を意識させずに、シェルコマンドをそのままジョブとして投入できる。

### 技術スタック

- **言語**: Python
- **CLI**: Typer
- **API**: FastAPI + Pydantic
- **DB**: PostgreSQL（SQLAlchemy + psycopg）
- **メッセージキュー**: RabbitMQ（Kombu）
- **実行基盤**: Kubernetes Job + Kueue
- **認証**: ServiceAccount JWT + TokenReview（Keycloak は JupyterHub ログイン時のみ）

### アーキテクチャ

```
User Pod (JupyterHub) → cjob CLI → Submit API → PostgreSQL + RabbitMQ
                                                        ↓
                                               Dispatcher → Kubernetes Job (Kueue)
                                                        ↓
                                               Watcher/Reconciler → DB 状態同期
```

- システムコンポーネントは `cjob-system` namespace に配置
- ユーザーごとに `user-<username>` namespace が分離されている
- ジョブログは PVC 上（`/workspace/.cjob/logs/<job_id>/`）に保存し、CLI が直接読む

## プロジェクト情報管理の基本方針

- 記述は人間が読みやすい形式で書く
- プロジェクト情報は適切な粒度に分割し、`docs/` ディレクトリ内に開発ドキュメントとして整備する
- 開発ドキュメントは Markdown 形式で作成する
- この CLAUDE.md ファイルには最小限の記述のみ含める
- 新しい実装や重要な決定があった場合は開発ドキュメントを更新する

## 開発ドキュメント一覧

- システムアーキテクチャ: docs/system_architecture.md
- 認証・認可ガイドライン: docs/auth_policy.md
- デプロイガイドライン: docs/deployment.md

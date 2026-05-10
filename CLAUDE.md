# CLAUDE.md

このファイルは、Claude Code (claude.ai/code) がこのリポジトリで作業する際のガイダンスを提供する。

## プロジェクト概要

CJob は、Kubernetes 環境上で動作するユーザー向けジョブキューシステムである。
研究計算・parameter sweep・バッチ計算を対象とし、ユーザーに Kubernetes の Job / Pod / YAML を意識させずに、シェルコマンドをそのままジョブとして投入できる。

### 技術スタック

- **CLI**: Rust（Clap）
- **API**: FastAPI + Pydantic
- **DB**: PostgreSQL（SQLAlchemy + psycopg）
- **実行基盤**: Kubernetes Job + Kueue
- **実行環境**: fixed image（`/bin/bash` が利用可能な任意の OS）+ namespace PVC（`/home/jovyan`）
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
- ユーザーごとに namespace が分離されている（例: `user-alice`）
- job_id はユーザー（namespace）ごとの連番（1, 2, 3...）
- ジョブログは PVC 上（`/home/jovyan/.cjob/logs/<job_id>/`）に保存し、CLI が直接読む
- submit 時の `cwd`・export 済み環境変数（`PATH` / `VIRTUAL_ENV` 含む）・コマンドを Job Pod で再現
- CLI の仕様は [docs/architecture/cli.md](docs/architecture/cli.md) を参照

## プロジェクト情報管理の基本方針

### 設計書が正本である

本プロジェクトでは、`docs/` 内の設計書がシステムの正本（Single Source of Truth）である。コードは設計書から導出される成果物であり、設計書とコードが矛盾する場合は設計書を正とする。

この原則の背景:

- 開発者はコードを直接編集せず、設計書の整備に集中し、実装は Claude に委ねる運用をとっている
- 設計書の正確性がコード品質に直結するため、設計書の劣化はプロジェクト全体の劣化を意味する
- 設計書は開発者がプロジェクトの方針を決定するための最重要資料であり、常に現状を正確に反映している必要がある

実装時の行動規範:

- **設計書を先に更新し、更新した設計書に基づいて実装する**
- コードの変更に伴い設計書の更新が必要な場合は、同じ開発セッション内で必ず設計書も更新する。設計書の更新を後回しにしない
- 設計書に記載されたパラメータ一覧、スキーマ定義、API 仕様などは、対応するコードと常に一致していなければならない
- 実装の都合で設計を変更する場合は、先に設計書を修正してから実装に反映する。コードだけ変えて設計書を放置しない

### その他の方針

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
  - 将来拡張: docs/architecture/roadmap.md
  - パフォーマンス分析: docs/architecture/performance.md
  - モニタリング設計: docs/architecture/monitoring.md
- 認証・認可ガイドライン: docs/auth_policy.md
- デプロイガイドライン: docs/deployment.md
- ビルド手順: docs/build.md
- テスト: docs/testing.md
- Git 運用規則: docs/git_conventions.md
- 運用ガイド: docs/operations.md
- バージョン管理: docs/versioning.md
- バージョン移行手順（汎用）: docs/migration.md
  - 未リリースの移行手順: docs/migration/unreleased.md（リリース時にバージョン名にリネーム）
  - バージョン固有の移行手順: docs/migration/ ディレクトリ（例: docs/migration/v1.10.0.md）
- ユーザーガイド: docs/user_guide.md

## 開発手順

issue 対応や機能追加は `/solve` skill を使う。基底規約は `~/.claude/skills/solve/SKILL.md`、本リポジトリ固有の追記（ドキュメント整合の対象、テスト実行規約、自動化モード stop conditions、Post-apply actions）は [.claude/solve-overrides.md](.claude/solve-overrides.md) を参照。

## Git 操作

- プッシュ前には毎回必ずユーザーの確認を取ること（自動化モードの場合を除く）
- コミット・ブランチ作成・PR 作成時は [Git 運用規則](docs/git_conventions.md) に従うこと。
- 別ディレクトリから git を実行する場合は `cd <path> && git ...` の複合コマンドを使わず、`git -C <path> ...` を使うこと。Claude Code の harness は bare repository attack 対策として `cd` + `git` の複合コマンドを承認待ちにするため、自動化モードでも中断される。

## Claude Code ツールの使用

- ドキュメントや指示内でシェルの `find` や `grep` に言及があっても、Claude Code の dedicated tool（Glob / Grep）を優先して使うこと。Grep は ripgrep ベースで gitignore をデフォルトで尊重するため、`.venv` / `target` / `.git` 等の除外が自動で効き、`find ... ! -path */.venv/*` のような複雑な除外指定は不要。
- ファイル列挙は Glob、または Grep の `output_mode="files_with_matches"` を使う。
- Bash 経由で `find` / `grep` を実行しない（シェル依存の特殊ケースを除く）。

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

- **設計書を先に更新し、更新した設計書に基づいて実装する**（開発手順の Step 5-8）
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

issue の対応や機能追加を行う際は、以下の手順に従って開発を進める。

1. **issue の確認・作成**: issue が指定されていない場合、問題点をまとめた issue を作成する
2. **情報収集**: issue の内容を把握し、プロジェクト概要および問題解決に必要な設計書の該当箇所を読み込む
3. **情報の確認**: 収集した情報だけでは要件や仕様が不明確な場合、ユーザーに不足情報の提供を求める
   - 例: 期待する動作、影響範囲、制約条件、優先度など
   - 問題の特定や方針決定に有用だと判断した場合、ユーザーにコマンドの実行結果（エラーログ、実行時の出力、クラスタの状態、バージョン情報など）の共有を依頼する
   - 曖昧な前提のまま設計・実装に進まないこと
4. **解決方法の決定**: 問題の解決方法を決定する
   - 複数コンポーネントに跨る変更や設計判断が必要な場合は、この段階で Plan モードを使用して方針を検討する
5. **設計書の更新**: 解決に伴う設計変更を `docs/` 内の設計書に反映させる
   - 設計書を更新した場合、変更内容に関連するキーワードで `docs/` 全体を検索し、同一情報が他の設計書にも記載されていないか確認する。該当箇所があれば同時に更新する
   - docs/migration/unreleased.md にこの変更に固有の移行手順（docs/migration.mdに記載済みの標準移行手順は除く）を記載する
   - unreleased.mdに記載する内容は実環境の管理者が行う作業に限る。単なる変更点の記録には用いるべきではない
6. **設計変更のコミット**: 設計書の変更を新しいブランチにコミットする
7. **実装計画の作成**: 設計書の内容に基づき、Plan モードで実装計画を作成する
   - ステップ 4 で Plan モードを使用済みの場合は、その計画を実装レベルに具体化する
   - 実装方法は設計方針を遵守すること
   - その場しのぎの解決方法を選択しない。今後の開発の負債とならない選択をできるよう深慮すること
   - 問題解決のための重要な関数についてはユニットテストを追加する。ただし、外部依存性が高いなどの理由でテストが困難な関数はテスト対象から除外する
   - 計画にはコミット単位を明記する（設計書・API・CLI・テスト等、論理的なまとまりごと）
8. **実装とコミット**: 実装計画の項目ごとに実装し、論理的なまとまりごとにコミットする
   - 例: 設計書更新 → API 変更 + テスト → CLI 変更 → テストドキュメント更新、のようにコミットを分割する
   - 各コミットは単体で意味のある変更単位とし、テストがある場合はコードと同じコミットに含める
   - 実装中に設計の問題や変更の必要性が発覚した場合は、Step 5 に戻って設計書を先に更新する
9. **プッシュ確認**: 変更の概要を表示し、ユーザーにプッシュの許可を求める
10. **PR作成**: `/create-pr` スキルを使用してpull requestを作成する
   - Test plan の完了済みテストにはチェックマークを付けておく
   - Post-apply actionsには必要に応じて以下の手順を含める
      - ConfigMap の更新
      - コンポーネントのビルドと再起動
      - cjob / cjobctl のビルド
      - DB スキーマの更新

## Git 操作

- プッシュ前には毎回必ずユーザーの確認を取ること
- コミット・ブランチ作成・PR 作成時は [Git 運用規則](docs/git_conventions.md) に従うこと。

# CJob 設計書

## 1. 概要

本設計書は、オンプレ Kubernetes 環境上で動作する、**ユーザー向けジョブキューシステム `cjob`** の設計をまとめたものである。
本システムは、研究計算・parameter sweep・バッチ計算を対象とし、**ユーザーに Kubernetes の Job / Pod / YAML を意識させずに**、シェルコマンドをそのままジョブとして投入できることを目的とする。

本システムは、以下の方針で設計する。

- ユーザー操作は `cjob add <job command>` を基本とする
- 実行環境は Kubernetes 上に構築する
- 実行単位は **1コマンド = 1 Kubernetes Job**
- ジョブ投入数が非常に多くなることを想定し、**DB スキャン型 Dispatcher** により dispatch を制御する
- Kubernetes 上の実行制御には **Kueue** を用いる
- ユーザーの作業ディレクトリと環境変数を可能な範囲でそのまま再現してジョブを実行する
- 将来的に Prefect 等の上位 orchestration 層を追加可能な構成とする

## 2. ドキュメント構成

詳細設計は以下のファイルに分割している。

| ドキュメント | 内容 |
|---|---|
| [architecture/requirements.md](architecture/requirements.md) | 機能要件・ユースケース |
| [architecture/prerequisites.md](architecture/prerequisites.md) | インフラ・実行環境・スケジューリング前提 |
| [architecture/system_design.md](architecture/system_design.md) | 必要機能一覧・実装方針・システム構成 |
| [architecture/database.md](architecture/database.md) | PostgreSQL テーブル定義・状態遷移 |
| [architecture/kueue.md](architecture/kueue.md) | Kueue 設計・Job テンプレート |
| [architecture/resources.md](architecture/resources.md) | リソース設計・制限まとめ |
| [architecture/api.md](architecture/api.md) | API エンドポイント仕様 |
| [architecture/cli.md](architecture/cli.md) | CLI コマンド仕様・使用例・動作詳細 |
| [architecture/dispatcher.md](architecture/dispatcher.md) | Dispatcher スケジューリング・詳細設計 |
| [architecture/watcher.md](architecture/watcher.md) | Watcher / Reconciler 設計 |
| [architecture/cjobctl.md](architecture/cjobctl.md) | 管理 CLI（cjobctl）設計 |
| [architecture/implementation.md](architecture/implementation.md) | 技術スタック・実装方針・手順・スコープ・将来拡張 |

関連ドキュメント:

| ドキュメント | 内容 |
|---|---|
| [auth_policy.md](auth_policy.md) | 認証・認可設計 |
| [deployment.md](deployment.md) | デプロイ設計・K8s マニフェスト |

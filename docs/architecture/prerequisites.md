# 環境前提

## 1. インフラ前提

本システムは次の前提で構築する。

- Kubernetes クラスタが存在する
- ユーザーごとに namespace が分離されている（手動作成・スクリプトで自動化）
- ユーザー namespace ごとに作業用 PVC が存在する
- PVC の mount path はデフォルト `/home/jovyan` とし、ConfigMap の `WORKSPACE_MOUNT_PATH` で変更可能
- Kueue を Kubernetes クラスタに導入する
- 状態管理用に PostgreSQL を使用する（新規デプロイ）
- ReadWriteMany 対応の StorageClass を導入済み（例: NFS subdir external provisioner）
- ジョブキューシステム専用ノードには `cjob.io/flavor=<flavor名>` ラベルと `role=computing:NoSchedule` Taint が付与されている
- 想定規模：現在はユーザー数 10 名・ノード 2 台。ノード数をユーザー数に比例して増設する運用で、長時間ジョブ中心のワークロードでは 100〜150 名まで対応可能（詳細は [performance.md](architecture/performance.md) §6 参照）

## 2. 実行環境前提

- **ジョブ投入を行う Pod とジョブを実行する Pod は同じ image を使う**
- image は User Pod の環境変数 `CJOB_IMAGE` から自動取得する（ユーザーが明示的に指定しない）
- `CJOB_IMAGE` が未設定の場合は `JUPYTER_IMAGE` にフォールバックする（JupyterHub 環境との後方互換）
- JupyterHub の User Pod には `JUPYTER_IMAGE` に現在のコンテナイメージ名が設定されている
- `cjob` CLI は Rust で実装したシングルバイナリとして GitHub Releases で配布する
- ユーザーは CLI バイナリを各自のホームディレクトリ（例: `/home/jovyan/.local/bin/`）に配置する
- CLI は image には含めない
- ベース OS は任意（`/bin/bash` が利用可能であること。例: Ubuntu 24.04）
- PVC 名はユーザー名と一致している
- 実行 shell は `/bin/bash -lc` を基本とする
- 作業ディレクトリは `${WORKSPACE_MOUNT_PATH}` 配下に限定する
- export 済み環境変数のみ再現対象とする（仮想環境の `PATH` / `VIRTUAL_ENV` を含む。ユーザー設定の `env.exclude` で除外指定した変数を除く）
- shell function / alias / shell option は再現対象外とする
- ユーザーは `${WORKSPACE_MOUNT_PATH}` 配下に Python 仮想環境を作成して管理する
- Job Pod と User Pod は同一 image のため、venv 内の C 拡張ライブラリ互換性が保たれる

## 3. スケジューリング前提

- Kubernetes Job が実行単位である
- Kueue は admission / queueing / fairness を担う
- ResourceQuota は namespace ごとのバグ等による意図しない無制限消費を防ぐ安全網として用いる（公平化は Kueue の BestEffortFIFO が担う）
- Kueue に流す Job 数は Dispatcher が制御する

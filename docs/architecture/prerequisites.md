# 環境前提

## 1. インフラ前提

本システムは次の前提で構築する。

- Kubernetes クラスタが存在する（v1.26 以上。Watcher が Job 状態判定に使う `status.ready` フィールドは `JobReadyPods` 機能が GA となった v1.26 で安定提供される。[watcher.md](watcher.md) §3 参照）
- ユーザーごとに namespace が分離されている（手動作成・スクリプトで自動化）
- ユーザー namespace ごとに作業用 PVC が存在する
- PVC の mount path はデフォルト `/home/jovyan` とし、ConfigMap の `WORKSPACE_MOUNT_PATH` で変更可能
- Kueue を Kubernetes クラスタに導入する
- 状態管理用に PostgreSQL を使用する（新規デプロイ）
- ReadWriteMany 対応の StorageClass を導入済み（例: NFS subdir external provisioner）
- ジョブキューシステム専用ノードには `cjob.io/flavor=<flavor名>` ラベルと `role=computing:NoSchedule` Taint が付与されている
- 想定規模：現在はユーザー数 10 名・ノード 2 台。ノード数をユーザー数に比例して増設する運用で、長時間ジョブ中心のワークロードでは 100〜150 名まで対応可能（詳細は [performance.md](performance.md) §6 参照）

## 2. 実行環境前提

- **ジョブを実行する Pod の image は、既定ではジョブ投入を行う Pod と同じものを使う**。flavor に既定イメージが設定されている場合、およびユーザーが `cjob add --image` で明示指定した場合はそれが優先される（§2.1 参照）
- 投入 Pod の image は User Pod の環境変数 `CJOB_IMAGE` から自動取得し、未設定の場合は `JUPYTER_IMAGE` にフォールバックする（JupyterHub 環境との後方互換）。両方未設定でも、flavor 既定イメージまたは `--image` で解決できればジョブは投入できる
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
- Job Pod と User Pod が同一 image である限り、venv 内の C 拡張ライブラリ互換性が保たれる。異なる image を使う場合は §2.1 の前提条件を満たす必要がある

### 2.1 Job Pod と投入 Pod で image が異なる場合の前提条件

flavor 既定イメージ（[resources.md](resources.md) の `RESOURCE_FLAVORS`）や `cjob add --image` を使うと、Job Pod のイメージは投入 Pod と異なりうる。この場合、次の条件を満たすイメージのみを使用すること。

- **投入 Pod のイメージと同じ base から派生し、Python のバージョンとインストールパスが一致していること**

PVC 上の venv は投入元 User Pod でビルドされ、submit 時に収集された `VIRTUAL_ENV` / `PATH` が Job Pod で再現される。venv の `pyvenv.cfg` はシステム Python のパスを `home` で指しているため、両イメージでこのパスが有効でなければ Job Pod 側で venv が壊れる。C 拡張ライブラリの ABI 互換性も同様に base の一致に依存する。

この前提が破られる典型例は、base OS やディストリビューションが異なるイメージ（例: Ubuntu ベースの投入 Pod に対して Alpine ベースの実行イメージ）や、Python のマイナーバージョンが異なるイメージを flavor 既定イメージに設定する場合である。CUDA ランタイムの有無のように、同一 base に対してライブラリを追加しただけのイメージであればこの条件を満たす。

flavor 既定イメージを設定する際の運用手順は [operations.md](../operations.md) §8 を参照。

## 3. スケジューリング前提

- Kubernetes Job が実行単位である
- Kueue は admission / queueing / fairness を担う
- ResourceQuota は namespace ごとのバグ等による意図しない無制限消費を防ぐ安全網として用いる（公平化は Kueue の BestEffortFIFO が担う）
- Kueue に流す Job 数は Dispatcher が制御する

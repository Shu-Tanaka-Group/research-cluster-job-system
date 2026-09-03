# 未リリース移行手順

本ファイルは **次回リリース向け** の移行手順を記載する作業ファイルである。リリース時にバージョン名（例: `v1.16.0.md`）にリネームし、新しい `unreleased.md` を作成する（[versioning.md](../versioning.md) 参照）。

[標準移行手順](../migration.md) に加えて次回リリース固有の移行手順がある場合は以下に追記する。

## ロールアウト時に実行中のジョブに残る旧挙動

制限時間の強制方式が K8s Job の `activeDeadlineSeconds` から Watcher による `started_at` 起点の強制に変更された（[watcher.md](../architecture/watcher.md) §3 ステップ 9 参照）。

本バージョンより前に Dispatcher が作成した K8s Job は `activeDeadlineSeconds` を保持したままであり、ロールアウト後もその値で K8s 側から終了させられる。したがって、ロールアウト時点で実行中・DISPATCHED のジョブには旧挙動（Kueue の admit 時点からの計測、すなわち Pod の Pending 滞留時間を含む計測）が残る。

- 対象ジョブは Watcher の新ロジックからも二重に監視されるが、`started_at` 起点の判定は `activeDeadlineSeconds` による終了より必ず後になるため、実害はない（K8s が先に終了させ、Watcher が `DeadlineExceeded` を `time limit exceeded` にマップする既存経路で FAILED になる）
- 旧挙動を残したくない場合は、ロールアウト前に実行中ジョブの完了を待つか、対象ジョブを `cjob cancel` して再投入する

新しく作成される K8s Job には `activeDeadlineSeconds` が付与されないため、ロールアウト以降に投入されたジョブは新挙動になる。

# 未リリース移行手順

本ファイルは **次回リリース向け** の移行手順を記載する作業ファイルである。リリース時にバージョン名（例: `v1.13.0.md`）にリネームし、新しい `unreleased.md` を作成する（[versioning.md](../versioning.md) 参照）。

[標準移行手順](../migration.md) に加えて次回リリース固有の移行手順がある場合は以下に追記する。

## cjobctl CLI コマンド変更

`cjobctl counters list` は `cjobctl jobs counters` に移行された。既存のスクリプトや手順書で旧コマンドを使用している場合は更新すること。

# 未リリース移行手順

[標準移行手順](../migration.md) に加えて、以下の追加作業が必要。

## node_name の累積リスト化（#126）

### API レスポンスの変更

`GET /v1/jobs/{job_id}` の `node_name` フィールドの型が `string | null` から `list[string] | null` に変更された。

**変更前:**

```json
{ "node_name": "worker07" }
```

**変更後:**

```json
{ "node_name": ["worker07", "worker08"] }
```

### DB スキーマの変更

なし。`node_name` カラムは TEXT のまま。内部的にカンマ区切りで複数ノード名を格納する形式に変更されたが、カラム型の変更は不要。既存データ（単一ノード名）はそのまま有効。

### 必要な作業

1. **Watcher イメージの再ビルド**: node_name の累積記録ロジックが変更された
2. **Submit API イメージの再ビルド**: API レスポンスの型変更が含まれる
3. **CLI のリビルド**: `node_name` を `list[string]` として受け取るように変更された

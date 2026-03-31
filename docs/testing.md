# テスト

## 1. テスト実行手順

### Python（Server）

```bash
cd server
uv run pytest tests/ -v
```

初回実行時、`uv` が仮想環境の作成と依存インストールを自動で行う。
`pytest` がインストールされていない場合は `uv pip install pytest` を実行する。
FastAPI の HTTPException を使うテストがあるため `fastapi` も必要（`uv pip install fastapi`）。

### Rust（CLI）

```bash
cd cli
cargo test
```

追加の依存インストールは不要。

## 2. テストカバレッジ

### テスト済み

| テストファイル | 対象 | テスト数 | 概要 |
|---|---|---|---|
| **Python** | | | |
| `tests/test_auth.py` | `api/auth.py::extract_bearer` | 7 | Bearer トークン抽出。正常系 / ヘッダなし / 空ヘッダ / Bearer 以外のスキーム / 小文字 bearer 等 |
| `tests/test_determine_status.py` | `watcher/reconciler.py::determine_status` | 9 | K8s Job の conditions から DB ステータスへのマッピング。SUCCEEDED / FAILED / RUNNING / DeadlineExceeded / 条件なし等 |
| `tests/test_build_k8s_job.py` | `dispatcher/k8s_job.py::build_k8s_job`, `_parse_taint` | 24 | K8s Job マニフェスト生成。ラベル / activeDeadlineSeconds / リソース / 環境変数 / ボリューム / コマンドラッピング / toleration（デフォルト・カスタム・空）/ taint パース（正常系・異常系） |
| `tests/test_services.py` | `api/services.py` 全関数 | 59 | submit_job（time_limit・リソース超過バリデーション含む）/ list_jobs / get_job / cancel（HELD 含む）/ hold_single・hold_bulk / release_single・release_bulk / delete（HELD skip 含む）/ reset（HELD ブロック含む）/ get_usage |
| `tests/test_reconciler.py` | `watcher/reconciler.py` | 35 | reconcile_cycle のステータス同期（started_at / finished_at / last_error / node_name 記録・RUNNING スキップ時の完了時取得・既存値の非上書き）/ CANCELLED 削除 / orphan 検出 / DELETING フェーズ 1・2 / namespace 分離 / RUNNING 遷移時の累計消費量加算（namespace_daily_usage）/ K8s Job 消失検出（DISPATCHED・RUNNING → FAILED 遷移・last_error・finished_at 設定）/ parse_cpu_millicores / parse_memory_mib |
| `tests/test_scheduler.py` | `dispatcher/scheduler.py` 4関数 | 13 | cas_update_to_dispatching / mark_dispatched / mark_failed / reset_stale_dispatching の CAS 動作・状態遷移 |
| `tests/test_gap_filling.py` | `dispatcher/scheduler.py::apply_gap_filling` | 7 | 隙間充填フィルタリング。無効時 / 滞留なし / 残り時間による候補選択 / RUNNING なし / namespace 混在 / 残り時間 0 / 候補なし |
| `tests/test_resource_utils.py` | `resource_utils.py` | 12 | CPU・メモリ文字列のパース。整数 / 小数 / ミリコア / Gi / Mi / Ki / 大きな値等 |
| `tests/test_node_sync.py` | `watcher/node_sync.py::sync_node_resources` | 7 | ノードリソース同期。挿入 / 更新 / 削除 / 全削除 / GPU パース / API エラー時のデータ保持 / ラベルセレクタ |
| `tests/test_cli_endpoints.py` | `api/routes.py` CLI 配布エンドポイント | 16 | `/v1/cli/version`・`/v1/cli/versions`・`/v1/cli/download` の正常系 / 404 / 認証不要 / バージョンソート / バージョン指定ダウンロード / 無効ディレクトリ除外の検証 |
| `tests/test_cluster_totals.py` | `dispatcher/scheduler.py::_fetch_cluster_totals` | 3 | DRF 正規化用クラスタ合計取得。空テーブル / 単一ノード / 複数ノード合計 |
| **Rust** | | | |
| `src/job_ids.rs` | `parse_job_ids` | 7 | ジョブ ID 式のパース（単体 / 範囲 / リスト / 組み合わせ / 重複除去 / エラー） |
| `src/main.rs` | `parse_duration` | 9 | 時間指定のパース（秒数 / s / m / h / d サフィックス / 空白 / 不正値 / オーバーフロー） |
| `src/display.rs` | `format_duration` / `format_time_limit` | 9 | 時間表示フォーマット（日 / 時間 / 分）/ RUNNING 時の残り時間計算 / 非 RUNNING / 不正日付のフォールバック |
| **Rust (cjobctl)** | | | |
| `src/cmd/cli_deploy.rs` | `run`（バリデーション） | 4 | --release + プレリリースのエラー / 安定版・プレリリース × release フラグのバリデーション順序 |
| `src/cmd/cli_list.rs` | `parse_versions` / `sort_versions` | 9 | ls 出力パース（latest 除外 / 空入力 / パース不能エントリ）/ ソート（降順 / プレリリース優先 / 設計書出力例の再現） |
| `src/cmd/cli_set_latest.rs` | `run`（バリデーション） | 2 | プレリリース版の拒否（beta / rc） |

**合計: Python 192 + Rust 40 = 232 テスト**

### 未テスト

| 対象 | 理由 |
|---|---|
| `dispatcher/scheduler.py::fetch_dispatchable_jobs` | PostgreSQL 固有の CTE（`ROW_NUMBER() OVER` + `NOW()`）・`GREATEST` ・`NULLIF` を使用。SQLite インメモリ DB では実行不可。テストには testcontainers 等で実 PostgreSQL が必要 |
| `dispatcher/scheduler.py::_cleanup_old_usage` | `CURRENT_DATE` 演算が PostgreSQL 固有。`fetch_dispatchable_jobs` 内で呼び出される |
| `dispatcher/scheduler.py::increment_retry` | `MAKE_INTERVAL(secs => :interval)` が PostgreSQL 固有関数 |
| `dispatcher/scheduler.py::fetch_stalled_jobs` | `NOW() - MAKE_INTERVAL(secs => :threshold)` が PostgreSQL 固有。`apply_gap_filling` テストではモックで代替 |
| `dispatcher/scheduler.py::estimate_shortest_remaining` | `EXTRACT(EPOCH FROM ...)` + `MAKE_INTERVAL` が PostgreSQL 固有。`apply_gap_filling` テストではモックで代替 |
| `api/routes.py` | FastAPI TestClient で テスト可能だが、services.py のテストでビジネスロジックはカバー済み。HTTP ステータスコード・認証・JSON シリアライズの検証が未実施 |
| `dispatcher/main.py` | メインループ。K8s `load_incluster_config()` やシグナルハンドリングに依存し、ユニットテストが困難 |
| `watcher/main.py` | 同上 |
| `cli/src/client.rs` | HTTP クライアント。テストには httpmock 等のモックライブラリ追加が必要 |

## 3. テスト基盤の技術的制約

### SQLite インメモリ DB の使用

Python テストは SQLite インメモリ DB（`sqlite:///:memory:`）を使用している。PostgreSQL 固有機能との互換性のために `conftest.py` で以下の対策を行っている。

- **JSONB → JSON 変換**: `jobs.env_json` と `job_events.payload_json` の JSONB 型を JSON に置換
- **BigInteger → Integer 変換**: `job_events.id` の BIGSERIAL（autoincrement）を SQLite 互換の Integer に変換
- **NOW() 関数の登録**: `mark_dispatched` / `mark_failed` 等の raw SQL で使われる `NOW()` を SQLite のユーザー定義関数として登録
- **allocate_job_id のモック**: `ON CONFLICT ... DO UPDATE ... RETURNING` 構文は PostgreSQL 固有のためモックで代替（`test_services.py` のみ）
- **JobEvent 挿入の抑制**: `test_services.py` では JobEvent の BIGSERIAL 問題を回避するため `session.add` をフィルタリング

これらの制約により、PostgreSQL 固有の raw SQL を含む関数（`fetch_dispatchable_jobs` / `increment_retry`）はテスト対象外となっている。

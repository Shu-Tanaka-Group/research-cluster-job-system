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
| `tests/test_determine_status.py` | `watcher/reconciler.py::determine_status` | 9 | K8s Job の conditions から DB ステータスへのマッピング。SUCCEEDED / FAILED / RUNNING / DeadlineExceeded / 条件なし等 |
| `tests/test_build_k8s_job.py` | `dispatcher/k8s_job.py::build_k8s_job` | 13 | K8s Job マニフェスト生成。ラベル / activeDeadlineSeconds / リソース / 環境変数 / ボリューム / コマンドラッピング等 |
| `tests/test_services.py` | `api/services.py` 全関数 | 32 | submit_job（time_limit バリデーション含む）/ list_jobs / get_job / cancel / delete / reset |
| `tests/test_reconciler.py` | `watcher/reconciler.py::reconcile_cycle` | 13 | ステータス同期（started_at / finished_at / last_error 記録）/ CANCELLED 削除 / orphan 検出 / DELETING フェーズ 1・2 / namespace 分離 |
| `tests/test_scheduler.py` | `dispatcher/scheduler.py` 4関数 | 13 | cas_update_to_dispatching / mark_dispatched / mark_failed / reset_stale_dispatching の CAS 動作・状態遷移 |
| **Rust** | | | |
| `src/job_ids.rs` | `parse_job_ids` | 7 | ジョブ ID 式のパース（単体 / 範囲 / リスト / 組み合わせ / 重複除去 / エラー） |
| `src/main.rs` | `parse_duration` | 8 | 時間指定のパース（秒数 / s / h / d サフィックス / 空白 / 不正値 / オーバーフロー） |
| `src/display.rs` | `format_duration` / `format_time_limit` | 9 | 時間表示フォーマット（日 / 時間 / 分）/ RUNNING 時の残り時間計算 / 非 RUNNING / 不正日付のフォールバック |

**合計: Python 80 + Rust 24 = 104 テスト**

### 未テスト

| 対象 | 理由 |
|---|---|
| `dispatcher/scheduler.py::fetch_dispatchable_jobs` | PostgreSQL 固有の CTE（`ROW_NUMBER() OVER` + `NOW()`）を使用。SQLite インメモリ DB では実行不可。テストには testcontainers 等で実 PostgreSQL が必要 |
| `dispatcher/scheduler.py::increment_retry` | `MAKE_INTERVAL(secs => :interval)` が PostgreSQL 固有関数 |
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

# テスト

## 1. テスト実行手順

### Python（Server）

```bash
cd server
uv run python -m pytest tests/ -v
```

初回実行時、`uv` が仮想環境の作成と依存インストールを自動で行う。
FastAPI の HTTPException を使うテストがあるため `fastapi` も必要（`uv pip install fastapi`）。

> **注意**: `uv run pytest` はエントリポイントスクリプトが見つからず `Failed to spawn: pytest` エラーになることがある。`uv run python -m pytest` を使うこと。

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
| `tests/test_build_k8s_job.py` | `dispatcher/k8s_job.py::build_k8s_job`, `_parse_taint` | 35 | K8s Job マニフェスト生成。ラベル / activeDeadlineSeconds / リソース / 環境変数 / ボリューム / コマンドラッピング / toleration（デフォルト・カスタム・空）/ CPU limit バッファ（乗数適用・メモリ非適用・GPU 非適用・ミリコア入力）/ taint パース（正常系・異常系） |
| `tests/test_services.py` | `api/services.py` 全関数 | 104 | submit_job（time_limit・リソース超過バリデーション・nominalQuota 考慮バリデーション・cpu_millicores / memory_mib 設定含む・RUNNING ジョブの MAX_QUEUED_JOBS_PER_NAMESPACE カウント除外）/ list_jobs（flavor・time_limit_ge・time_limit_lt フィルター含む）/ get_job / cancel（HELD 含む）/ hold_single・hold_bulk / release_single・release_bulk / delete（HELD skip 含む）/ reset（HELD ブロック含む）/ get_usage（ResourceQuota あり・なし・namespace 分離含む）/ submit_sweep（クラスタ合計・nominalQuota 考慮バリデーション含む）/ list_flavors（quota 有無・nodes と quota の同時取得）/ Prometheus カウンター（submit_job / submit_sweep で投入カウンター増加・cancel で完了カウンター増加・スキップ時は不変） |
| `tests/test_reconciler.py` | `watcher/reconciler.py` | 47 | reconcile_cycle のステータス同期（started_at / finished_at / last_error / node_name 記録・RUNNING スキップ時の完了時取得・既存値の非上書き）/ CANCELLED 削除 / orphan 検出 / DELETING フェーズ 1・2 / namespace 分離 / RUNNING 遷移時の累計消費量加算（namespace_daily_usage・flavor 別記録・異なる flavor の分離行）/ K8s Job 消失検出（DISPATCHED・RUNNING → FAILED 遷移・last_error・finished_at 設定）/ list_cjob_k8s_jobs の API エラー伝播・正常系 / parse_cpu_millicores / parse_memory_mib / Prometheus カウンター（SUCCEEDED / FAILED 遷移・K8s Job 消失で完了カウンター増加） |
| `tests/test_scheduler.py` | `dispatcher/scheduler.py` 6関数 | 37 | cas_update_to_dispatching / mark_dispatched / mark_failed / reset_stale_dispatching の CAS 動作・状態遷移 / mark_failed の Prometheus カウンター増加（成功時に increment・更新なし時は不変）/ filter_by_resource_quota の ResourceQuota 残リソースによる dispatch 候補フィルタリング（quota 行なし通過・CPU / メモリ / GPU 不足スキップ・sweep parallelism 倍計算・サイクル内累計追跡・namespace 混在・空リスト・count/jobs.batch 制限（NULL スキップ・充足通過・不足スキップ・累計追跡・sweep 1 カウント・リソース充足でも count 不足スキップ））/ fetch_dispatchable_jobs の fetch_limit パラメータ検証（DRF パス・フォールバックパス）・DRF 正規化の nominalQuota 考慮（nominalQuota による cap・allocatable 優先・quota なしフォールバック・複数 flavor 合算）・DRF flavor weight 適用（複数 flavor の重み付き容量計算） |
| `tests/test_gap_filling.py` | `dispatcher/scheduler.py::apply_gap_filling` | 17 | 隙間充填フィルタリング。無効時 / 滞留なし / 残り時間による候補選択 / RUNNING なし / namespace 混在 / 残り時間 0 / 候補なし / リソース超過 / quota 情報なし / 未知 flavor / 累積追跡 / sweep parallelism / RUNNING なし + リソース条件 / GPU リソース / cross-flavor 非干渉（GPU 滞留が CPU を阻害しない・flavor ごとの独立残り時間・flavor 単位フィルタ） |
| `tests/test_resource_utils.py` | `resource_utils.py` | 18 | CPU・メモリ文字列のパース。整数 / 小数 / ミリコア / Gi / Mi / Ki / Ti / milli-bytes / 10 進接頭辞(k, M, G, T) / 大きな値等 |
| `tests/test_node_sync.py` | `watcher/node_sync.py::sync_node_resources` | 26 | ノードリソース同期。挿入 / 更新 / 削除 / 全削除 / GPU パース / API エラー時のデータ保持 / ラベルセレクタ / 部分失敗時の失敗 flavor データ保持 / 部分失敗時の成功 flavor 古ノード削除 / DaemonSet Pod request の差し引き（単一 Pod・複数 Pod 合算・複数コンテナ合算・非 DaemonSet Pod 除外・オーナー参照なし Pod 除外・Succeeded/Failed/Unknown phase 除外・Pending phase 計上・requests 未設定コンテナ 0 扱い・0 クランプ・複数ノード独立集計・Pod 取得 API エラー時のデータ保持・GPU 非適用） |
| `tests/test_quota_sync.py` | `watcher/quota_sync.py::sync_flavor_quotas` | 7 | flavor quota 同期。挿入 / 複数 flavor / 更新 / 削除 / API エラー時のデータ保持 / 空 resourceGroups / ClusterQueue 名設定 |
| `tests/test_resource_quota_sync.py` | `watcher/resource_quota_sync.py::sync_resource_quotas` | 16 | ResourceQuota 同期。ユーザー namespace への挿入 / 値更新 / ユーザー namespace 除去時の行削除 / ResourceQuota なし時の行削除 / namespace 一覧 API エラー時のデータ保持 / ResourceQuota 一覧 API エラー時のデータ保持 / ユーザー namespace なしの全削除 / CPU・メモリパース / GPU リソース名取得 / field_selector 設定 / USER_NAMESPACE_LABEL 設定 / 非ユーザー namespace の除外 / ジョブなし namespace の追跡 / count/jobs.batch 同期（値あり・値なし NULL・再同期時更新） |
| `tests/test_cli_endpoints.py` | `api/routes.py` CLI 配布エンドポイント | 17 | `/v1/cli/version`・`/v1/cli/versions`・`/v1/cli/download` の正常系 / 404 / 認証不要 / バージョンソート / バージョン指定ダウンロード / 無効ディレクトリ除外 / 不正バージョン文字列の拒否（パストラバーサル防止）の検証 |
| `tests/test_cluster_totals.py` | `dispatcher/scheduler.py::_fetch_cluster_totals` | 6 | DRF 正規化用クラスタ合計取得。空テーブル / 単一ノード / 複数ノード合計 / drf_weight による容量スケーリング / 複数 flavor の重み付き合計 / quota なし時のデフォルト weight |
| **Rust** | | | |
| `src/job_ids.rs` | `parse_job_ids` | 7 | ジョブ ID 式のパース（単体 / 範囲 / リスト / 組み合わせ / 重複除去 / エラー） |
| `src/main.rs` | `parse_duration` / `parse_time_limit_range` | 23 | 時間指定のパース（秒数 / s / m / h / d サフィックス / 空白 / 不正値 / オーバーフロー）/ time_limit 範囲指定のパース（h / m / d / 混合単位 / 秒数 / 片端省略 / コロンなし / 空範囲 / 不正値 / 下限≧上限エラー） |
| `src/display.rs` | `format_duration` / `format_time_limit` | 9 | 時間表示フォーマット（日 / 時間 / 分）/ RUNNING 時の残り時間計算 / 非 RUNNING / 不正日付のフォールバック |
| **Rust (cjobctl)** | | | |
| `src/cmd/cli_deploy.rs` | `run`（バリデーション） | 4 | --release + プレリリースのエラー / 安定版・プレリリース × release フラグのバリデーション順序 |
| `src/cmd/cli_list.rs` | `parse_versions` / `sort_versions` | 9 | ls 出力パース（latest 除外 / 空入力 / パース不能エントリ）/ ソート（降順 / プレリリース優先 / 設計書出力例の再現） |
| `src/cmd/cli_set_latest.rs` | `run`（バリデーション） | 2 | プレリリース版の拒否（beta / rc） |

**合計: Python 358 + Rust (cli) 62 + Rust (cjobctl) 28 = 448 テスト**

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
| `ctl/src/cmd/usage.rs::quota` | DB SELECT + K8s namespace 一覧の突き合わせ表示のみ。テスト可能な純粋関数なし |

## 3. テスト基盤の技術的制約

### SQLite インメモリ DB の使用

Python テストは SQLite インメモリ DB（`sqlite:///:memory:`）を使用している。PostgreSQL 固有機能との互換性のために `conftest.py` で以下の対策を行っている。

- **JSONB → JSON 変換**: `jobs.env_json` と `job_events.payload_json` の JSONB 型を JSON に置換
- **BigInteger → Integer 変換**: `job_events.id` の BIGSERIAL（autoincrement）を SQLite 互換の Integer に変換
- **NOW() 関数の登録**: `mark_dispatched` / `mark_failed` 等の raw SQL で使われる `NOW()` を SQLite のユーザー定義関数として登録
- **allocate_job_id のモック**: `ON CONFLICT ... DO UPDATE ... RETURNING` 構文は PostgreSQL 固有のためモックで代替（`test_services.py` のみ）
- **JobEvent 挿入の抑制**: `test_services.py` では JobEvent の BIGSERIAL 問題を回避するため `session.add` をフィルタリング

これらの制約により、PostgreSQL 固有の raw SQL を含む関数（`fetch_dispatchable_jobs` / `increment_retry`）はテスト対象外となっている。

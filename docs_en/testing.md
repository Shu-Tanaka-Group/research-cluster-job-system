> *This document was auto-translated from the [Japanese original](../docs/testing.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Testing

## 1. How to Run Tests

### Python (Server)

```bash
cd server
uv run --extra test --extra api --with httpx python -m pytest tests/ -v
```

On the first run, `uv` automatically creates a virtual environment and installs dependencies. Running the tests requires the following in addition to the base dependencies.

| Dependency | How to specify | Why it is needed |
|---|---|---|
| `pytest` | `--extra test` | Test runner |
| `fastapi` | `--extra api` | Some tests use FastAPI's HTTPException |
| `httpx` | `--with httpx` | Required by Starlette's `TestClient`, which `tests/test_cli_endpoints.py` uses. It is not included in any `pyproject.toml` extra |

Without `httpx`, the whole run aborts at collection time with `RuntimeError: The starlette.testclient module requires the httpx package to be installed.`

> **Note**: Installing packages individually with `uv pip install` does not persist; they are lost when `uv run` re-syncs the virtual environment. Specify them with `--extra` / `--with` as shown above.

> **Note**: `uv run pytest` may fail with a `Failed to spawn: pytest` error because the entry point script cannot be found. Use `uv run python -m pytest` instead.

### Rust (CLI)

```bash
cd cli
cargo test
```

No additional dependency installation is required.

## 2. Integration Tests (PostgreSQL)

### Prerequisites

- A Docker-compatible runtime (Docker Desktop / Colima) must be running
- When using Colima, the following environment variables are required:
  ```bash
  export DOCKER_HOST=unix://$HOME/.config/colima/default/docker.sock
  export TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock
  ```

### How to Run

```bash
cd server

# Integration tests only
uv run --extra integration --extra api --with httpx python -m pytest -m integration -v

# Unit tests + integration tests
uv run --extra integration --extra api --with httpx python -m pytest -v
```

Integration tests are automatically skipped in environments where Docker is not available.

### How It Works

- `testcontainers[postgres]` automatically starts a PostgreSQL container (`postgres:16-alpine`) at the beginning of the test session
- Each test isolates data via transaction rollback (no need to restart the container)
- The container is automatically destroyed at the end of the test session

## 3. Test Coverage

### Tested

| Test File | Target | # Tests | Summary |
|---|---|---|---|
| **Python** | | | |
| `tests/test_auth.py` | `api/auth.py::extract_bearer` | 7 | Bearer token extraction. Happy path / no header / empty header / non-Bearer scheme / lowercase bearer, etc. |
| `tests/test_determine_status.py` | `watcher/reconciler.py::determine_status` | 13 | Mapping from K8s Job conditions to DB status. SUCCEEDED / FAILED / RUNNING / DeadlineExceeded / no conditions, etc. |
| `tests/test_build_k8s_job.py` | `dispatcher/k8s_job.py` | 45 | K8s Job manifest generation. Labels / activeDeadlineSeconds not set (regular / sweep) / resources / environment variables / volumes / command wrapping / tolerations (default / custom / empty) / CPU limit buffer (multiplier applied / not applied to memory / not applied to GPU / millicores input) / taint parsing (happy path / error cases) / `_extract_k8s_error_message` body parsing (message extraction / fallback to reason / invalid JSON / dict without message) / `create_k8s_job` ApiException classification (403 + exceeded quota → QuotaExceededError / 403 RBAC → PermanentK8sError including message / 429/503 → TemporaryK8sError / 403 without body → PermanentK8sError / 422 → PermanentK8sError) |
| `tests/test_services.py` | All functions in `api/services.py` | 152 | submit_job (time_limit / resource-exceeded validation / nominalQuota-aware validation / cpu_millicores / memory_mib settings / exclusion of RUNNING jobs from MAX_QUEUED_JOBS_PER_NAMESPACE count) / list_jobs (including flavor / time_limit_ge / time_limit_lt filters) / get_job / cancel (including HELD) / hold_single & hold_bulk / release_single & release_bulk / set_single & set_bulk (changing each parameter for QUEUED/HELD / skipping RUNNING/SUCCEEDED/CANCELLED / not_found / invalid flavor rejection / GPU-unsupported flavor rejection / resource-exceeded rejection / time_limit-exceeded rejection / simultaneous multi-field changes / preservation of unspecified fields / cpu_millicores/memory_mib update / post-merge validation / bulk operations) / delete (including HELD skip) / reset (including HELD block) / get_usage (with/without ResourceQuota / namespace isolation) / submit_sweep (including cluster total / nominalQuota-aware validation) / list_flavors (with/without quota / simultaneous node and quota retrieval / flavor default image returned) / image resolution (`--image` precedence / flavor default over fallback / fallback when the flavor has no default / `DEFAULT_FLAVOR` default applied / 400 when nothing resolves / empty string treated as unspecified / old-CLI compatibility (sending only `image`) / flavor default applied for sweep / 400 for sweep) / image re-resolution on set (explicit `--image` / `--image` taking precedence over the flavor default / re-resolution on flavor change / kept when the new flavor has no default / kept when the flavor is unchanged / no change reported when the value is identical / recorded in the `SET` event / not reported on skip / single value reported for bulk / None for bulk when unchanged) / image returned by get_job / Prometheus counters (submission counter incremented on submit_job / submit_sweep / completion counter incremented on cancel / unchanged on skip) |
| `tests/test_reconciler.py` | `watcher/reconciler.py` | 118 | reconcile_cycle status sync (recording started_at / finished_at / last_error / node_name / obtaining completion time when skipping RUNNING / no overwrite of existing values) / CANCELLED deletion / orphan detection / DELETING phases 1 & 2 / namespace isolation / cumulative consumption addition on RUNNING transition (namespace_daily_usage / per-flavor recording / separate rows for different flavors) / completion fallback for usage recording when RUNNING is skipped (SUCCEEDED / FAILED / sweep parallelism applied / no duplicate for jobs that observed RUNNING) / K8s Job disappearance detection (DISPATCHED/RUNNING → FAILED transition / last_error / finished_at set / DISPATCHED within dispatch grace period spared / FAILED after grace expires / legacy behaviour when dispatched_at is NULL / RUNNING not subject to grace) / terminal state regression guard (blocking FAILED / SUCCEEDED → RUNNING / allowing DISPATCHED → RUNNING forward path) / time limit enforcement (FAILED transition for jobs past the limit / last_error / finished_at / K8s Job deletion / job_events record / failed counter increment / no double-counting of usage / jobs within the limit untouched / naive timestamps read as UTC / job stays RUNNING when deletion fails / started_at NULL not targeted / terminal states not targeted / jobs completed in the same cycle protected / jobs transitioning to RUNNING in the same cycle protected / k8s_job_name fallback to k8s_map / skipped when the name is unknown / sweep jobs / only over-limit jobs affected) / DISPATCHED stall guard (requeue to QUEUED for jobs past the threshold / K8s Job deletion / UNSCHEDULABLE event and payload / requeue counter increment / first backoff / doubling on the second requeue / clamping at the ceiling / exponent clamped for a very large attempt count / retry_count not incremented / no usage recorded / jobs within the threshold not targeted / dispatched_at NULL not targeted / delegated to Step 8 when the K8s Job is absent / job stays DISPATCHED when deletion fails / k8s_job_name fallback to k8s_map / jobs transitioning to RUNNING in the same cycle protected / jobs completed in the same cycle protected / RUNNING jobs not targeted / disabled when the threshold is 0 / naive timestamps read as UTC / only stalled jobs affected) / list_cjob_k8s_jobs API error propagation & pagination (`_continue` token propagation / skipping jobs with invalid labels) / LightK8sJob extraction / NamespacePodNodeResolver per-namespace Pod cache (single-fetch reuse / cached empty result on API error / empty result for unknown job-name) / parse_cpu_millicores / parse_memory_mib / Prometheus counters (completion counter incremented on SUCCEEDED/FAILED transition and K8s Job disappearance) |
| `tests/test_scheduler.py` | 6 functions in `dispatcher/scheduler.py` | 37 | cas_update_to_dispatching / mark_dispatched / mark_failed / reset_stale_dispatching CAS behavior & state transitions / mark_failed Prometheus counter increment (incremented on success / unchanged when no update) / filter_by_resource_quota filtering of dispatch candidates by remaining ResourceQuota (no quota row passes / insufficient CPU/memory/GPU skipped / sweep parallelism multiplied / cycle-level cumulative tracking / mixed namespaces / empty list / count/jobs.batch limit (NULL skipped / sufficient passes / insufficient skipped / cumulative tracking / sweep counts as 1 / resource-sufficient but count-insufficient skipped)) / fetch_dispatchable_jobs fetch_limit parameter validation (DRF path / fallback path) / per-flavor DRF capacity with nominalQuota consideration (capped by nominalQuota / allocatable preferred / no-quota fallback / individual parameters per flavor) / DRF flavor weight per-flavor parameter validation (weight passed as separate field without being multiplied into capacity) |
| `tests/test_gap_filling.py` | `dispatcher/scheduler.py::apply_gap_filling` | 17 | Gap-filling filter. Disabled / no stalled jobs / candidate selection by remaining time / no RUNNING jobs / mixed namespaces / remaining time 0 / no candidates / resource exceeded / no quota info / unknown flavor / cumulative tracking / sweep parallelism / no RUNNING + resource conditions / GPU resources / cross-flavor non-interference (GPU stall does not block CPU / independent remaining time per flavor / per-flavor filter) |
| `tests/test_node_bin_packing.py` | `dispatcher/scheduler.py::filter_by_node_capacity` | 27 | Per-node bin-packing pre-check. Disable flag / pass-through when `node_resources` is empty / pass-through when flavor is absent / single-node fit & no-fit / multi-node fit / GPU / memory check / same-cycle cumulative tracking (least-loaded spread) / RUNNING job subtraction (normal node_name / listed nodes only / least-loaded fallback when `node_name` is NULL / even sweep distribution / with remainder) / in-flight subtraction (least-loaded provisional placement for DISPATCHING / DISPATCHED / sweep parallelism distribution) / sweep admit (admit when all pods fit / reject on partial fit / consolidation onto a single node) / flavor isolation / issue #203 scenario reproduction (rejection of the third job under 300Gi × 2 nodes pressure / passing when capacity is freed) / mixed namespaces |
| `tests/test_resource_utils.py` | `resource_utils.py` | 18 | Parsing CPU and memory strings. Integer / decimal / millicores / Gi / Mi / Ki / Ti / milli-bytes / decimal prefixes (k, M, G, T) / large values, etc. |
| `tests/test_node_sync.py` | `watcher/node_sync.py::sync_node_resources` | 27 | Node resource sync. Insert / update / delete / delete all / GPU parsing / data preservation on API error / label selector / failed-flavor data preservation on partial failure / old-node deletion for successful flavors on partial failure / DaemonSet Pod request subtraction (single Pod / multiple Pods summed / multiple containers summed / non-DaemonSet Pods excluded / Pods without owner references excluded / Succeeded/Failed/Unknown phases excluded / Pending phase counted / containers with no requests treated as 0 / clamped to 0 / independent aggregation per node / data preservation on Pod retrieval API error / not applied to GPU / paginated fetch via `_continue` token) |
| `tests/test_quota_sync.py` | `watcher/quota_sync.py::sync_flavor_quotas` | 7 | Flavor quota sync. Insert / multiple flavors / update / delete / data preservation on API error / empty resourceGroups / ClusterQueue name setting |
| `tests/test_resource_quota_sync.py` | `watcher/resource_quota_sync.py::sync_resource_quotas` | 16 | ResourceQuota sync. Insert into user namespace / value update / row deletion when user namespace is removed / row deletion when no ResourceQuota / data preservation on namespace list API error / data preservation on ResourceQuota list API error / delete all when no user namespaces / CPU & memory parsing / GPU resource name retrieval / field_selector setting / USER_NAMESPACE_LABEL setting / exclusion of non-user namespaces / tracking of namespaces with no jobs / count/jobs.batch sync (with value / NULL when no value / update on re-sync) |
| `tests/test_cli_endpoints.py` | CLI distribution endpoints in `api/routes.py` | 17 | Validation of `/v1/cli/version` / `/v1/cli/versions` / `/v1/cli/download` happy paths / 404 / no authentication required / version sorting / version-specified download / exclusion of invalid directories / rejection of invalid version strings (path traversal prevention) |
| `tests/test_config.py` | `config.py::FlavorDefinition` / `Settings.flavors` | 10 | Flavor definition schema. Required fields only / with GPU / with `image` / `image` defaulting to None / `image` given as null / rejection of unknown fields (`extra="forbid"`) / missing required field / JSON parsing of `RESOURCE_FLAVORS` and `get_flavor_definition` / failure when accessing `flavors` with an unknown field / validity of the default value |
| `tests/test_cluster_totals.py` | `dispatcher/scheduler.py::_fetch_flavor_caps` | 6 | Per-flavor capacity retrieval for DRF normalization. Empty table / single node / multiple nodes summed / capacity cap by nominalQuota (weight not multiplied into capacity, passed as separate field) / individual capacity per flavor / default weight when no quota |
| `tests/test_integration_scheduler.py` | 6 functions in `dispatcher/scheduler.py` (integration tests) | 33 | Integration tests using a PostgreSQL container. `_cleanup_old_usage` (deletion outside retention period / boundary date) / `increment_retry` (retry_after setting / retry_count increment / no-op for non-DISPATCHING) / `defer_to_queue` (DISPATCHING → QUEUED transition / retry_after in future / retry_count preserved / no-op for non-DISPATCHING / CANCELLED preserved / DEFERRED JobEvent recorded / no event when no update) / `fetch_stalled_jobs` (detection exceeding threshold / exclusion of recent dispatches / exclusion of non-DISPATCHED / detection of QUEUED jobs requeued by the stall guard and waiting out their backoff / exclusion of backoffs with unschedulable_count 0 / exclusion once the backoff has elapsed / exclusion of QUEUED jobs without retry_after) / `estimate_shortest_remaining` (shortest remaining time / no RUNNING / namespace/flavor scope) / `fetch_dispatchable_jobs` (fallback namespace order / budget exceeded / DRF usage priority / round-robin / weight amplification / flavor budget / weight=0 excluded / in-flight score reflected / in-flight BIGINT overflow avoidance / retry_after exclusion / automatic deletion of old usage) |
| **Rust** | | | |
| `src/job_ids.rs` | `parse_job_ids` | 7 | Parsing job ID expressions (single / range / list / combination / deduplication / error) |
| `src/config.rs` | User configuration file | 10 | Reading and writing the configuration file / operations on the environment variable exclusion list |
| `src/main.rs` | `parse_duration` / `parse_time_limit_range` / `shell_quote` / `pick_fallback_image`, etc. | 39 | Parsing time specifications (seconds / s / m / h / d suffixes / whitespace / invalid values / overflow) / parsing time_limit range specifications (h / m / d / mixed units / seconds / open-ended / no colon / empty range / invalid values / lower-bound >= upper-bound error) / obtaining the submitting Pod image (`CJOB_IMAGE` preferred / `JUPYTER_IMAGE` fallback / empty string treated as unset / None when both are unset) |
| `src/display.rs` | `format_duration` / `format_time_limit` | 16 | Time display formatting (days / hours / minutes) / remaining time calculation for RUNNING / non-RUNNING / fallback for invalid dates |
| **Rust (cjobctl)** | | | |
| `src/cmd/cli_deploy.rs` | `run` (validation) | 4 | Error on --release + pre-release / validation order for stable and pre-release × release flag |
| `src/cmd/cli_list.rs` | `parse_versions` / `sort_versions` | 9 | Parsing ls output (latest excluded / empty input / unparseable entries) / sorting (descending / pre-release priority / reproducing the design doc output example) |
| `src/cmd/cli_set_latest.rs` | `run` (validation) | 2 | Rejection of pre-release versions (beta / rc) |
| `src/cmd/config/set.rs` | `validate_resource_flavors` / `validate_against_configmap` | 30 | Structural validation of `RESOURCE_FLAVORS`. Success cases (names returned in definition order / `gpu_resource_name` omitted or null / `image` specified, omitted, or null / multi-line ConfigMap value) / invalid JSON / non-array / empty array / non-object element / required field missing, empty, or non-string / empty `gpu_resource_name` / empty or non-string `image` / unknown field / duplicate `name` / `label_selector` form (no `=`, two `=`, one side empty) / all violations reported together. Consistency with `DEFAULT_FLAVOR` (when setting `RESOURCE_FLAVORS`: match, mismatch, warning when unset, surrounding whitespace ignored; when setting `DEFAULT_FLAVOR`: match, mismatch, warning and skip when `RESOURCE_FLAVORS` is unset or broken) / unrelated keys pass through |

**Total: Python 515 + Python integration 33 + Rust (cli) 72 + Rust (cjobctl) 58 = 678 tests**

### Not Tested

| Target | Reason |
|---|---|
| `api/routes.py` | Testable with FastAPI TestClient, but business logic is already covered by services.py tests. HTTP status codes, authentication, and JSON serialization are not validated. |
| `dispatcher/main.py` | Main loop. Depends on K8s `load_incluster_config()` and signal handling, making unit testing difficult. |
| `watcher/main.py` | Same as above. |
| `cli/src/client.rs` | HTTP client. Testing requires adding a mock library such as httpmock. |
| `ctl/src/cmd/usage.rs::quota` | Only displays DB SELECT + K8s namespace list cross-referenced results. No pure functions suitable for testing. |

## 3. Technical Constraints of the Test Infrastructure

### Use of SQLite In-Memory DB

Python tests use a SQLite in-memory DB (`sqlite:///:memory:`). The following measures are applied in `conftest.py` to ensure compatibility with PostgreSQL-specific features.

- **JSONB → JSON conversion**: The JSONB type of `jobs.env_json` and `job_events.payload_json` is replaced with JSON.
- **BigInteger → Integer conversion**: The BIGSERIAL (autoincrement) of `job_events.id` is converted to a SQLite-compatible Integer.
- **Registering the NOW() function**: `NOW()` used in raw SQL for `mark_dispatched` / `mark_failed`, etc. is registered as a SQLite user-defined function.
- **Mocking allocate_job_id**: The `ON CONFLICT ... DO UPDATE ... RETURNING` syntax is PostgreSQL-specific and is replaced with a mock (only in `test_services.py`).
- **Suppressing JobEvent insertion**: In `test_services.py`, `session.add` is filtered to work around the BIGSERIAL issue with JobEvent.

Due to these constraints, functions containing PostgreSQL-specific raw SQL are excluded from SQLite tests. These functions are covered by integration tests using `testcontainers[postgres]` (§2).

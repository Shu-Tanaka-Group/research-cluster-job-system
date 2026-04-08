> *This document was auto-translated from the [Japanese original](../../docs/architecture/cli.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# CLI Design

## 1. Basic Commands

```bash
cjob add [--cpu <cpu>] [--memory <memory>] [--flavor <name>] [--gpu <N>] [--time-limit <duration>] -- <command...>
cjob sweep -n <count> --parallel <n> [--flavor <name>] [--gpu <N>] [--time-limit <duration>] -- <command...>
cjob list [--status <status>] [--flavor <name>] [--time-limit <range>] [--format ids] [--limit <n>] [--all] [--reverse]
cjob status <job-id>
cjob cancel <job-id>              # Single specification
cjob cancel <start>-<end>         # Range specification (e.g., 1-10)
cjob cancel <id>,<id>,...         # Multiple specification (e.g., 1,3,5)
cjob cancel <start>-<end>,<id>,.. # Combination (e.g., 1-5,8,10-12)
cjob delete <job-id>              # Single specification
cjob delete <start>-<end>         # Range specification (e.g., 1-10)
cjob delete <id>,<id>,...         # Multiple specification (e.g., 1,3,5)
cjob delete <start>-<end>,<id>,.. # Combination (e.g., 1-5,8,10-12)
cjob delete --all                 # Delete all completed jobs
cjob hold <job-id>                # Single specification
cjob hold <start>-<end>           # Range specification (e.g., 1-10)
cjob hold <id>,<id>,...           # Multiple specification (e.g., 1,3,5)
cjob hold <start>-<end>,<id>,..   # Combination (e.g., 1-5,8,10-12)
cjob hold --all                   # Hold all QUEUED jobs
cjob release <job-id>             # Single specification
cjob release <start>-<end>        # Range specification (e.g., 1-10)
cjob release <id>,<id>,...        # Multiple specification (e.g., 1,3,5)
cjob release <start>-<end>,<id>,.. # Combination (e.g., 1-5,8,10-12)
cjob release --all                # Release all HELD jobs
cjob set <job-ids> [--cpu <cpu>] [--memory <memory>] [--flavor <name>] [--gpu <N>] [--time-limit <duration>]
cjob reset
cjob logs <job-id>
cjob logs --follow <job-id>
cjob logs <job-id> --index <n>           # sweep: Display log for a specific index
cjob logs --follow <job-id> --index <n>  # sweep: Follow log for a specific index
cjob usage
cjob flavor list                         # List available flavors
cjob flavor info <name>                  # Resource limits for a specified flavor
cjob update
cjob config list                              # Display all settings
cjob config add <table> <key> <value>         # Add element to list-type setting
cjob config remove <table> <key> <value>      # Remove element from list-type setting
cjob config set <table> <key> <value>         # Change scalar-type setting value
cjob config unset <table> <key>               # Delete scalar-type setting value
```

## 2. Usage Examples

### 2.1 Submitting a Single Job

```bash
cjob add -- python main.py --alpha 0.1 --beta 16

# Submitting a GPU job (specifying flavor)
cjob add --flavor gpu-a100 --gpu 1 -- python train.py --epochs 100
```

### 2.2 Executing a Shell Script

```bash
cjob add -- bash run_experiment.sh case001
```

### 2.3 Execution with a Virtual Environment

```bash
source /home/jovyan/myenv/bin/activate
cjob add -- python main.py --config config.yaml
# PATH / VIRTUAL_ENV are already exported, so the venv is reproduced in the Job Pod
```

### 2.4 Parameter Sweep

```bash
# Execute 100 tasks with parallelism of 10
cjob sweep -n 100 --parallel 10 -- python main.py --trial _INDEX_

# With time limit
cjob sweep -n 50 --parallel 5 --time-limit 6h -- bash run.sh
```

Each task is identified by the `_INDEX_` placeholder (0-origin, 0 to completions-1). `_INDEX_` is replaced with the actual index value (`$CJOB_INDEX`) at Job Pod execution time.

### 2.5 Listing Jobs

```bash
cjob list
```

### 2.6 Checking Status

```bash
cjob status <job-id>
```

### 2.7 Cancelling

```bash
cjob cancel <job-id>
```

### 2.8 Modifying Parameters

Modify parameters of QUEUED / HELD jobs. Only the specified options are updated; unspecified items retain their current values.

```bash
# Change flavor
cjob set <job-ids> --flavor cpu-sub

# Change resources
cjob set <job-ids> --cpu 4 --memory 16Gi

# Change time-limit
cjob set <job-ids> --time-limit 12h

# Change multiple parameters simultaneously
cjob set <job-ids> --flavor cpu-sub --cpu 4 --memory 16Gi --time-limit 12h

# Comma-separated and range specification
cjob set 10,11,12 --flavor cpu-sub
cjob set 10-20,25,30 --cpu 8

# Integration with cjob list --format ids
cjob set $(cjob list --status QUEUED --flavor cpu --format ids) --flavor cpu-sub
```

Modifiable statuses are QUEUED / HELD only. Jobs from DISPATCHING onward cannot be modified (skipped) as their K8s Jobs have already been created.
An error is returned if no options are specified.
Validation rules are the same as `cjob add` (flavor existence check, resource limits, GPU compatibility, time-limit range).

### 2.9 Retrieving Logs

```bash
# Check after completion
cjob logs <job-id>

# Real-time tracking
cjob logs --follow <job-id>
```

### 2.10 Deleting Completed Jobs

```bash
# Single specification
cjob delete 5

# Range and multiple specification
cjob delete 1-5
cjob delete 1,3,5
cjob delete 1-5,8,10-12

# Delete all completed jobs (running jobs are skipped)
cjob delete --all
```

## 3. `cjob sweep` Behavior

1. Like `cjob add`, collects `pwd`, exported environment variables, and `CJOB_IMAGE` / `JUPYTER_IMAGE` (exits with error if neither is set)
2. Joins the argv after `--` in a shell-safe manner to generate the command
3. Sends `-n` as `completions` and `--parallel` as `parallelism` to `POST /v1/sweep`
4. Displays `job_id`, task count, and parallelism

### Arguments

| Argument | Required | Description |
|---|---|---|
| `-n <count>` | Required | Total number of tasks (completions). Upper limit is server-side `MAX_SWEEP_COMPLETIONS` (default 1000) |
| `--parallel <n>` | Optional | Concurrency (parallelism). Default 1 |
| `--time-limit <duration>` | Optional | Time limit for the **entire** sweep. Server-side default when omitted |
| `--cpu <cpu>` | Optional | CPU resource. Default "1" |
| `--memory <memory>` | Optional | Memory resource. Default "1Gi" |
| `--gpu <N>` | Optional | Number of GPUs. Default 0 (no GPU) |
| `--flavor <name>` | Optional | ResourceFlavor name (e.g., "cpu", "gpu-a100"). Server-side default when omitted |
| `-- <command>` | Required | Command to execute for each task |

### `_INDEX_` Placeholder

`_INDEX_` in the command is replaced by the CLI with the `$CJOB_INDEX` shell variable before sending to the Submit API. At Job Pod execution time, the `CJOB_INDEX` environment variable (= K8s `JOB_COMPLETION_INDEX`) is expanded to become each task's unique index value.

- 0-origin (same as K8s `JOB_COMPLETION_INDEX`)
- Value range: `0` to `completions - 1`

Within script files, the `$CJOB_INDEX` environment variable can be referenced directly. Since the contents of script files are not subject to shell expansion by the user's shell, `$CJOB_INDEX` can be written directly without using the `_INDEX_` placeholder.

```bash
# run.sh
echo "index is $CJOB_INDEX"
python main.py --trial $CJOB_INDEX
```

```bash
cjob sweep -n 10 --parallel 5 -- bash run.sh
```

## 4. `cjob add` Behavior

### Arguments

| Argument | Required | Description |
|---|---|---|
| `--cpu <cpu>` | Optional | CPU resource. Default "1" |
| `--memory <memory>` | Optional | Memory resource. Default "1Gi" |
| `--gpu <N>` | Optional | Number of GPUs. Default 0 (no GPU) |
| `--flavor <name>` | Optional | ResourceFlavor name (e.g., "cpu", "gpu-a100"). Server-side default when omitted |
| `--time-limit <duration>` | Optional | Execution time limit. Server-side default when omitted |
| `-- <command>` | Required | Command to execute |

### Behavior

1. Get `pwd`
2. Collect exported environment variables (including `PATH` / `VIRTUAL_ENV`)
3. Get container image name from the `CJOB_IMAGE` environment variable (falls back to `JUPYTER_IMAGE` if not set; exits with error if neither is set)
4. Join the argv after `--` in a shell-safe manner to generate the command
5. If `--time-limit` is specified, convert to seconds (uses API default value when omitted)
6. Read ServiceAccount JWT and namespace from fixed paths
7. Submit job to the API (including `image` and `time_limit_seconds` fields)
8. Display `job_id`

### `--time-limit` Option

Specifies the execution time limit. Server-side default (24 hours) is applied when omitted.

```bash
cjob add --time-limit 3600 -- python main.py    # Specify in seconds
cjob add --time-limit 1h -- python main.py       # 1 hour
cjob add --time-limit 6h -- python main.py       # 6 hours
cjob add --time-limit 1d -- python main.py       # 1 day
cjob add --time-limit 3d -- python main.py       # 3 days
```

Accepted formats: integer (seconds), `<number>s` (seconds), `<number>m` (minutes), `<number>h` (hours), `<number>d` (days). Maximum value is limited by the server-side `MAX_TIME_LIMIT_SECONDS` (default 604800 = 7 days).

## 5. `cjob logs` Behavior

`cjob logs` is dedicated to log viewing. Log deletion is handled by `cjob delete` or `cjob reset`.

Behavior varies by job state:

| State | Behavior |
|---|---|
| QUEUED / DISPATCHING / DISPATCHED | Without `--follow`: Displays "not yet started" and suggests using `--follow`, then exits. With `--follow`: Waits up to 5 minutes (displays state and elapsed time during wait) |
| HELD | No logs because the job is held. Displays "Job is held" and suggests releasing with `cjob release` |
| RUNNING | Follows with tail -f after file is created (when `--follow` is specified) |
| SUCCEEDED / FAILED | Displays the entire file and exits |
| CANCELLED | Displays file if available, otherwise "No logs available" |
| DELETING | Reset in progress. Displays file if available, otherwise "No logs available (reset in progress)" and exits |

Log files are on PVC and read directly by the CLI. The log directory path uses `log_dir` obtained from `GET /v1/jobs/{job_id}`.

### Behavior During QUEUED / DISPATCHING / DISPATCHED

Without `--follow`, notifies that the job has not yet started, suggests using `--follow`, and exits immediately.

```
$ cjob logs 3
Job 3 has not started yet. (QUEUED)
Use `cjob logs --follow 3` to follow the log.
```

With `--follow`, polls `GET /v1/jobs/{job_id}` every few seconds, displaying state and elapsed time. If the job does not start within 5 minutes, displays a timeout message and exits.

```
$ cjob logs --follow 3
Waiting for job 3 to start... (QUEUED) [0:00:12]
Waiting for job 3 to start... (DISPATCHING) [0:00:25]
Waiting for job 3 to start... (DISPATCHED) [0:00:48]
Job 3 has started. Following log.
<log output>
```

```
$ cjob logs --follow 3   # When job doesn't start within 5 minutes
Waiting for job 3 to start... (DISPATCHED) [5:00:00]
Timed out. Job is still in DISPATCHED state.
Check status with `cjob status 3`.
```

### `--follow` Exit Conditions

`--follow` mode is explicitly terminated by the user with Ctrl-C. It does not automatically exit when the job transitions to `SUCCEEDED` / `FAILED` / `CANCELLED`.

However, when `--follow` is not specified (normal `cjob logs`) and the job is already in a terminal state, the entire file is displayed and the command exits.

```
$ cjob logs --follow 3
<log output in progress>
^C      ← User exits with Ctrl-C
```

## 6. `cjob list` Behavior

Calls `GET /v1/jobs` and displays results in table format. By default, displays the latest 50 entries sorted by JOB_ID in ascending order.

```
$ cjob list
JOB_ID  TYPE   STATUS      FLAVOR      PROGRESS    COMMAND                              CREATED              FINISHED
51      job    SUCCEEDED   cpu         -           python main.py --alpha 0.1 --beta 16 2026-03-23 12:34     2026-03-23 12:37
52      job    RUNNING     cpu         -           python main.py --alpha 0.2 --beta 16 2026-03-23 12:35     -
53      sweep  RUNNING     gpu-a100    48/2/100    python main.py --trial $CJOB_INDEX   2026-03-23 12:35     -
54      sweep  SUCCEEDED   gpu-a100    98/2/100    python main.py --trial $CJOB_INDEX   2026-03-23 12:36     2026-03-23 13:00
(Showing latest 50 of 100 jobs. Use --all to display all.)
```

The TYPE column shows `job` for regular jobs and `sweep` for sweep jobs. The PROGRESS column shows `succeeded/failed/total` for sweep jobs and `-` for regular jobs.

Options:

- `--status <status>`: Show only jobs with the specified status (e.g., `--status RUNNING`)
- `--flavor <name>`: Show only jobs with the specified flavor (e.g., `--flavor gpu-a100`). Sent as the `flavor` parameter to the API
- `--time-limit <range>`: Filter by time_limit_seconds range. Specified in `<min>:<max>` format. `<min>` is inclusive ("or more"), `<max>` is exclusive ("less than"). Either side can be omitted (e.g., `6h:12h`, `:12h`, `6h:`). Duration format is the same as `cjob add --time-limit` (integer seconds, `<number>s/m/h/d`). Converted to seconds by the CLI and sent as `time_limit_ge` / `time_limit_lt` parameters to the API
- `--format ids`: Output job IDs comma-separated (e.g., `1,3,5,8`). Outputs only IDs instead of table display, usable as input to other subcommands. Outputs nothing if no matching jobs
- `--limit <n>`: Limit display to the latest n entries (1 or more). Default 50 when omitted. Sends value to the API's `limit` parameter
- `--all`: Display all entries. Omits the API's `limit` parameter (API returns all entries when `limit` is omitted)
- `--reverse`: Display in descending order by JOB_ID

```bash
cjob list                                    # Display latest 50 in ascending order
cjob list --all                              # Display all in ascending order
cjob list --reverse                          # Display latest 50 in descending order
cjob list --status RUNNING                   # Display latest 50 RUNNING jobs
cjob list --limit 10                         # Display only latest 10
cjob list --flavor gpu-a100                   # Display only gpu-a100 flavor jobs
cjob list --status QUEUED --time-limit 6h:   # QUEUED jobs with time_limit of 6 hours or more
cjob list --time-limit :12h                  # Jobs with time_limit less than 12 hours
cjob list --time-limit 6h:12h               # Jobs with time_limit 6 hours or more and less than 12 hours
cjob list --status QUEUED --format ids       # Output QUEUED job IDs comma-separated

# Hold all queued jobs that take 6 hours or more
cjob hold $(cjob list --status QUEUED --time-limit 6h: --format ids)
```

When the display count is less than the total number of jobs, a message indicating omission is displayed to standard error. The omission message is not displayed when `--format ids` is specified.

Long commands are truncated at the end for display (e.g., truncated at 40 characters).

## 7. `cjob status` Behavior

Calls `GET /v1/jobs/{job_id}` and displays key fields in formatted output.

```
$ cjob status 2
job_id:       2
type:         job
status:       RUNNING
command:      python main.py --alpha 0.2 --beta 16
cwd:          /home/jovyan/project-a/exp1
flavor:       cpu
cpu:          2
memory:       4Gi
gpu:          0
time_limit:   24h (23h 24m remaining)
created_at:   2026-03-23 12:35:00
dispatched_at: 2026-03-23 12:35:05
started_at:   2026-03-23 12:35:10
finished_at:  -
k8s_job_name: cjob-alice-2
node_name:    worker07
log_dir:      /home/jovyan/.cjob/logs/2
```

`time_limit` displays `time_limit_seconds` in a human-readable format. When the job is RUNNING, remaining time is also shown.

For sweep jobs, additional fields are displayed.

```
$ cjob status 3
job_id:         3
type:           sweep
status:         RUNNING
command:        python main.py --trial $CJOB_INDEX
cwd:            /home/jovyan/project-a
flavor:         cpu
cpu:            2
memory:         4Gi
gpu:            0
completions:    100
parallelism:    10
progress:       48/2/100 (succeeded/failed/total)
failed_indexes: 12,37
time_limit:     6h (4h 32m remaining)
created_at:     2026-03-23 12:35:00
dispatched_at:  2026-03-23 12:35:05
started_at:     2026-03-23 12:35:10
finished_at:    -
k8s_job_name:   cjob-alice-3
node_name:      worker07,worker08
log_dir:        /home/jovyan/.cjob/logs/3
```

`node_name` is the node name where the job was executed. For regular jobs, a single node name is displayed; for sweep jobs, all node names used for execution are displayed comma-separated (the Watcher cumulatively records them during RUNNING transition and sweep progress changes; see [watcher.md](watcher.md) §4.3.1 for details).

`last_error` displays the failure reason when the job is FAILED. The line itself is not displayed when the value is `null`.

```
$ cjob status 5
job_id:        5
type:          job
status:        FAILED
command:       echo hello
cwd:           /home/jovyan
flavor:        cpu
cpu:           1
memory:        1Gi
gpu:           0
time_limit:    1m
created_at:    2026-03-23 13:00:00
dispatched_at: -
started_at:    -
finished_at:   2026-03-23 13:00:01
k8s_job_name:  -
node_name:     -
log_dir:       /home/jovyan/.cjob/logs/5
last_error:    K8s API permanent error 403: admission webhook "validate-image.kyverno.io" denied the request
```

If a nonexistent job_id is specified, an error message is displayed and the command exits.

```
$ cjob status 999
Error: job_id 999 not found.
```

### sweep Job Logs

sweep jobs display all task logs concatenated in ascending index order with `cjob logs <job_id>`. A header line is inserted at each task boundary.

```
$ cjob logs 3
=== [index 0] ===
Training with alpha=0.1 ...
Done.
=== [index 1] ===
Training with alpha=0.2 ...
Done.
```

`--index <n>` displays only the log for the specified index task.

```
$ cjob logs 3 --index 2
Training with alpha=0.5 ...
Error: convergence failed
```

`--follow` is used in combination with `--index`. Using `--follow` alone (without `--index`) results in an error, prompting the user to specify `--index`.

Log directory structure:
- Regular jobs: `/home/jovyan/.cjob/logs/<job_id>/`
- sweep jobs: `/home/jovyan/.cjob/logs/<job_id>/<index>/`

## 8. CLI Configuration

### 8.1 API Endpoint

The Submit API endpoint is read from the `CJOB_API_URL` environment variable. A default value is used when not set.

```
# Note: The CLI is implemented in Rust (using the reqwest crate, etc.). The following is pseudocode for conceptual explanation.

SUBMIT_API_URL = env("CJOB_API_URL")
              or "http://submit-api.cjob-system.svc.cluster.local:8080"
```

The log directory path is not stored on the CLI side but is obtained from the API. Individual job `log_dir` is obtained from `GET /v1/jobs/{job_id}`, and the log base directory from `log_base_dir` in `GET /v1/jobs`. This prevents inconsistencies between CLI-side configuration and the server-side ConfigMap (`LOG_BASE_DIR`).

### 8.2 User Configuration File

User-specific settings are managed in a TOML format file. Operated via the `cjob config` subcommand.

#### Configuration File Path

Saved to `$XDG_CONFIG_HOME/cjob/config.toml`. Defaults to `~/.config/cjob/config.toml` when `XDG_CONFIG_HOME` is not set.

#### TOML Schema

```toml
[env]
exclude = ["SECRET_TOKEN", "JUPYTER_TOKEN"]
```

| Table | Key | Type | Description |
|---|---|---|---|
| `env` | `exclude` | List | List of environment variable names to exclude during job submission |

When the configuration file does not exist, all items are treated as default values (empty).

#### `cjob config` Subcommand

`cjob config` is a local operation that does not require authentication.

##### `cjob config list`

Displays all settings in TOML format. Displays default values when the configuration file does not exist.

```
$ cjob config list
[env]
exclude = [
    "SECRET_TOKEN",
    "JUPYTER_TOKEN",
]
```

##### `cjob config add <table> <key> <value>`

Adds an element to a list-type setting. Does nothing if the value already exists (no duplicates).

```bash
cjob config add env exclude MY_SECRET
```

##### `cjob config remove <table> <key> <value>`

Removes an element from a list-type setting.

```bash
cjob config remove env exclude MY_SECRET
```

##### `cjob config set <table> <key> <value>`

Changes a scalar-type setting value. Returns an error when used on a list-type key.

> **[Implementation Status] Not yet implemented (planned for future).** This subcommand is not yet implemented because no scalar-type setting keys currently exist.

##### `cjob config unset <table> <key>`

Deletes a scalar-type setting value (reverts to default). Returns an error when used on a list-type key.

> **[Implementation Status] Not yet implemented (planned for future).** Not yet implemented for the same reason as `cjob config set`.

##### Validation

Unknown table/key combinations result in an error. Subcommands that don't match the type (`set`/`unset` on list-type, `add`/`remove` on scalar-type) also result in an error, with guidance on the correct command.

```
$ cjob config set env exclude X
Error: env.exclude is a list type. Use add / remove instead.

$ cjob config add unknown key value
Error: Unknown setting: unknown.key
```

#### Environment Variable Exclusion

`cjob add` / `cjob sweep` reads the configuration file before job submission and excludes environment variables listed in `env.exclude` from the submission. When the configuration file does not exist, all environment variables are sent as before.

## 9. `cjob cancel` Behavior

Parses the job_id specification format to expand into a list of job_ids, then calls `POST /v1/jobs/cancel`.

**Cancelling sweep jobs:** Cancelling a sweep job deletes the entire K8s Indexed Job and immediately stops all in-progress tasks. Partial cancellation (specific indexes only) is not supported.

```
# Note: The CLI is implemented in Rust. The following is pseudocode for conceptual explanation.

fn parse_job_ids(expr) -> Vec<u32>:
    // "1-5,8,10-12" → [1, 2, 3, 4, 5, 8, 10, 11, 12]
    Split expr by ',' and process each part
        If it contains '-': Add sequential numbers from start..=end
        Otherwise: Add that number
    Remove duplicates, sort in ascending order, and return

fn cmd_cancel(expr):
    job_ids = parse_job_ids(expr)
    if len(job_ids) == 1:
        Call POST /v1/jobs/{job_id}/cancel
        Display "Job {job_id}: {status}"
    else:
        Send job_ids to POST /v1/jobs/cancel
        Receive result:
            If cancelled: Display "Cancelled"
            If skipped: Display "Skipped (already completed or cancelled)"
            If not_found: Display "Not found"
```

## 10. `cjob delete` Behavior

If the `--all` flag is present, calls `POST /v1/jobs/delete` without job_ids.
Otherwise, parses the job_id specification format to expand into a list of job_ids before calling.

```
# Note: The CLI is implemented in Rust. The following is pseudocode for conceptual explanation.

fn cmd_delete(expr, all: bool):
    if all:
        Send empty request to POST /v1/jobs/delete
    else:
        job_ids = parse_job_ids(expr)   // Shares the same parse logic as cancel
        Send job_ids to POST /v1/jobs/delete

    Receive result:
        Delete log directories corresponding to each path in result.log_dirs
        If deleted: Display "Deleted"
        If skipped:
            Jobs with reason "running" → "Cannot delete because it is running. Run cjob cancel first"
            Jobs with reason "held" → "Cannot delete because it is held. Run cjob cancel or cjob release first"
            Jobs with reason "deleting" → "Cannot delete because reset is in progress"
            (Branch based on skipped[].reason in the API response)
        If not_found: Display "Not found"
```

## 11. `cjob hold` Behavior

Holds QUEUED jobs and stops their execution by the Dispatcher.

If the `--all` flag is present, calls `POST /v1/jobs/hold` without job_ids (targets all QUEUED jobs in the namespace).
Otherwise, parses the job_id specification format to expand into a list of job_ids before calling.

```
# Note: The CLI is implemented in Rust. The following is pseudocode for conceptual explanation.

fn cmd_hold(expr, all: bool):
    if all:
        Send empty request to POST /v1/jobs/hold
    else:
        job_ids = parse_job_ids(expr)   // Shares the same parse logic as cancel
        Send job_ids to POST /v1/jobs/hold

    Receive result:
        If held: Display "Held"
        If skipped: Display "Skipped (not QUEUED)"
        If not_found: Display "Not found"
```

### Usage Examples

```bash
# Single specification
cjob hold 5

# Range and multiple specification
cjob hold 1-10
cjob hold 1,3,5
cjob hold 1-5,8,10-12

# Hold all QUEUED jobs
cjob hold --all
```

## 12. `cjob release` Behavior

Returns held (HELD) jobs to the queue and resumes their execution by the Dispatcher.

If the `--all` flag is present, calls `POST /v1/jobs/release` without job_ids (targets all HELD jobs in the namespace).
Otherwise, parses the job_id specification format to expand into a list of job_ids before calling.

```
# Note: The CLI is implemented in Rust. The following is pseudocode for conceptual explanation.

fn cmd_release(expr, all: bool):
    if all:
        Send empty request to POST /v1/jobs/release
    else:
        job_ids = parse_job_ids(expr)   // Shares the same parse logic as cancel
        Send job_ids to POST /v1/jobs/release

    Receive result:
        If released: Display "Returned to queue"
        If skipped: Display "Skipped (not HELD)"
        If not_found: Display "Not found"
```

### Usage Examples

```bash
# Single specification
cjob release 5

# Range and multiple specification
cjob release 1-10
cjob release 1,3,5

# Release all HELD jobs
cjob release --all
```

## 13. `cjob reset` Behavior

1. Retrieves job list with `GET /v1/jobs`, retains `log_base_dir` from the response, and checks in the following order:
   - If any `DELETING` jobs exist: Displays "Previous reset process has not yet completed. Please wait a moment and try again." and aborts
   - If any `QUEUED` / `DISPATCHING` / `DISPATCHED` / `RUNNING` / `HELD` jobs exist: Displays their job_ids and aborts
2. If all jobs are completed, displays a confirmation prompt to the user
3. Only if 'y' is entered, executes the following in order:
   1. Deletes the log directory at the path obtained from `log_base_dir` (deleting before the API call ensures that even if the CLI crashes after the API call, the log_dir does not exist when the Watcher resets the counter and job_id=1 is reused)
   2. Calls `POST /v1/reset` (returns 202 Accepted)
4. Displays a reset started message and exits (does not wait for completion)

The actual K8s Job deletion, DB cleanup, and counter reset are processed asynchronously by the Watcher.
If `cjob add` is executed before the reset completes, the Submit API returns 409 and rejects the submission because `DELETING` jobs exist.

**Note:** A race condition exists between the pre-check in step 1 and `POST /v1/reset` in step 3-2. If `POST /v1/reset` returns 409 after log deletion (e.g., another client operated after the pre-check), logs may be deleted without the reset being executed. Since the CLI assumes single-user usage, this is extremely rare, and even if it occurs, the job DB records are preserved, so the next `cjob reset` can reset normally.

```
$ cjob reset
Cannot reset because there are incomplete jobs.
Incomplete jobs: 3, 7, 12

$ cjob reset   # After all jobs are completed
Delete all 15 jobs and logs. Are you sure? [y/N] y
Reset has started. Please wait for background cleanup to complete.
```

## 14. `cjob usage` Behavior

Calls `GET /v1/usage` and displays daily resource usage for the past `FAIR_SHARE_WINDOW_DAYS` days.

Display units are converted for human readability.

- CPU: millicores seconds → core·h (`/ 1000 / 3600`)
- Memory: MiB seconds → GiB·h (`/ 1024 / 3600`)
- GPU: seconds → h (`/ 3600`)

The GPU column is hidden when there is no GPU usage across the entire cluster (`total_gpu_seconds == 0`).

```
$ cjob usage

Resource Usage (past 7 days)
──────────────────────────────────────────────────
  Date              CPU (core·h)    Mem (GiB·h)
  2026-03-23               24.0           48.0
  2026-03-24               12.5           25.0
  2026-03-25                8.0           16.0
  ────────────────────────────────────────────────
  Total                    44.5           89.0
```

When there is no usage history, displays "No usage history available."

### Resource Quota Display

When `resource_quota` in the response is not `null`, a Resource Quota section is displayed in table format before the usage table.

Column meanings:
- **Resource**: Resource type (CPU / Memory / GPU / Jobs)
- **Used**: Current usage
- **Hard**: Quota limit
- **Remaining**: Remaining (`hard - used`)
- **Use%**: Usage rate (`used / hard * 100`), 1 decimal place

Unit conversion:
- CPU: millicores → core count, 1 decimal place (e.g., `280.0`)
- Memory: MiB → GiB, integer (e.g., `800Gi`)
- GPU: count as-is (e.g., `1`)
- Jobs: count as-is (e.g., `10`)

The GPU row is hidden when `hard_gpu == 0`.
The Jobs row is hidden when `hard_count` is `null`.

```
$ cjob usage

Resource Quota
──────────────────────────────────────────────────
  Resource       Used       Hard  Remaining    Use%
  CPU           280.0      300.0       20.0   93.3%
  Memory        800Gi     1250Gi      450Gi   64.0%
  GPU               1          4          3   25.0%
  Jobs             10         50         40   20.0%

Resource Usage (past 7 days)
──────────────────────────────────────────────────
  Date              CPU (core·h)    Mem (GiB·h)
  2026-03-23               24.0           48.0
  2026-03-24               12.5           25.0
  2026-03-25                8.0           16.0
  ────────────────────────────────────────────────
  Total                    44.5           89.0
```

## 15. `cjob update` Behavior

Manages CLI binary versioning and updates. Binaries are distributed via the Submit API.

### Options

| Option | Description |
|---|---|
| `--pre` | Include pre-release versions (beta, etc.) |
| `--yes` / `-y` | Skip confirmation prompt |
| `--list` | Display list of available versions (mutually exclusive with `--version`) |
| `--version <version>` | Install specified version (mutually exclusive with `--list`) |

### Default Behavior (Update to Latest Stable)

1. Get the latest stable version (contents of the `latest` file) with `GET /v1/cli/version`
2. Compare with the local CLI version (shown by `--version`)
3. If it's the same version, display "Already up to date" and exit
4. If a newer version is available:
   1. Display confirmation prompt (skippable with `--yes`)
   2. Download binary with `GET /v1/cli/download?version=<version>`
   3. Replace the current executable with the new binary (temporary file + atomic rename)
   4. Grant execute permission (`0o755`) to the file after replacement
   5. Display update complete message

### With `--pre`

Gets the full version list with `GET /v1/cli/versions` and uses the latest version including pre-releases as the update target.

### With `--list`

Gets the full version list with `GET /v1/cli/versions` and displays it. By default, only stable versions are shown; with `--pre`, pre-release versions are also included. The currently installed version is marked with `(current)`, and the latest version with `(latest)`.

### With `--version <version>`

Directly installs the specified version. After the confirmation prompt, downloads with `GET /v1/cli/download?version=<version>` and replaces the binary.

### Usage Examples

```bash
# Update to latest stable (default)
$ cjob update
Update? 1.2.0 → 1.3.0 [y/N] y
Update complete. (1.3.0)

# Update to latest including beta
$ cjob update --pre
Update? 1.2.0 → 1.3.1-beta.2 [y/N] y
Update complete. (1.3.1-beta.2)

# Skip confirmation
$ cjob update -y
Update complete. (1.3.0)

# Already up to date
$ cjob update
Already up to date (1.3.0)

# List available versions (stable only)
$ cjob update --list
1.3.0 (latest)
1.2.0 (current)
1.1.0

# List including beta versions
$ cjob update --list --pre
1.3.1-beta.2
1.3.1-beta.1
1.3.0 (latest)
1.2.0 (current)
1.1.0

# Install specific version
$ cjob update --version 1.3.1-beta.1
Update? 1.2.0 → 1.3.1-beta.1 [y/N] y
Update complete. (1.3.1-beta.1)
```

## 16. `cjob flavor` Behavior

Calls `GET /v1/flavors` and displays the list of available ResourceFlavors and their resource limits. Uses an authentication-free endpoint, so it can be executed without a ServiceAccount JWT.

### `cjob flavor list`

Displays the list of available flavors. The default flavor is marked with `*`.

```
$ cjob flavor list
NAME             GPU    NODES    DEFAULT
cpu              -      2          *
gpu-a100         yes    1
```

### `cjob flavor info <name>`

Displays resource limits and per-task limits for the specified flavor.

QUOTA is the ClusterQueue's nominalQuota (total resource amount shared across the entire flavor). TASK LIMIT is the per-task resource limit, calculated as `min(max_node_allocatable, nominalQuota)`. The GPU row is omitted for non-GPU flavors.

```
$ cjob flavor info cpu
name:   cpu
GPU:    Not supported

RESOURCE      QUOTA    TASK LIMIT
CPU             256           128
Memory       1000Gi       503.4Gi
```

For GPU-capable flavors, the GPU row is also displayed.

```
$ cjob flavor info gpu-a100
name:   gpu-a100
GPU:    Supported

RESOURCE      QUOTA    TASK LIMIT
CPU              64            64
Memory        500Gi         500Gi
GPU               4             4
```

When quota information is not available because the Watcher has not yet synced, a message is displayed.

```
$ cjob flavor info cpu
name:   cpu
GPU:    Not supported

(Resource information has not been retrieved yet)
```

When a nonexistent flavor is specified, an error is displayed.

```
$ cjob flavor info xxx
Error: flavor 'xxx' does not exist. Available flavors: cpu, gpu-a100
```

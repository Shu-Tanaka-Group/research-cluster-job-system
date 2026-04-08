> *This document was auto-translated from the [Japanese original](../docs/user_guide.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# User Guide

For a list and transitions of job states (QUEUED, RUNNING, SUCCEEDED, etc.), see [Job States in the README](../README.en.md#job-states).

## 1. Job Count Limits

In CJob, per-user job count limits are enforced to maintain system stability and fairness.

| Limit | Maximum | Behavior When Limit Is Reached |
|---|---|---|
| Number of jobs that can be submitted | 500 | New jobs cannot be submitted |
| Number of concurrently running jobs | 32 per flavor, 50 total | Excess jobs wait in queued state |

### 1.1 Maximum Number of Submittable Jobs (500)

There is a limit on the number of jobs that can be submitted via `cjob add` or `cjob sweep`. When the total of queued and cancelled jobs exceeds **500**, new jobs cannot be submitted. Running jobs are not counted, so running jobs do not consume submission slots. Simply cancelling a job does not free up slots.

When the limit is reached, delete cancelled jobs with `cjob delete`. You can also free up slots by cancelling unnecessary queued jobs and then deleting them. For details on deleting jobs, see [Deleting Completed Jobs](../README.en.md#deleting-completed-jobs).

### 1.2 Maximum Number of Concurrently Running Jobs (32 per Flavor, 50 Total)

The maximum number of jobs that can be in a running state at once is **32 per flavor**. For example, CPU jobs and GPU jobs can each run up to 32 simultaneously. Even if 32 CPU jobs are running, the GPU job slots are unaffected.

Additionally, there is a **total job count limit across all flavors (default: 50)**. For example, if 30 CPU jobs and 20 GPU jobs are running for a total of 50, no new jobs of any flavor will start and they will wait in queued state.

Jobs beyond the 32nd or those exceeding the total limit will wait in queued state (QUEUED), and the next job will automatically start when a running job completes.

Generally, jobs are executed in submission order, but depending on resource availability, smaller jobs submitted later may execute first.

> [!NOTE]
> Completed jobs remain internally for a certain period (5 minutes by default). During this time, they are still counted toward the total job count, so when many jobs complete in a short period, the actual number of jobs that can run may be fewer than the limit. Slots are automatically freed when the retention period expires. You can check the current job count in the Resource Quota section of `cjob usage`.

## 2. Efficient Job Execution with sweep

When you want to run the same program repeatedly with different parameters, `cjob sweep` is convenient.

`cjob sweep` counts as **a single job** regardless of the number of executions. For example, running 100 times with `-n 100` consumes only 1 slot out of the submission limit (500). The same applies to the concurrent execution limit (32). For large-scale runs, using `cjob sweep` is preferable to submitting jobs one by one with `cjob add`, as you don't need to worry about hitting limits.

### 2.1 Basic Usage

For example, suppose you want to run 100 times varying parameter `--trial` from 0 to 99.

Submitting one by one with `cjob add` requires executing the command 100 times, but with `cjob sweep` a single command suffices.

```bash
# Submit 100 tasks at once and run 10 in parallel
cjob sweep -n 100 --parallel 10 -- python main.py --trial _INDEX_
```

- `-n 100`: Execute 100 times in total
- `--parallel 10`: Run up to 10 concurrently
- `_INDEX_`: Each execution is automatically assigned a number: 0, 1, 2, ... , 99

### 2.2 Using `_INDEX_`

`_INDEX_` is automatically replaced with a different number (sequential starting from 0) for each execution. You can use this number to vary parameters.

```bash
cjob sweep -n 50 --parallel 5 -- python train.py --seed _INDEX_
```

Within script files, you can reference the number directly using the variable `$CJOB_INDEX`.

```bash
# Example content of run.sh
python train.py --config configs/config_${CJOB_INDEX}.yaml
```

```bash
cjob sweep -n 10 --parallel 5 -- bash run.sh
```

### 2.3 Choosing the Parallelism Level

`--parallel` is the number of tasks running simultaneously within a single sweep job. Increasing parallelism proportionally increases simultaneous CPU and memory consumption, so set an appropriate value based on the cluster's resource availability.

```bash
# Run 10 tasks in parallel
cjob sweep -n 100 --parallel 10 -- python main.py --trial _INDEX_

# Use lower parallelism to reduce resource consumption
cjob sweep -n 100 --parallel 5 -- python main.py --trial _INDEX_
```

### 2.4 Per-Task Resource Specification

Using the `--cpu`, `--memory`, and `--gpu` options, you can specify **the amount of compute resources each individual task uses**. This is the amount allocated to each parallel task, not the sweep as a whole.

```bash
# Allocate 2 CPU cores and 4GiB memory per task
cjob sweep -n 50 --parallel 5 --cpu 2 --memory 4Gi -- python main.py --trial _INDEX_

# When using GPU, specify the number of GPUs per task with --gpu
cjob sweep -n 10 --parallel 2 --gpu 1 -- python train.py --trial _INDEX_
```

- `--cpu`: Number of CPU cores per task (default: 1)
- `--memory`: Amount of memory per task (default: 1Gi)
- `--gpu`: Number of GPUs per task (default: 0, i.e., no GPU)

For example, with `--parallel 5 --cpu 2 --memory 4Gi`, up to 5 tasks run simultaneously, consuming a total of 10 CPU cores and 20GiB of memory. If the cluster lacks sufficient resources, tasks will wait in queued state, so adjust parallelism and resource amounts as needed.

### 2.5 Time Limit

The time limit set with `--time-limit` applies to **the total time until all tasks complete**. It is not a per-execution time limit.

For example, with `-n 100 --parallel 10 --time-limit 2h`, all 100 tasks must complete within 2 hours. After 2 hours, the entire job including remaining tasks is terminated.

When the number of tasks is large or parallelism is low, the time to completion increases accordingly. Set a value with sufficient margin.

### 2.6 Checking sweep Logs

Logs for each task in a sweep can be checked individually by specifying the index number.

```bash
# Display logs for all tasks
cjob logs 3

# Display log for task number 5 only
cjob logs 3 --index 5

# Track log for task number 5 in real-time
cjob logs --follow 3 --index 5
```

### 2.7 Cancelling a sweep

Cancelling a sweep job stops all in-progress tasks. It is not possible to cancel a specific task number only.

```bash
cjob cancel 3
```

## 3. Specifying Compute Resource Type (`--flavor`)

A cluster may have multiple types of compute nodes with different capabilities, such as CPU-only nodes and nodes with GPUs. When submitting a job, the `--flavor` option lets you specify which type of node to run on.

### 3.1 Basic Usage

```bash
# Run on regular CPU nodes (default when --flavor is omitted)
cjob add -- python main.py

# Run on GPU nodes
cjob add --flavor gpu --gpu 1 -- python train.py --epochs 100
```

When `--flavor` is omitted, the default type configured by the administrator (usually CPU nodes) is automatically selected.

### 3.2 Checking Available Types

Use `cjob flavor list` to check available node types. The type marked with `*` is the default (used when `--flavor` is omitted).

```
$ cjob flavor list
NAME             GPU    NODES    DEFAULT
cpu              -      2          *
gpu              yes    1
```

To check resource limits for each type, use `cjob flavor info`.

```
$ cjob flavor info gpu
name:   gpu
GPU:    Supported

RESOURCE        QUOTA   TASK LIMIT
CPU                32           32
Memory          128Gi        128Gi
GPU                 4            4
```

- **QUOTA**: Total resources available across all nodes of that type
- **TASK LIMIT**: Maximum resources that can be specified for a single job (task)

### 3.3 Usage with sweep

`cjob sweep` also supports the `--flavor` option.

```bash
# Run 10 tasks in parallel (2 at a time) on GPU nodes
cjob sweep -n 10 --parallel 2 --flavor gpu --gpu 1 -- python train.py --trial _INDEX_
```

### 3.4 Matching Resources to the Specified Type

Submit jobs within the resource range of the node type specified with `--flavor`. For example, specifying `--gpu 1` for a node type that does not have GPUs results in an error.

```
$ cjob add --flavor cpu --gpu 1 -- python train.py
Error: flavor 'cpu' does not support GPU
```

An error also occurs when the CPU or memory request exceeds the **TASK LIMIT** shown by `cjob flavor info`.

### 3.5 Checking a Job's Type

You can check which type of node a submitted job is running on with `cjob status`.

```
$ cjob status 1
job_id:        1
status:        RUNNING
command:       python train.py --epochs 100
cwd:           /home/jovyan/project-a
flavor:        gpu
cpu:           1
memory:        1Gi
gpu:           1
time_limit:    24h (23h 50m remaining)
...
```

## 4. Holding Job Execution (`cjob hold` / `cjob release`)

You can temporarily hold execution of submitted jobs and return them to the queue later. For example, this can be used to prevent jobs from being forcibly stopped during a scheduled system maintenance.

### 4.1 Basic Usage

```bash
# Hold execution of job 5
cjob hold 5

# Release hold and return to queue
cjob release 5
```

Held jobs are displayed with `HELD` status in `cjob list`. While held, the system will not execute them. Returning them to the queue with `cjob release` allows them to be automatically executed when their turn comes, just like normal queued jobs.

### 4.2 Holding Multiple Jobs at Once

Like `cjob cancel` and `cjob delete`, range and multiple specifications are supported.

```bash
# Range specification
cjob hold 1-10

# Multiple specification
cjob hold 1,3,5

# Combination
cjob hold 1-5,8,10-12

# Hold all queued (QUEUED) jobs
cjob hold --all
```

Releasing works the same way.

```bash
# Return all held (HELD) jobs to the queue
cjob release --all
```

### 4.3 Holding Jobs Filtered by Time Limit

By combining the `--time-limit` option and `--format ids` option of `cjob list`, you can filter jobs by their time limit (the value set with `--time-limit`) and hold them in bulk.

For example, if there are only 6 hours until maintenance and you want to hold only jobs that would take 6 hours or more:

```bash
# Check IDs of queued jobs with a time limit of 6 hours or more
cjob list --status QUEUED --time-limit 6h: --format ids

# Hold them in bulk using the result
cjob hold $(cjob list --status QUEUED --time-limit 6h: --format ids)
```

`--time-limit` uses the format `<lower>:<upper>` to specify a range. The lower bound is inclusive ("or more") and the upper bound is exclusive ("less than").

```bash
# Jobs with a time limit of 6 hours or more
cjob list --time-limit 6h:

# Jobs with a time limit of less than 12 hours
cjob list --time-limit :12h

# Jobs with a time limit of 6 hours or more and less than 12 hours
cjob list --time-limit 6h:12h
```

`--format ids` outputs job IDs separated by commas. It can be passed directly to commands that accept IDs, such as `cjob hold` and `cjob cancel`.

### 4.4 Conditions for Holding Jobs

Only **QUEUED (waiting)** jobs can be held. Jobs that are already running (RUNNING) or in the dispatch process (DISPATCHING / DISPATCHED) cannot be held.

Held jobs can also be cancelled.

```bash
# Cancel a held job
cjob cancel 5
```

## 5. Modifying Parameters of Submitted Jobs (`cjob set`)

You can change resource and execution time settings for submitted jobs that are in QUEUED (waiting) or HELD (held) state. For example, this is useful when you want to move jobs to a different type of node because CPU nodes are congested, or when you want to adjust resource amounts or execution time after submission.

### 5.1 Basic Usage

```bash
# Change the flavor of job 5
cjob set 5 --flavor cpu-sub

# Change the CPU and memory of job 5
cjob set 5 --cpu 4 --memory 16Gi

# Change the time limit of job 5
cjob set 5 --time-limit 12h

# Change multiple parameters at once
cjob set 5 --flavor cpu-sub --cpu 4 --memory 16Gi --time-limit 12h
```

Only the specified options are updated; unspecified items retain their original values. An error occurs if no options are specified.

### 5.2 Modifying Multiple Jobs at Once

Like `cjob cancel` and `cjob hold`, range and multiple specifications are supported.

```bash
# Range specification
cjob set 10-20 --flavor cpu-sub

# Multiple specification
cjob set 10,11,12 --flavor cpu-sub

# Combination
cjob set 10-20,25,30 --cpu 8
```

Combining with `--format ids` from `cjob list` allows you to modify jobs matching certain conditions in bulk.

```bash
# Change all queued CPU jobs to cpu-sub
cjob set $(cjob list --status QUEUED --flavor cpu --format ids) --flavor cpu-sub
```

### 5.3 Conditions for Modifying Jobs

Only **QUEUED (waiting)** or **HELD (held)** jobs can be modified. Jobs that are already running (RUNNING) or in the dispatch process (DISPATCHING / DISPATCHED) cannot be modified. Jobs that cannot be modified are skipped.

Validation rules are the same as for `cjob add`. For example, changing to a node type without GPU while `--gpu 1` is set on the job results in an error.

## 6. Aligning CPU Core Count Between Your Program and `--cpu`

`--cpu` is the number of CPU cores the system allocates to the job. On the other hand, how many cores a program actually uses depends on the program's own settings. **If these two are not aligned, resources cannot be used efficiently.**

For example, if you specify `--cpu 4` but the program is configured to use only 1 core, the remaining 3 cores are wasted. Conversely, if you leave `--cpu 1` but the program tries to use many cores, processing may slow down as it exceeds the allocated range.

### 6.1 When Programs Use Multiple Cores

There are broadly two cases where programs use multiple CPU cores.

**Libraries automatically using multiple cores**

Many numerical computing libraries such as NumPy and SciPy internally use multiple cores automatically to speed up operations like matrix computation. In this case, multiple cores are used even if you haven't written any parallel processing code.

**Writing parallel processing yourself**

When you use Python's `concurrent.futures` or `multiprocessing` to spawn multiple processes or threads, CPU cores are consumed accordingly. For example, `ProcessPoolExecutor(max_workers=4)` uses up to 4 cores simultaneously.

In both cases, adjust so that the total number of cores your program uses matches the value specified with `--cpu`.

### 6.2 Adjusting Core Count on the Program Side

How to adjust the number of cores a program uses varies by program and library. Before submitting a job, check the documentation for the programs and libraries you use.

As a common example, numerical computing libraries often allow core count control via environment variables.

| Environment Variable | Example Targets |
|---|---|
| `OMP_NUM_THREADS` | Programs using OpenMP in general |
| `OPENBLAS_NUM_THREADS` | OpenBLAS (sometimes used internally by NumPy, etc.) |
| `MKL_NUM_THREADS` | Intel MKL (sometimes used internally by NumPy, etc.) |

These are only examples. Depending on the program, core count may be specified via different environment variables, command-line arguments, or configuration files.

When writing parallel processing yourself, match the number of processes or threads in your code to the `--cpu` value.

### 6.3 Configuration Examples

Set the program-side core count to match the number of cores specified with `--cpu`.

```bash
# Specifying environment variables at the beginning of the command
cjob add --cpu 4 -- OMP_NUM_THREADS=4 python main.py

# For cjob sweep
cjob sweep -n 50 --parallel 5 --cpu 4 -- \
  OMP_NUM_THREADS=4 python main.py --trial _INDEX_
```

You can also set them in advance with `export`. Since CJob carries over exported environment variables at job submission time, the same settings apply to all subsequent jobs.

```bash
export OMP_NUM_THREADS=4

cjob add --cpu 4 -- python main.py
cjob sweep -n 50 --parallel 5 --cpu 4 -- python main.py --trial _INDEX_
```

## 7. Checking Resource Usage (`cjob usage`)

Running `cjob usage` shows current resource consumption and recent usage history.

### 7.1 Resource Quota (Current Resource Consumption)

Shows the currently used resource amounts and allocated limits.

```
Resource Quota
──────────────────────────────────────────────────
  Resource       Used       Hard  Remaining    Use%
  CPU           280.0      300.0       20.0   93.3%
  Memory        800Gi     1250Gi      450Gi   64.0%
  GPU               1          4          3   25.0%
  Jobs             10         50         40   20.0%
```

- **Used**: Current usage
- **Hard**: Allocated limit
- **Remaining**: Remaining (`Hard - Used`)
- **Use%**: Usage rate

CPU, Memory, and GPU are the total resources requested by running jobs. Jobs is the number of jobs existing in the system, corresponding to the [total job count limit in §1.2](#12-maximum-number-of-concurrently-running-jobs-32-per-flavor-50-total).

### 7.2 Resource Usage (Daily Usage History)

Shows daily resource consumption for the past 7 days.

```
Resource Usage (past 7 days)
──────────────────────────────────────────────────
  Date              CPU (core·h)    Mem (GiB·h)
  2026-03-23               24.0           48.0
  2026-03-24               12.5           25.0
  2026-03-25                8.0           16.0
  ────────────────────────────────────────────────
  Total                    44.5           89.0
```

- CPU is displayed in core·h (core-hours), memory in GiB·h (gibibyte-hours)
- If GPU is being used, a GPU column (GPU·h) is also displayed
- When multiple flavors (CPU nodes and GPU nodes, etc.) are used, each day's value is the total across all flavors
- Resource consumption is calculated based on the `--time-limit` value from the moment the job enters the running state

## 8. Managing User Settings (`cjob config`)

Use the `cjob config` command to customize CJob's behavior. Settings are saved to `~/.config/cjob/config.toml` (or `$XDG_CONFIG_HOME/cjob/config.toml` if the `XDG_CONFIG_HOME` environment variable is set).

### 8.1 Environment Variable Exclusion Settings

CJob carries over all shell environment variables when submitting a job, but if there are variables you don't want carried over, you can add them to the exclusion list.

```bash
# Add environment variables to exclude
cjob config add env exclude MY_SECRET_TOKEN
cjob config add env exclude AWS_SECRET_ACCESS_KEY

# Remove from exclusion list
cjob config remove env exclude MY_SECRET_TOKEN
```

Environment variables added to the exclusion list will not be sent to the server when submitting jobs with `cjob add` or `cjob sweep`.

### 8.2 Checking Current Settings

```bash
cjob config list
```

```
[env]
exclude = [
    "MY_SECRET_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
]
```

If the configuration file does not exist or nothing has been configured, default values (empty list) are displayed.

> *This document was auto-translated from the [Japanese original](../../docs/architecture/requirements.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# Functional Requirements

## 1. Features of the Job Queue System to Provide

### 1.1 Basic Features for Users

The main features provided by this system are as follows.

- Submit shell commands as jobs
- Submit parameter sweeps as jobs (parallel execution via Indexed Job)
- View a list of submitted jobs
- Check the status of individual jobs
- Cancel jobs
- Hold and release job execution (individual, bulk, or all)
- Modify parameters of submitted jobs (for jobs in QUEUED / HELD state, flavor, CPU, memory, GPU, and execution time limit can be changed)
- Delete completed jobs (individual or bulk)
- View job logs (including real-time tracking)
- Check own resource usage (daily CPU, memory, and GPU consumption; remaining ResourceQuota for the namespace)
- Delete all job history and logs and reset job_id
- Self-update the CLI binary
- In the future, available via API from a workflow engine

### 1.2 Information to Reproduce at Job Submission

The following information at the time of job submission is reproduced when the job is executed.

- Working directory (`cwd`)
- Exported environment variables (including `PATH` / `VIRTUAL_ENV` for virtual environments, excluding variables specified for exclusion in user settings)
- Executed command string

### 1.3 Execution Control Features

- Temporary queuing of jobs
- Conversion of jobs to Kubernetes Jobs
- Admission control via Kueue
- Control of the number of dispatches per namespace
- Prevention of unintended unlimited consumption via ResourceQuota per namespace (safety net)
- Tracking of execution state
- Consistency between Kubernetes Job / Pod state and the internal state DB

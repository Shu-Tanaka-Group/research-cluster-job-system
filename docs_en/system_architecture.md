> *This document was auto-translated from the [Japanese original](../docs/system_architecture.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# CJob Design Document

## 1. Overview

This document describes the design of **`cjob`, a user-facing job queue system** that runs on Kubernetes.
The system targets research computing, parameter sweeps, and batch computation, with the goal of allowing users to submit shell commands directly as jobs **without needing to be aware of Kubernetes Jobs, Pods, or YAML**.

The system is designed according to the following principles:

- The basic user operation is `cjob add <job command>`
- The execution environment is built on Kubernetes
- One Kubernetes Job is created per job submission (for sweeps, an Indexed Job runs multiple tasks)
- Assuming a very large number of job submissions, dispatch is controlled by a **DB-scan-based Dispatcher**
- **Kueue** is used for execution control on Kubernetes
- Jobs are executed reproducing the user's working directory and environment variables as closely as possible
- The architecture allows for future addition of a higher-level orchestration layer such as Prefect

## 2. Document Structure

The detailed design is split across the following files.

| Document | Contents |
|---|---|
| [architecture/requirements.md](architecture/requirements.md) | Functional requirements and use cases |
| [architecture/prerequisites.md](architecture/prerequisites.md) | Infrastructure, execution environment, and scheduling prerequisites |
| [architecture/system_design.md](architecture/system_design.md) | Feature list, implementation policy, and system configuration |
| [architecture/database.md](architecture/database.md) | PostgreSQL table definitions and state transitions |
| [architecture/kueue.md](architecture/kueue.md) | Kueue design and Job templates |
| [architecture/resources.md](architecture/resources.md) | Resource design and limits summary |
| [architecture/api.md](architecture/api.md) | API endpoint specifications |
| [architecture/cli.md](architecture/cli.md) | CLI command specifications, usage examples, and behavior details |
| [architecture/dispatcher.md](architecture/dispatcher.md) | Dispatcher scheduling and detailed design |
| [architecture/watcher.md](architecture/watcher.md) | Watcher / Reconciler design |
| [architecture/cjobctl.md](architecture/cjobctl.md) | Admin CLI (cjobctl) design |
| [architecture/roadmap.md](architecture/roadmap.md) | Future extensions |
| [architecture/performance.md](architecture/performance.md) | Performance analysis and scaling estimates |
| [architecture/monitoring.md](architecture/monitoring.md) | Monitoring design and Grafana dashboards |

Related documents:

| Document | Contents |
|---|---|
| [auth_policy.md](auth_policy.md) | Authentication and authorization design |
| [deployment.md](deployment.md) | Deployment design and K8s manifests |

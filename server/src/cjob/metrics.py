from prometheus_client import Counter

JOBS_SUBMITTED_TOTAL = Counter(
    "cjob_jobs_submitted_total",
    "Total number of jobs submitted",
)

JOBS_COMPLETED_TOTAL = Counter(
    "cjob_jobs_completed_total",
    "Total number of jobs that reached a terminal status",
    ["status"],
)

use crate::client::{JobDetailResponse, JobSummary};

pub fn print_job_ids(jobs: &[JobSummary]) {
    if jobs.is_empty() {
        return;
    }
    let ids: Vec<String> = jobs.iter().map(|j| j.job_id.to_string()).collect();
    println!("{}", ids.join(","));
}

pub fn print_job_table(jobs: &[JobSummary]) {
    if jobs.is_empty() {
        println!("ジョブがありません。");
        return;
    }

    println!(
        "{:<8} {:<6} {:<12} {:<12} {:<12} {:<34} {:<20} {}",
        "JOB_ID", "TYPE", "STATUS", "FLAVOR", "PROGRESS", "COMMAND", "CREATED", "FINISHED"
    );
    for job in jobs {
        let job_type = if job.completions.is_some() { "sweep" } else { "job" };
        let progress = match (job.completions, job.succeeded_count, job.failed_count) {
            (Some(total), Some(succ), Some(fail)) => {
                format!("{}/{}/{}", succ, fail, total)
            }
            _ => "-".to_string(),
        };
        let command_display = if job.command.len() > 34 {
            format!("{}...", &job.command[..31])
        } else {
            job.command.clone()
        };
        // Trim datetime to minute precision
        let created = if job.created_at.len() > 16 {
            &job.created_at[..16]
        } else {
            &job.created_at
        };
        let finished = match &job.finished_at {
            Some(ts) if ts.len() > 16 => &ts[..16],
            Some(ts) => ts.as_str(),
            None => "-",
        };
        println!(
            "{:<8} {:<6} {:<12} {:<12} {:<12} {:<34} {:<20} {}",
            job.job_id, job_type, job.status, job.flavor, progress, command_display, created, finished
        );
    }
}

fn format_duration(secs: u32) -> String {
    let days = secs / 86400;
    let hours = (secs % 86400) / 3600;
    let minutes = (secs % 3600) / 60;
    if days > 0 {
        format!("{}d {}h", days, hours)
    } else if hours > 0 {
        format!("{}h {}m", hours, minutes)
    } else {
        format!("{}m", minutes)
    }
}

fn format_time_limit(job: &JobDetailResponse) -> String {
    let limit_str = format_duration(job.time_limit_seconds);
    if job.status == "RUNNING" {
        if let Some(ref started) = job.started_at {
            // Parse started_at and compute remaining time
            if let Ok(started_dt) = chrono::DateTime::parse_from_rfc3339(started) {
                let elapsed = chrono::Utc::now()
                    .signed_duration_since(started_dt)
                    .num_seconds()
                    .max(0) as u32;
                let remaining = job.time_limit_seconds.saturating_sub(elapsed);
                return format!("{} (残り {})", limit_str, format_duration(remaining));
            }
        }
    }
    limit_str
}

pub fn print_job_detail(job: &JobDetailResponse) {
    let is_sweep = job.completions.is_some();

    println!("job_id:        {}", job.job_id);
    if is_sweep {
        println!("type:          sweep");
    } else {
        println!("type:          job");
    }
    println!("status:        {}", job.status);
    println!("command:       {}", job.command);
    println!("cwd:           {}", job.cwd);
    println!("flavor:        {}", job.flavor);
    println!("cpu:           {}", job.cpu);
    println!("memory:        {}", job.memory);
    println!("gpu:           {}", job.gpu);
    if let (Some(completions), Some(parallelism)) = (job.completions, job.parallelism) {
        println!("completions:   {}", completions);
        println!("parallelism:   {}", parallelism);
        if let (Some(succ), Some(fail)) = (job.succeeded_count, job.failed_count) {
            println!("progress:      {}/{}/{} (succeeded/failed/total)", succ, fail, completions);
        }
        if let Some(ref fi) = job.failed_indexes {
            if !fi.is_empty() {
                println!("failed_indexes: {}", fi);
            }
        }
    }
    println!("time_limit:    {}", format_time_limit(job));
    println!(
        "created_at:    {}",
        job.created_at
    );
    println!(
        "dispatched_at: {}",
        job.dispatched_at.as_deref().unwrap_or("-")
    );
    println!(
        "started_at:    {}",
        job.started_at.as_deref().unwrap_or("-")
    );
    println!(
        "finished_at:   {}",
        job.finished_at.as_deref().unwrap_or("-")
    );
    println!(
        "k8s_job_name:  {}",
        job.k8s_job_name.as_deref().unwrap_or("-")
    );
    println!(
        "node_name:     {}",
        job.node_name
            .as_ref()
            .map(|v| v.join(", "))
            .as_deref()
            .unwrap_or("-")
    );
    println!(
        "log_dir:       {}",
        job.log_dir.as_deref().unwrap_or("-")
    );
    if let Some(ref err) = job.last_error {
        println!("last_error:    {}", err);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::client::JobDetailResponse;

    #[test]
    fn test_format_duration_days() {
        assert_eq!(format_duration(86400), "1d 0h");
        assert_eq!(format_duration(90000), "1d 1h");
        assert_eq!(format_duration(604800), "7d 0h");
    }

    #[test]
    fn test_format_duration_hours() {
        assert_eq!(format_duration(3600), "1h 0m");
        assert_eq!(format_duration(5400), "1h 30m");
        assert_eq!(format_duration(82800), "23h 0m");
    }

    #[test]
    fn test_format_duration_minutes() {
        assert_eq!(format_duration(0), "0m");
        assert_eq!(format_duration(60), "1m");
        assert_eq!(format_duration(3599), "59m");
    }

    fn make_job(status: &str, time_limit_seconds: u32, started_at: Option<&str>) -> JobDetailResponse {
        JobDetailResponse {
            job_id: 1,
            status: status.to_string(),
            namespace: "user-test".to_string(),
            command: "echo hello".to_string(),
            cwd: "/home/jovyan".to_string(),
            cpu: "1".to_string(),
            memory: "1Gi".to_string(),
            gpu: 0,
            flavor: "cpu".to_string(),
            time_limit_seconds,
            k8s_job_name: None,
            log_dir: None,
            created_at: "2026-03-23T12:00:00Z".to_string(),
            dispatched_at: None,
            started_at: started_at.map(|s| s.to_string()),
            finished_at: None,
            last_error: None,
            completions: None,
            parallelism: None,
            succeeded_count: None,
            failed_count: None,
            completed_indexes: None,
            failed_indexes: None,
            node_name: None,
        }
    }

    #[test]
    fn test_format_time_limit_not_running() {
        let job = make_job("QUEUED", 86400, None);
        assert_eq!(format_time_limit(&job), "1d 0h");
    }

    #[test]
    fn test_format_time_limit_running_no_started_at() {
        let job = make_job("RUNNING", 3600, None);
        assert_eq!(format_time_limit(&job), "1h 0m");
    }

    #[test]
    fn test_format_time_limit_running_with_started_at() {
        // Use a started_at far in the past so remaining is 0
        let job = make_job("RUNNING", 3600, Some("2020-01-01T00:00:00Z"));
        let result = format_time_limit(&job);
        assert!(result.contains("残り"));
        assert!(result.contains("0m"));
    }

    #[test]
    fn test_format_time_limit_running_just_started() {
        // Use current time as started_at so remaining ≈ full time_limit
        let now = chrono::Utc::now().to_rfc3339();
        let job = make_job("RUNNING", 86400, Some(&now));
        let result = format_time_limit(&job);
        assert!(result.starts_with("1d 0h"));
        assert!(result.contains("残り"));
    }

    #[test]
    fn test_format_time_limit_succeeded() {
        let job = make_job("SUCCEEDED", 86400, Some("2026-03-23T12:00:00Z"));
        // Non-RUNNING status should not show remaining time
        assert_eq!(format_time_limit(&job), "1d 0h");
    }

    #[test]
    fn test_format_time_limit_invalid_started_at() {
        let job = make_job("RUNNING", 3600, Some("not-a-date"));
        // Invalid date should fall back to just the limit string
        assert_eq!(format_time_limit(&job), "1h 0m");
    }

    fn make_sweep_job() -> JobDetailResponse {
        JobDetailResponse {
            job_id: 3,
            status: "RUNNING".to_string(),
            namespace: "user-test".to_string(),
            command: "python main.py --trial $CJOB_INDEX".to_string(),
            cwd: "/home/jovyan/project-a".to_string(),
            cpu: "2".to_string(),
            memory: "4Gi".to_string(),
            gpu: 0,
            flavor: "cpu".to_string(),
            time_limit_seconds: 21600,
            k8s_job_name: Some("cjob-test-3".to_string()),
            log_dir: Some("/home/jovyan/.cjob/logs/3".to_string()),
            created_at: "2026-03-23T12:35:00Z".to_string(),
            dispatched_at: Some("2026-03-23T12:35:05Z".to_string()),
            started_at: Some("2020-01-01T00:00:00Z".to_string()),
            finished_at: None,
            last_error: None,
            completions: Some(100),
            parallelism: Some(10),
            succeeded_count: Some(48),
            failed_count: Some(2),
            completed_indexes: Some("0-47".to_string()),
            failed_indexes: Some("12,37".to_string()),
            node_name: Some(vec!["worker07".to_string()]),
        }
    }

    #[test]
    fn test_format_sweep_time_limit() {
        let job = make_sweep_job();
        let result = format_time_limit(&job);
        assert!(result.contains("6h 0m"));
        assert!(result.contains("残り"));
    }
}

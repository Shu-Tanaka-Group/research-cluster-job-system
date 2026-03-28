use crate::client::{JobDetailResponse, JobSummary};

pub fn print_job_table(jobs: &[JobSummary]) {
    if jobs.is_empty() {
        println!("ジョブがありません。");
        return;
    }

    println!(
        "{:<8} {:<12} {:<40} {:<20} {}",
        "JOB_ID", "STATUS", "COMMAND", "CREATED", "FINISHED"
    );
    for job in jobs {
        let command_display = if job.command.len() > 40 {
            format!("{}...", &job.command[..37])
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
            "{:<8} {:<12} {:<40} {:<20} {}",
            job.job_id, job.status, command_display, created, finished
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
    println!("job_id:        {}", job.job_id);
    println!("status:        {}", job.status);
    println!("command:       {}", job.command);
    println!("cwd:           {}", job.cwd);
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
            time_limit_seconds,
            k8s_job_name: None,
            log_dir: None,
            created_at: "2026-03-23T12:00:00Z".to_string(),
            dispatched_at: None,
            started_at: started_at.map(|s| s.to_string()),
            finished_at: None,
            last_error: None,
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
}

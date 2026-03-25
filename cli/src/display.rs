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

pub fn print_job_detail(job: &JobDetailResponse) {
    println!("job_id:        {}", job.job_id);
    println!("status:        {}", job.status);
    println!("command:       {}", job.command);
    println!("cwd:           {}", job.cwd);
    println!(
        "created_at:    {}",
        job.created_at
    );
    println!(
        "dispatched_at: {}",
        job.dispatched_at.as_deref().unwrap_or("-")
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
}

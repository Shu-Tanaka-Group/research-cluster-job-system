use anyhow::{bail, Result};
use std::io::{self, Read, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant};

use crate::client::CjobClient;

const WAIT_TIMEOUT: Duration = Duration::from_secs(300); // 5 minutes
const POLL_INTERVAL: Duration = Duration::from_secs(3);
const TAIL_INTERVAL: Duration = Duration::from_millis(200);

fn stdout_path(log_dir: &str) -> PathBuf {
    PathBuf::from(log_dir).join("stdout.log")
}

fn stderr_path(log_dir: &str) -> PathBuf {
    PathBuf::from(log_dir).join("stderr.log")
}

pub async fn show_logs(job_id: u32, follow: bool, index: Option<u32>, client: &CjobClient) -> Result<()> {
    // Get current job status
    let job = client.get_job(job_id).await?;

    let log_dir = match &job.log_dir {
        Some(d) => d.as_str(),
        None => bail!("ジョブ {} の log_dir が設定されていません", job_id),
    };

    let is_sweep = job.completions.is_some();

    // Validate --index usage
    if index.is_some() && !is_sweep {
        bail!("--index はスイープジョブのみ使用できます");
    }

    // For sweep with --follow but no --index
    if is_sweep && follow && index.is_none() {
        bail!("スイープの全インデックスを追跡するには --index を指定してください\n例: cjob logs --follow {} --index 0", job_id);
    }

    match job.status.as_str() {
        "QUEUED" | "DISPATCHING" | "DISPATCHED" => {
            if !follow {
                println!("ジョブ {} はまだ開始されていません。({})", job_id, job.status);
                println!("`cjob logs --follow {}` でログの追跡ができます。", job_id);
                return Ok(());
            }
            wait_for_start(job_id, client).await?;
        }
        "RUNNING" => {}
        "SUCCEEDED" | "FAILED" => {
            if is_sweep {
                return show_sweep_logs(log_dir, index, job.completions.unwrap());
            }
            print_full_logs(log_dir)?;
            return Ok(());
        }
        "CANCELLED" => {
            if is_sweep {
                if let Some(idx) = index {
                    let idx_dir = format!("{}/{}", log_dir, idx);
                    if stdout_path(&idx_dir).exists() {
                        print_full_logs(&idx_dir)?;
                    } else {
                        println!("No logs available");
                    }
                } else {
                    return show_sweep_logs(log_dir, None, job.completions.unwrap_or(0));
                }
                return Ok(());
            }
            if stdout_path(log_dir).exists() {
                print_full_logs(log_dir)?;
            } else {
                println!("No logs available");
            }
            return Ok(());
        }
        "DELETING" => {
            if stdout_path(log_dir).exists() {
                print_full_logs(log_dir)?;
            } else {
                println!("No logs available（reset 処理中）");
            }
            return Ok(());
        }
        _ => {
            println!("不明なステータス: {}", job.status);
            return Ok(());
        }
    }

    // For RUNNING jobs
    if is_sweep {
        if let Some(idx) = index {
            let idx_dir = format!("{}/{}", log_dir, idx);
            if follow {
                return tail_logs(&idx_dir).await;
            } else {
                return print_full_logs(&idx_dir);
            }
        } else {
            // No --index, show all (non-follow, validated above)
            return show_sweep_logs(log_dir, None, job.completions.unwrap());
        }
    }

    if follow {
        tail_logs(log_dir).await
    } else {
        print_full_logs(log_dir)
    }
}

fn show_sweep_logs(log_dir: &str, index: Option<u32>, completions: u32) -> Result<()> {
    if let Some(idx) = index {
        let idx_dir = format!("{}/{}", log_dir, idx);
        return print_full_logs(&idx_dir);
    }

    // Show all indexes
    let base = PathBuf::from(log_dir);
    let mut found_any = false;
    for i in 0..completions {
        let idx_dir = base.join(i.to_string());
        let stdout = idx_dir.join("stdout.log");
        if stdout.exists() {
            println!("=== [index {}] ===", i);
            let content = std::fs::read_to_string(&stdout)?;
            print!("{}", content);

            let stderr = idx_dir.join("stderr.log");
            if stderr.exists() {
                let err_content = std::fs::read_to_string(&stderr)?;
                if !err_content.is_empty() {
                    eprint!("{}", err_content);
                }
            }
            found_any = true;
        }
    }
    if !found_any {
        println!("No logs available");
    }
    Ok(())
}

async fn wait_for_start(job_id: u32, client: &CjobClient) -> Result<()> {
    let start = Instant::now();

    loop {
        if start.elapsed() >= WAIT_TIMEOUT {
            let job = client.get_job(job_id).await?;
            eprintln!(
                "タイムアウトしました。ジョブはまだ {} 状態です。",
                job.status
            );
            eprintln!("`cjob status {}` で状態を確認してください。", job_id);
            bail!("タイムアウト");
        }

        let job = client.get_job(job_id).await?;
        let elapsed = start.elapsed();
        let mins = elapsed.as_secs() / 60;
        let secs = elapsed.as_secs() % 60;

        match job.status.as_str() {
            "QUEUED" | "DISPATCHING" | "DISPATCHED" => {
                eprint!(
                    "\rジョブ {} の開始を待機中... ({}) [{:01}:{:02}]",
                    job_id, job.status, mins, secs
                );
                io::stderr().flush().ok();
                tokio::time::sleep(POLL_INTERVAL).await;
            }
            "RUNNING" => {
                eprintln!("\nジョブ {} が開始しました。ログを追跡します。", job_id);
                return Ok(());
            }
            "SUCCEEDED" | "FAILED" => {
                eprintln!("\nジョブ {} は {} で完了しました。", job_id, job.status);
                return Ok(());
            }
            "CANCELLED" => {
                bail!("ジョブ {} はキャンセルされました。", job_id);
            }
            _ => {
                bail!("ジョブ {} の状態が不明です: {}", job_id, job.status);
            }
        }
    }
}

fn print_full_logs(log_dir: &str) -> Result<()> {
    let path = stdout_path(log_dir);
    if !path.exists() {
        println!("No logs available");
        return Ok(());
    }

    let content = std::fs::read_to_string(&path)?;
    print!("{}", content);

    // Also print stderr if it has content
    let err_path = stderr_path(log_dir);
    if err_path.exists() {
        let err_content = std::fs::read_to_string(&err_path)?;
        if !err_content.is_empty() {
            eprint!("{}", err_content);
        }
    }

    Ok(())
}

async fn tail_logs(log_dir: &str) -> Result<()> {
    let path = stdout_path(log_dir);

    // Wait for log file to appear
    let start = Instant::now();
    while !path.exists() {
        if start.elapsed() >= Duration::from_secs(30) {
            bail!("ログファイルの生成を待機中にタイムアウトしました");
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }

    let mut file = std::fs::File::open(&path)?;
    let mut buf = vec![0u8; 8192];

    loop {
        let n = file.read(&mut buf)?;
        if n > 0 {
            io::stdout().write_all(&buf[..n])?;
            io::stdout().flush()?;
        } else {
            tokio::time::sleep(TAIL_INTERVAL).await;
        }
    }
}

use anyhow::{bail, Result};
use std::io::{self, Read, Write};
use std::path::PathBuf;
use std::time::{Duration, Instant};

use crate::client::CjobClient;

const LOG_BASE_DIR: &str = "/home/jovyan/.cjob/logs";
const WAIT_TIMEOUT: Duration = Duration::from_secs(300); // 5 minutes
const POLL_INTERVAL: Duration = Duration::from_secs(3);
const TAIL_INTERVAL: Duration = Duration::from_millis(200);

fn log_dir(job_id: u32) -> PathBuf {
    PathBuf::from(format!("{}/{}", LOG_BASE_DIR, job_id))
}

fn stdout_path(job_id: u32) -> PathBuf {
    log_dir(job_id).join("stdout.log")
}

fn stderr_path(job_id: u32) -> PathBuf {
    log_dir(job_id).join("stderr.log")
}

pub async fn show_logs(job_id: u32, follow: bool, client: &CjobClient) -> Result<()> {
    // Get current job status
    let job = client.get_job(job_id).await?;

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
            print_full_logs(job_id)?;
            return Ok(());
        }
        "CANCELLED" => {
            if stdout_path(job_id).exists() {
                print_full_logs(job_id)?;
            } else {
                println!("No logs available");
            }
            return Ok(());
        }
        "DELETING" => {
            if stdout_path(job_id).exists() {
                print_full_logs(job_id)?;
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

    if follow {
        tail_logs(job_id).await
    } else {
        print_full_logs(job_id)
    }
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

fn print_full_logs(job_id: u32) -> Result<()> {
    let path = stdout_path(job_id);
    if !path.exists() {
        println!("No logs available");
        return Ok(());
    }

    let content = std::fs::read_to_string(&path)?;
    print!("{}", content);

    // Also print stderr if it has content
    let err_path = stderr_path(job_id);
    if err_path.exists() {
        let err_content = std::fs::read_to_string(&err_path)?;
        if !err_content.is_empty() {
            eprint!("{}", err_content);
        }
    }

    Ok(())
}

async fn tail_logs(job_id: u32) -> Result<()> {
    let path = stdout_path(job_id);

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

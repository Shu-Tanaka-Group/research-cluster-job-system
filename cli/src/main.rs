mod auth;
mod client;
mod display;
mod job_ids;
mod logs;

use anyhow::Result;
use clap::{Parser, Subcommand};
use std::collections::HashMap;

#[derive(Parser)]
#[command(name = "cjob", about = "CJob - ジョブキューシステム CLI")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// ジョブを投入する
    Add {
        /// コマンド（-- の後に指定）
        #[arg(trailing_var_arg = true, required = true)]
        command: Vec<String>,

        /// CPU リソース（例: "2"）
        #[arg(long, default_value = "1")]
        cpu: String,

        /// メモリリソース（例: "4Gi"）
        #[arg(long, default_value = "1Gi")]
        memory: String,
    },
    /// ジョブ一覧を表示する
    List {
        /// ステータスでフィルタ
        #[arg(long)]
        status: Option<String>,

        /// 表示件数を制限
        #[arg(long)]
        limit: Option<u32>,
    },
    /// ジョブの詳細を表示する
    Status {
        /// ジョブ ID
        job_id: u32,
    },
    /// ジョブをキャンセルする
    Cancel {
        /// ジョブ ID（例: 1, 1-5, 1,3,5, 1-5,8,10-12）
        job_ids: String,
    },
    /// 完了済みジョブを削除する
    Delete {
        /// ジョブ ID（例: 1, 1-5, 1,3,5）
        job_ids: Option<String>,

        /// 完了済みジョブを全て削除
        #[arg(long)]
        all: bool,
    },
    /// 全ジョブ履歴をリセットする
    Reset,
    /// ジョブのログを表示する
    Logs {
        /// ジョブ ID
        job_id: u32,

        /// リアルタイムでログを追跡する
        #[arg(long)]
        follow: bool,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    let token = auth::read_token()?;
    let api_client = client::CjobClient::new(token)?;

    match cli.command {
        Commands::Add {
            command,
            cpu,
            memory,
        } => cmd_add(&api_client, command, cpu, memory).await,
        Commands::List { status, limit } => cmd_list(&api_client, status, limit).await,
        Commands::Status { job_id } => cmd_status(&api_client, job_id).await,
        Commands::Cancel { job_ids } => cmd_cancel(&api_client, &job_ids).await,
        Commands::Delete { job_ids, all } => cmd_delete(&api_client, job_ids, all).await,
        Commands::Reset => cmd_reset(&api_client).await,
        Commands::Logs { job_id, follow } => logs::show_logs(job_id, follow, &api_client).await,
    }
}

async fn cmd_add(
    client: &client::CjobClient,
    command: Vec<String>,
    cpu: String,
    memory: String,
) -> Result<()> {
    let cwd = std::env::current_dir()?
        .to_string_lossy()
        .to_string();

    let image = std::env::var("JUPYTER_IMAGE")
        .unwrap_or_default();

    if image.is_empty() {
        anyhow::bail!("JUPYTER_IMAGE 環境変数が設定されていません");
    }

    // Collect exported environment variables
    let env: HashMap<String, String> = std::env::vars().collect();

    let cmd_str = command.join(" ");

    let req = client::JobSubmitRequest {
        command: cmd_str,
        image,
        cwd,
        env,
        resources: client::ResourceSpec {
            cpu,
            memory,
            gpu: 0,
        },
    };

    let resp = client.submit_job(&req).await?;
    println!("ジョブ {} を投入しました。({})", resp.job_id, resp.status);
    Ok(())
}

async fn cmd_list(
    client: &client::CjobClient,
    status: Option<String>,
    limit: Option<u32>,
) -> Result<()> {
    let resp = client
        .list_jobs(status.as_deref(), limit)
        .await?;
    display::print_job_table(&resp.jobs);
    Ok(())
}

async fn cmd_status(client: &client::CjobClient, job_id: u32) -> Result<()> {
    let resp = client.get_job(job_id).await?;
    display::print_job_detail(&resp);
    Ok(())
}

async fn cmd_cancel(client: &client::CjobClient, job_ids_expr: &str) -> Result<()> {
    let ids = job_ids::parse_job_ids(job_ids_expr)?;

    if ids.len() == 1 {
        let resp = client.cancel_single(ids[0]).await?;
        let status = resp
            .get("status")
            .and_then(|v| v.as_str())
            .unwrap_or("unknown");
        println!("ジョブ {}: {}", ids[0], status);
    } else {
        let resp = client.cancel_bulk(&ids).await?;
        if !resp.cancelled.is_empty() {
            println!("キャンセルしました: {:?}", resp.cancelled);
        }
        if !resp.skipped.is_empty() {
            println!("スキップしました（完了済み）: {:?}", resp.skipped);
        }
        if !resp.not_found.is_empty() {
            println!("見つかりませんでした: {:?}", resp.not_found);
        }
    }
    Ok(())
}

async fn cmd_delete(
    client: &client::CjobClient,
    job_ids_expr: Option<String>,
    all: bool,
) -> Result<()> {
    let job_ids = if all {
        None
    } else if let Some(expr) = job_ids_expr {
        Some(job_ids::parse_job_ids(&expr)?)
    } else {
        anyhow::bail!("job_id を指定するか --all を使用してください");
    };

    let resp = client.delete_jobs(job_ids).await?;

    if !resp.deleted.is_empty() {
        // Delete log directories for deleted jobs
        for jid in &resp.deleted {
            let log_dir = format!("/home/jovyan/.cjob/logs/{}", jid);
            let _ = std::fs::remove_dir_all(&log_dir);
        }
        println!("削除しました: {:?}", resp.deleted);
    }
    for item in &resp.skipped {
        match item.reason.as_str() {
            "running" => println!(
                "ジョブ {}: 実行中のため削除できませんでした。先に cjob cancel を実行してください",
                item.job_id
            ),
            "deleting" => println!(
                "ジョブ {}: リセット処理中のため削除できませんでした",
                item.job_id
            ),
            _ => println!("ジョブ {}: スキップ ({})", item.job_id, item.reason),
        }
    }
    if !resp.not_found.is_empty() {
        println!("見つかりませんでした: {:?}", resp.not_found);
    }
    Ok(())
}

async fn cmd_reset(client: &client::CjobClient) -> Result<()> {
    // Check current job status
    let list_resp = client.list_jobs(None, None).await?;

    // Check for DELETING jobs
    let has_deleting = list_resp.jobs.iter().any(|j| j.status == "DELETING");
    if has_deleting {
        println!("前回のリセット処理がまだ完了していません。しばらく待ってから再試行してください。");
        return Ok(());
    }

    // Check for active jobs
    let active: Vec<u32> = list_resp
        .jobs
        .iter()
        .filter(|j| matches!(j.status.as_str(), "QUEUED" | "DISPATCHING" | "DISPATCHED" | "RUNNING"))
        .map(|j| j.job_id)
        .collect();
    if !active.is_empty() {
        println!("完了していないジョブがあるためリセットできません。");
        println!("完了待ちのジョブ: {:?}", active);
        return Ok(());
    }

    let total = list_resp.jobs.len();
    if total == 0 {
        println!("リセットするジョブがありません。");
        return Ok(());
    }

    // Confirmation prompt
    eprint!("全 {} 件のジョブとログを削除します。よろしいですか？ [y/N] ", total);
    std::io::Write::flush(&mut std::io::stderr())?;
    let mut input = String::new();
    std::io::stdin().read_line(&mut input)?;
    if input.trim().to_lowercase() != "y" {
        println!("中止しました。");
        return Ok(());
    }

    // Delete log directory before API call
    let _ = std::fs::remove_dir_all("/home/jovyan/.cjob/logs");

    // Call reset API
    client.reset().await?;
    println!("リセットを開始しました。バックグラウンドでクリーンアップが完了するまでお待ちください。");
    Ok(())
}

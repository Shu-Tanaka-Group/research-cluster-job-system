mod auth;
mod client;
mod display;
mod job_ids;
mod logs;

use anyhow::Result;
use clap::{Parser, Subcommand};
use std::collections::HashMap;

#[derive(Parser)]
#[command(name = "cjob", about = "CJob - ジョブキューシステム CLI", version)]
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

        /// GPU 数（例: 1）
        #[arg(long, default_value = "0")]
        gpu: u32,

        /// ResourceFlavor 名（例: "cpu", "gpu-a100"）
        #[arg(long)]
        flavor: Option<String>,

        /// 実行時間の上限（例: 3600, 1h, 6h, 1d, 3d）
        #[arg(long = "time-limit")]
        time_limit: Option<String>,
    },
    /// パラメータスイープを投入する
    Sweep {
        /// タスク数
        #[arg(short = 'n', long = "count")]
        count: u32,

        /// 並列数
        #[arg(long = "parallel", default_value = "1")]
        parallel: u32,

        /// 実行時間の上限（例: 3600, 1h, 6h, 1d, 3d）
        #[arg(long = "time-limit")]
        time_limit: Option<String>,

        /// CPU リソース（例: "2"）
        #[arg(long, default_value = "1")]
        cpu: String,

        /// メモリリソース（例: "4Gi"）
        #[arg(long, default_value = "1Gi")]
        memory: String,

        /// GPU 数（例: 1）
        #[arg(long, default_value = "0")]
        gpu: u32,

        /// ResourceFlavor 名（例: "cpu", "gpu-a100"）
        #[arg(long)]
        flavor: Option<String>,

        /// コマンド（-- の後に指定）
        #[arg(trailing_var_arg = true, required = true)]
        command: Vec<String>,
    },
    /// ジョブ一覧を表示する
    List {
        /// ステータスでフィルタ
        #[arg(long)]
        status: Option<String>,

        /// 表示件数を制限
        #[arg(long)]
        limit: Option<u32>,

        /// JOB_ID の降順で表示する
        #[arg(long)]
        reverse: bool,

        /// 全件表示する
        #[arg(long)]
        all: bool,
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
    /// ジョブの実行を保留する
    Hold {
        /// ジョブ ID（例: 1, 1-5, 1,3,5, 1-5,8,10-12）
        job_ids: Option<String>,

        /// QUEUED 状態のジョブを全て保留にする
        #[arg(long)]
        all: bool,
    },
    /// 保留中のジョブをキューに戻す
    Release {
        /// ジョブ ID（例: 1, 1-5, 1,3,5, 1-5,8,10-12）
        job_ids: Option<String>,

        /// HELD 状態のジョブを全てキューに戻す
        #[arg(long)]
        all: bool,
    },
    /// 完了済みジョブを削除する
    Delete {
        /// ジョブ ID（例: 1, 1-5, 1,3,5）
        job_ids: Option<String>,

        /// 完了済みジョブを全て削除
        #[arg(long)]
        all: bool,
    },
    /// リソース使用状況を表示する
    Usage,
    /// 全ジョブ履歴をリセットする
    Reset,
    /// ジョブのログを表示する
    Logs {
        /// ジョブ ID
        job_id: u32,

        /// リアルタイムでログを追跡する
        #[arg(long)]
        follow: bool,

        /// スイープのインデックス指定
        #[arg(long)]
        index: Option<u32>,
    },
    /// 計算リソースの種類を表示する
    Flavor {
        #[command(subcommand)]
        action: FlavorCommands,
    },
    /// CLI を最新バージョンに更新する
    Update {
        /// プレリリース版を含める
        #[arg(long = "pre")]
        pre: bool,

        /// 確認をスキップする
        #[arg(short = 'y', long = "yes")]
        yes: bool,

        /// 利用可能なバージョン一覧を表示する
        #[arg(long = "list", conflicts_with = "version")]
        list: bool,

        /// 指定バージョンをインストールする
        #[arg(long = "version")]
        version: Option<String>,
    },
}

#[derive(Subcommand)]
enum FlavorCommands {
    /// 利用可能な種類の一覧を表示する
    List,
    /// 指定した種類のリソース上限を表示する
    Info {
        /// 種類の名前（例: cpu, gpu）
        name: String,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    if let Commands::Update { pre, yes, list, version } = cli.command {
        let api_client = client::CjobClient::new_without_auth()?;
        return cmd_update(&api_client, pre, yes, list, version).await;
    }

    if let Commands::Flavor { action } = cli.command {
        let api_client = client::CjobClient::new_without_auth()?;
        return match action {
            FlavorCommands::List => cmd_flavor_list(&api_client).await,
            FlavorCommands::Info { name } => cmd_flavor_info(&api_client, &name).await,
        };
    }

    let token = auth::read_token()?;
    let api_client = client::CjobClient::new(token)?;

    match cli.command {
        Commands::Add {
            command,
            cpu,
            memory,
            gpu,
            flavor,
            time_limit,
        } => cmd_add(&api_client, command, cpu, memory, gpu, flavor, time_limit).await,
        Commands::Sweep {
            count,
            parallel,
            time_limit,
            cpu,
            memory,
            gpu,
            flavor,
            command,
        } => cmd_sweep(&api_client, command, count, parallel, cpu, memory, gpu, flavor, time_limit).await,
        Commands::List { status, limit, reverse, all } => cmd_list(&api_client, status.map(|s| s.to_uppercase()), limit, reverse, all).await,
        Commands::Status { job_id } => cmd_status(&api_client, job_id).await,
        Commands::Cancel { job_ids } => cmd_cancel(&api_client, &job_ids).await,
        Commands::Hold { job_ids, all } => cmd_hold(&api_client, job_ids, all).await,
        Commands::Release { job_ids, all } => cmd_release(&api_client, job_ids, all).await,
        Commands::Delete { job_ids, all } => cmd_delete(&api_client, job_ids, all).await,
        Commands::Usage => cmd_usage(&api_client).await,
        Commands::Reset => cmd_reset(&api_client).await,
        Commands::Logs { job_id, follow, index } => logs::show_logs(job_id, follow, index, &api_client).await,
        Commands::Update { .. } => unreachable!(),
        Commands::Flavor { .. } => unreachable!(),
    }
}

/// Quote a command argument for safe embedding in a bash -lc string.
/// Uses double quotes so that shell variables (e.g. $CJOB_INDEX) expand
/// in the Job Pod. Characters special inside double quotes are escaped.
fn shell_quote(arg: &str) -> String {
    if !arg.is_empty()
        && !arg.contains(|c: char| c.is_whitespace() || "\"'\\$`!#&|;(){}".contains(c))
    {
        return arg.to_string();
    }
    let escaped = arg
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('`', "\\`")
        .replace('!', "\\!");
    format!("\"{}\"", escaped)
}

fn build_command_string(args: &[String]) -> String {
    args.iter()
        .map(|arg| shell_quote(arg))
        .collect::<Vec<_>>()
        .join(" ")
}

fn parse_duration(s: &str) -> Result<u32> {
    let s = s.trim();
    if let Ok(secs) = s.parse::<u32>() {
        return Ok(secs);
    }
    let (num_str, multiplier) = if s.ends_with('d') {
        (&s[..s.len() - 1], 86400u32)
    } else if s.ends_with('h') {
        (&s[..s.len() - 1], 3600u32)
    } else if s.ends_with('m') {
        (&s[..s.len() - 1], 60u32)
    } else if s.ends_with('s') {
        (&s[..s.len() - 1], 1u32)
    } else {
        anyhow::bail!("不正な時間指定です: {}（例: 3600, 30m, 1h, 6h, 1d, 3d）", s);
    };
    let num: u32 = num_str.parse().map_err(|_| {
        anyhow::anyhow!("不正な時間指定です: {}（例: 3600, 30m, 1h, 6h, 1d, 3d）", s)
    })?;
    num.checked_mul(multiplier)
        .ok_or_else(|| anyhow::anyhow!("時間指定が大きすぎます: {}", s))
}

async fn cmd_add(
    client: &client::CjobClient,
    command: Vec<String>,
    cpu: String,
    memory: String,
    gpu: u32,
    flavor: Option<String>,
    time_limit: Option<String>,
) -> Result<()> {
    let cwd = std::env::current_dir()?
        .to_string_lossy()
        .to_string();

    let image = std::env::var("CJOB_IMAGE")
        .or_else(|_| std::env::var("JUPYTER_IMAGE"))
        .unwrap_or_default();

    if image.is_empty() {
        anyhow::bail!("CJOB_IMAGE または JUPYTER_IMAGE 環境変数が設定されていません");
    }

    // Collect exported environment variables
    let env: HashMap<String, String> = std::env::vars().collect();

    let cmd_str = build_command_string(&command);

    let time_limit_seconds = match time_limit {
        Some(ref s) => Some(parse_duration(s)?),
        None => None,
    };

    let req = client::JobSubmitRequest {
        command: cmd_str,
        image,
        cwd,
        env,
        resources: client::ResourceSpec {
            cpu,
            memory,
            gpu,
            flavor,
        },
        time_limit_seconds,
    };

    let resp = client.submit_job(&req).await?;
    println!("ジョブ {} を投入しました。({})", resp.job_id, resp.status);
    Ok(())
}

async fn cmd_sweep(
    client: &client::CjobClient,
    command: Vec<String>,
    count: u32,
    parallel: u32,
    cpu: String,
    memory: String,
    gpu: u32,
    flavor: Option<String>,
    time_limit: Option<String>,
) -> Result<()> {
    let cwd = std::env::current_dir()?
        .to_string_lossy()
        .to_string();

    let image = std::env::var("CJOB_IMAGE")
        .or_else(|_| std::env::var("JUPYTER_IMAGE"))
        .unwrap_or_default();

    if image.is_empty() {
        anyhow::bail!("CJOB_IMAGE または JUPYTER_IMAGE 環境変数が設定されていません");
    }

    // Collect exported environment variables
    let env: HashMap<String, String> = std::env::vars().collect();

    // Replace _INDEX_ placeholder with $CJOB_INDEX before quoting.
    // Double-quote strategy ensures $CJOB_INDEX expands in the Job Pod.
    let quoted_args: Vec<String> = command
        .iter()
        .map(|arg| arg.replace("_INDEX_", "$CJOB_INDEX"))
        .collect();
    let cmd_str = build_command_string(&quoted_args);

    let time_limit_seconds = match time_limit {
        Some(ref s) => Some(parse_duration(s)?),
        None => None,
    };

    let req = client::SweepSubmitRequest {
        command: cmd_str,
        image,
        cwd,
        env,
        resources: client::ResourceSpec {
            cpu,
            memory,
            gpu,
            flavor,
        },
        completions: count,
        parallelism: parallel,
        time_limit_seconds,
    };

    let resp = client.submit_sweep(&req).await?;
    println!(
        "スイープ {} を投入しました。({}, {} タスク, 並列 {})",
        resp.job_id, resp.status, count, parallel
    );
    Ok(())
}

const DEFAULT_LIST_LIMIT: u32 = 50;

async fn cmd_list(
    client: &client::CjobClient,
    status: Option<String>,
    limit: Option<u32>,
    reverse: bool,
    all: bool,
) -> Result<()> {
    if let Some(0) = limit {
        anyhow::bail!("--limit には 1 以上の値を指定してください");
    }

    let effective_limit = if all {
        None
    } else {
        Some(limit.unwrap_or(DEFAULT_LIST_LIMIT))
    };
    let order = if reverse { "desc" } else { "asc" };

    let resp = client
        .list_jobs(status.as_deref(), effective_limit, Some(order))
        .await?;
    display::print_job_table(&resp.jobs);

    if let Some(lim) = effective_limit {
        if resp.total_count > lim {
            eprintln!(
                "（{}件中最新の{}件を表示。全件表示するには --all を使用してください）",
                resp.total_count, lim
            );
        }
    }

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

async fn cmd_hold(
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

    if let Some(ref ids) = job_ids {
        if ids.len() == 1 {
            let resp = client.hold_single(ids[0]).await?;
            let status = resp
                .get("status")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            println!("ジョブ {}: {}", ids[0], status);
            return Ok(());
        }
    }

    let resp = client.hold_bulk(job_ids).await?;
    if !resp.held.is_empty() {
        println!("保留しました: {:?}", resp.held);
    }
    if !resp.skipped.is_empty() {
        println!("スキップしました（QUEUED 以外）: {:?}", resp.skipped);
    }
    if !resp.not_found.is_empty() {
        println!("見つかりませんでした: {:?}", resp.not_found);
    }
    Ok(())
}

async fn cmd_release(
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

    if let Some(ref ids) = job_ids {
        if ids.len() == 1 {
            let resp = client.release_single(ids[0]).await?;
            let status = resp
                .get("status")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown");
            println!("ジョブ {}: {}", ids[0], status);
            return Ok(());
        }
    }

    let resp = client.release_bulk(job_ids).await?;
    if !resp.released.is_empty() {
        println!("キューに戻しました: {:?}", resp.released);
    }
    if !resp.skipped.is_empty() {
        println!("スキップしました（HELD 以外）: {:?}", resp.skipped);
    }
    if !resp.not_found.is_empty() {
        println!("見つかりませんでした: {:?}", resp.not_found);
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
        for log_dir in &resp.log_dirs {
            let _ = std::fs::remove_dir_all(log_dir);
        }
        println!("削除しました: {:?}", resp.deleted);
    }
    for item in &resp.skipped {
        match item.reason.as_str() {
            "running" => println!(
                "ジョブ {}: 実行中のため削除できませんでした。先に cjob cancel を実行してください",
                item.job_id
            ),
            "held" => println!(
                "ジョブ {}: 保留中のため削除できませんでした。先に cjob cancel または cjob release を実行してください",
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
    let list_resp = client.list_jobs(None, None, None).await?;

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
        .filter(|j| matches!(j.status.as_str(), "QUEUED" | "DISPATCHING" | "DISPATCHED" | "RUNNING" | "HELD"))
        .map(|j| j.job_id)
        .collect();
    if !active.is_empty() {
        println!("完了していないジョブがあるためリセットできません。");
        let active_str: Vec<String> = active.iter().map(|id| id.to_string()).collect();
        println!("完了待ちのジョブ: {}", active_str.join(", "));
        return Ok(());
    }

    let total = list_resp.jobs.len();
    if total == 0 {
        println!("リセットするジョブがありません。");
        return Ok(());
    }

    // Confirmation prompt
    if !confirm(&format!("全 {} 件のジョブとログを削除します。よろしいですか？", total))? {
        println!("中止しました。");
        return Ok(());
    }

    // Delete log directory before API call
    let _ = std::fs::remove_dir_all(&list_resp.log_base_dir);

    // Call reset API
    client.reset().await?;
    println!("リセットを開始しました。バックグラウンドでクリーンアップが完了するまでお待ちください。");
    Ok(())
}

async fn cmd_usage(client: &client::CjobClient) -> Result<()> {
    let resp = client.get_usage().await?;

    println!(
        "\nResource Usage (past {} days)",
        resp.window_days
    );
    println!("{}", "─".repeat(50));

    if resp.daily.is_empty() {
        println!("  使用実績がありません。");
    } else {
        // Check if GPU column is needed
        let has_gpu = resp.total_gpu_seconds > 0;

        if has_gpu {
            println!(
                "  {:<12} {:>14} {:>14} {:>10}",
                "Date", "CPU (core·h)", "Mem (GiB·h)", "GPU (h)"
            );
        } else {
            println!(
                "  {:<12} {:>14} {:>14}",
                "Date", "CPU (core·h)", "Mem (GiB·h)"
            );
        }

        for d in &resp.daily {
            let cpu_h = d.cpu_millicores_seconds as f64 / 1000.0 / 3600.0;
            let mem_h = d.memory_mib_seconds as f64 / 1024.0 / 3600.0;
            if has_gpu {
                let gpu_h = d.gpu_seconds as f64 / 3600.0;
                println!("  {:<12} {:>14.1} {:>14.1} {:>10.1}", d.date, cpu_h, mem_h, gpu_h);
            } else {
                println!("  {:<12} {:>14.1} {:>14.1}", d.date, cpu_h, mem_h);
            }
        }

        println!("  {}", "─".repeat(48));

        let total_cpu_h = resp.total_cpu_millicores_seconds as f64 / 1000.0 / 3600.0;
        let total_mem_h = resp.total_memory_mib_seconds as f64 / 1024.0 / 3600.0;
        if has_gpu {
            let total_gpu_h = resp.total_gpu_seconds as f64 / 3600.0;
            println!("  {:<12} {:>14.1} {:>14.1} {:>10.1}", "Total", total_cpu_h, total_mem_h, total_gpu_h);
        } else {
            println!("  {:<12} {:>14.1} {:>14.1}", "Total", total_cpu_h, total_mem_h);
        }
    }

    println!();
    Ok(())
}

fn confirm(message: &str) -> Result<bool> {
    eprint!("{} [y/N] ", message);
    std::io::Write::flush(&mut std::io::stderr())?;
    let mut input = String::new();
    std::io::stdin().read_line(&mut input)?;
    Ok(input.trim().to_lowercase() == "y")
}

async fn cmd_update(
    client: &client::CjobClient,
    pre: bool,
    yes: bool,
    list: bool,
    version: Option<String>,
) -> Result<()> {
    let current_version = env!("CARGO_PKG_VERSION");

    if list {
        return cmd_update_list(client, pre, current_version).await;
    }

    let target_version = if let Some(ref v) = version {
        v.clone()
    } else if pre {
        let resp = client.get_cli_versions().await?;
        match resp.versions.first() {
            Some(v) => v.clone(),
            None => anyhow::bail!("利用可能なバージョンがありません"),
        }
    } else {
        let resp = client.get_cli_version().await?;
        resp.version
    };

    if current_version == target_version {
        println!("すでに最新バージョンです ({})", current_version);
        return Ok(());
    }

    if !yes {
        if !confirm(&format!(
            "更新しますか？ {} → {}",
            current_version, target_version
        ))? {
            println!("中止しました。");
            return Ok(());
        }
    }

    let binary = client.download_cli_binary(Some(&target_version)).await?;
    replace_binary(&binary)?;

    println!("更新が完了しました。({})", target_version);
    Ok(())
}

async fn cmd_update_list(
    client: &client::CjobClient,
    pre: bool,
    current_version: &str,
) -> Result<()> {
    let resp = client.get_cli_versions().await?;

    let versions: Vec<&str> = if pre {
        resp.versions.iter().map(|s| s.as_str()).collect()
    } else {
        resp.versions.iter().filter(|v| !v.contains('-')).map(|s| s.as_str()).collect()
    };

    if versions.is_empty() {
        println!("利用可能なバージョンがありません。");
        return Ok(());
    }

    for v in &versions {
        let mut markers = Vec::new();
        if *v == resp.latest {
            markers.push("latest");
        }
        if *v == current_version {
            markers.push("current");
        }
        if markers.is_empty() {
            println!("{}", v);
        } else {
            println!("{} ({})", v, markers.join(", "));
        }
    }

    Ok(())
}

async fn cmd_flavor_list(client: &client::CjobClient) -> Result<()> {
    let resp = client.get_flavors().await?;

    println!("{:<16} {:<6} {:<8} {}", "NAME", "GPU", "NODES", "DEFAULT");
    for f in &resp.flavors {
        let gpu = if f.has_gpu { "yes" } else { "-" };
        let default_marker = if f.name == resp.default_flavor { "  *" } else { "" };
        println!("{:<16} {:<6} {:<8} {}", f.name, gpu, f.nodes.len(), default_marker);
    }
    Ok(())
}

async fn cmd_flavor_info(client: &client::CjobClient, name: &str) -> Result<()> {
    let resp = client.get_flavors().await?;

    let flavor = resp.flavors.iter().find(|f| f.name == name);
    let flavor = match flavor {
        Some(f) => f,
        None => {
            let available: Vec<&str> = resp.flavors.iter().map(|f| f.name.as_str()).collect();
            anyhow::bail!(
                "flavor '{}' は存在しません。利用可能な flavor: {}",
                name,
                available.join(", ")
            );
        }
    };

    println!("name:   {}", flavor.name);
    println!("GPU:    {}", if flavor.has_gpu { "対応" } else { "非対応" });

    if flavor.nodes.is_empty() {
        println!();
        println!("（ノード情報がまだ取得されていません）");
        return Ok(());
    }

    println!();
    if flavor.has_gpu {
        println!("{:<24} {:>12} {:>14} {:>6}", "NODE", "CPU (cores)", "Memory (GiB)", "GPU");
        for n in &flavor.nodes {
            println!(
                "{:<24} {:>12} {:>14.1} {:>6}",
                n.node_name,
                n.cpu_millicores / 1000,
                n.memory_mib as f64 / 1024.0,
                n.gpu,
            );
        }
    } else {
        println!("{:<24} {:>12} {:>14}", "NODE", "CPU (cores)", "Memory (GiB)");
        for n in &flavor.nodes {
            println!(
                "{:<24} {:>12} {:>14.1}",
                n.node_name,
                n.cpu_millicores / 1000,
                n.memory_mib as f64 / 1024.0,
            );
        }
    }

    Ok(())
}

fn replace_binary(binary: &[u8]) -> Result<()> {
    use anyhow::Context;
    use std::os::unix::fs::PermissionsExt;

    let current_exe = std::env::current_exe()
        .context("実行ファイルのパスを取得できませんでした")?;
    let tmp_path = current_exe
        .parent()
        .ok_or_else(|| anyhow::anyhow!("実行ファイルの親ディレクトリを取得できませんでした"))?
        .join(".cjob.update.tmp");

    std::fs::write(&tmp_path, binary)
        .context("一時ファイルの書き込みに失敗しました")?;
    std::fs::set_permissions(&tmp_path, std::fs::Permissions::from_mode(0o755))
        .context("実行権限の設定に失敗しました")?;

    if let Err(e) = std::fs::rename(&tmp_path, &current_exe) {
        let _ = std::fs::remove_file(&tmp_path);
        return Err(anyhow::anyhow!(e).context("バイナリの置き換えに失敗しました"));
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_duration_raw_seconds() {
        assert_eq!(parse_duration("3600").unwrap(), 3600);
        assert_eq!(parse_duration("1").unwrap(), 1);
        assert_eq!(parse_duration("0").unwrap(), 0);
    }

    #[test]
    fn test_parse_duration_seconds_suffix() {
        assert_eq!(parse_duration("60s").unwrap(), 60);
        assert_eq!(parse_duration("3600s").unwrap(), 3600);
    }

    #[test]
    fn test_parse_duration_minutes() {
        assert_eq!(parse_duration("1m").unwrap(), 60);
        assert_eq!(parse_duration("30m").unwrap(), 1800);
        assert_eq!(parse_duration("90m").unwrap(), 5400);
    }

    #[test]
    fn test_parse_duration_hours() {
        assert_eq!(parse_duration("1h").unwrap(), 3600);
        assert_eq!(parse_duration("6h").unwrap(), 21600);
        assert_eq!(parse_duration("24h").unwrap(), 86400);
    }

    #[test]
    fn test_parse_duration_days() {
        assert_eq!(parse_duration("1d").unwrap(), 86400);
        assert_eq!(parse_duration("3d").unwrap(), 259200);
        assert_eq!(parse_duration("7d").unwrap(), 604800);
    }

    #[test]
    fn test_parse_duration_whitespace() {
        assert_eq!(parse_duration(" 1h ").unwrap(), 3600);
    }

    #[test]
    fn test_parse_duration_invalid_suffix() {
        assert!(parse_duration("1x").is_err());
        assert!(parse_duration("abc").is_err());
    }

    #[test]
    fn test_parse_duration_invalid_number() {
        assert!(parse_duration("abch").is_err());
        assert!(parse_duration("-1h").is_err());
    }

    #[test]
    fn test_parse_duration_overflow() {
        assert!(parse_duration("99999999d").is_err());
    }

    // ── shell_quote / build_command_string ──

    #[test]
    fn test_shell_quote_simple() {
        assert_eq!(shell_quote("echo"), "echo");
        assert_eq!(shell_quote("main.py"), "main.py");
        assert_eq!(shell_quote("--alpha"), "--alpha");
    }

    #[test]
    fn test_shell_quote_with_spaces() {
        assert_eq!(shell_quote("hello world"), "\"hello world\"");
    }

    #[test]
    fn test_shell_quote_with_double_quote() {
        assert_eq!(shell_quote("say \"hi\""), "\"say \\\"hi\\\"\"");
    }

    #[test]
    fn test_shell_quote_with_single_quote() {
        assert_eq!(shell_quote("it's"), "\"it's\"");
    }

    #[test]
    fn test_shell_quote_with_dollar() {
        // $ is not escaped — allows shell variable expansion in Job Pod
        assert_eq!(shell_quote("$HOME"), "\"$HOME\"");
    }

    #[test]
    fn test_shell_quote_with_backslash() {
        assert_eq!(shell_quote("a\\b"), "\"a\\\\b\"");
    }

    #[test]
    fn test_shell_quote_with_backtick() {
        assert_eq!(shell_quote("a`b"), "\"a\\`b\"");
    }

    #[test]
    fn test_shell_quote_empty() {
        assert_eq!(shell_quote(""), "\"\"");
    }

    #[test]
    fn test_build_command_string_simple() {
        let args = vec!["echo".into(), "hello".into()];
        assert_eq!(build_command_string(&args), "echo hello");
    }

    #[test]
    fn test_build_command_string_with_spaces() {
        let args = vec!["echo".into(), "hello world".into()];
        assert_eq!(build_command_string(&args), "echo \"hello world\"");
    }

    #[test]
    fn test_build_command_string_sweep_placeholder() {
        // Simulates what cmd_sweep does: replace _INDEX_ then build
        let args: Vec<String> = vec!["python".into(), "main.py".into(), "--trial".into(), "$CJOB_INDEX".into()];
        assert_eq!(
            build_command_string(&args),
            "python main.py --trial \"$CJOB_INDEX\""
        );
    }

    #[test]
    fn test_build_command_string_sweep_placeholder_in_phrase() {
        let args: Vec<String> = vec!["echo".into(), "index=$CJOB_INDEX".into()];
        assert_eq!(
            build_command_string(&args),
            "echo \"index=$CJOB_INDEX\""
        );
    }
}

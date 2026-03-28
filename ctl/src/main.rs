mod cmd;
mod config;
mod db;
mod k8s;

use anyhow::Result;
use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "cjobctl", about = "CJob admin CLI", version)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Inspect job state in the database
    Jobs {
        #[command(subcommand)]
        command: JobsCommands,
    },
    /// Manage resource usage data
    Usage {
        #[command(subcommand)]
        command: UsageCommands,
    },
    /// Inspect job counters
    Counters {
        #[command(subcommand)]
        command: CountersCommands,
    },
    /// Show pod status in cjob-system namespace
    Status,
    /// Show component logs
    Logs {
        /// Component name (dispatcher, watcher, submit-api)
        component: String,
        /// Number of lines to show
        #[arg(long, default_value = "50")]
        tail: i64,
    },
    /// Show cjob-config ConfigMap
    Config {
        #[command(subcommand)]
        command: ConfigCommands,
    },
    /// Manage namespace weights for fair sharing
    Weight {
        #[command(subcommand)]
        command: WeightCommands,
    },
    /// Manage cluster resources and ClusterQueue quota
    Cluster {
        #[command(subcommand)]
        command: ClusterCommands,
    },
    /// Manage database schema
    Db {
        #[command(subcommand)]
        command: DbCommands,
    },
    /// Manage CLI binary distribution
    Cli {
        #[command(subcommand)]
        command: CliCommands,
    },
}

#[derive(Subcommand)]
enum JobsCommands {
    /// List all jobs
    List {
        /// Filter by namespace
        #[arg(long)]
        namespace: Option<String>,
        /// Filter by status
        #[arg(long)]
        status: Option<String>,
    },
    /// Show DISPATCHED jobs that appear stuck
    Stalled,
    /// Show remaining time for RUNNING jobs
    Remaining,
    /// Show job count by namespace and status
    Summary,
    /// Cancel jobs in a namespace
    Cancel {
        /// Target namespace (required)
        #[arg(long)]
        namespace: String,
        /// Cancel a specific job by ID
        #[arg(long)]
        job_id: Option<i32>,
        /// Cancel all jobs with this status (e.g. RUNNING, QUEUED, DISPATCHED)
        #[arg(long)]
        status: Option<String>,
        /// Cancel all active jobs in the namespace
        #[arg(long)]
        all: bool,
    },
}

#[derive(Subcommand)]
enum UsageCommands {
    /// Show daily usage, 7-day aggregate, and DRF dominant share
    List,
    /// Reset usage data
    Reset {
        /// Target namespace
        #[arg(long)]
        namespace: Option<String>,
        /// Reset all namespaces
        #[arg(long)]
        all: bool,
    },
}

#[derive(Subcommand)]
enum CountersCommands {
    /// List job counters per namespace
    List,
}

#[derive(Subcommand)]
enum ConfigCommands {
    /// Show cjob-config ConfigMap contents
    Show,
}

#[derive(Subcommand)]
enum WeightCommands {
    /// List all namespace weight overrides
    List,
    /// Set weight for a namespace
    Set {
        /// Target namespace (e.g. user-alice)
        namespace: String,
        /// Weight value (>= 0)
        weight: i32,
    },
    /// Reset weight for a namespace to default (1)
    Reset {
        /// Target namespace
        namespace: String,
    },
    /// Give exclusive cluster access to a namespace
    Exclusive {
        /// Namespace to grant exclusive access (omit with --release)
        namespace: Option<String>,
        /// Release exclusive mode (reset all weights)
        #[arg(long)]
        release: bool,
    },
}

#[derive(Subcommand)]
enum ClusterCommands {
    /// Show node resources, cluster totals, and rejection thresholds
    Resources,
    /// Show current ClusterQueue nominalQuota
    ShowQuota,
    /// Update ClusterQueue nominalQuota
    SetQuota {
        /// CPU cores (e.g. 256)
        #[arg(long)]
        cpu: Option<u32>,
        /// Memory with unit (e.g. 1000Gi)
        #[arg(long)]
        memory: Option<String>,
        /// GPU count (e.g. 4)
        #[arg(long)]
        gpu: Option<u32>,
        /// Allow values exceeding cluster allocatable total
        #[arg(long)]
        force: bool,
    },
}

#[derive(Subcommand)]
enum DbCommands {
    /// Run idempotent schema migration
    Migrate,
}

#[derive(Subcommand)]
enum CliCommands {
    /// List deployed CLI versions on PVC
    List,
    /// Deploy CLI binary to PVC
    Deploy {
        /// Path to the CLI binary
        #[arg(long)]
        binary: String,
        /// Version string (e.g. 1.2.0, 1.3.0-beta.1)
        #[arg(long)]
        version: String,
        /// Force update latest even for pre-release versions
        #[arg(long)]
        latest: bool,
    },
    /// Remove a deployed CLI version from PVC
    Remove {
        /// Version to remove (e.g. 1.1.0)
        version: String,
    },
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        // --- DB-only commands ---
        Commands::Jobs { command } => {
            let config = config::Config::load()?;
            let conn = db::connect(&config.database, config.system_namespace()).await?;
            match command {
                JobsCommands::List { namespace, status } => {
                    let status_upper = status.map(|s| s.to_uppercase());
                    cmd::jobs::list(&conn.client, namespace.as_deref(), status_upper.as_deref()).await
                }
                JobsCommands::Stalled => cmd::jobs::stalled(&conn.client).await,
                JobsCommands::Remaining => cmd::jobs::remaining(&conn.client).await,
                JobsCommands::Summary => cmd::jobs::summary(&conn.client).await,
                JobsCommands::Cancel { namespace, job_id, status, all } => {
                    let status_upper = status.map(|s| s.to_uppercase());
                    cmd::jobs::cancel(&conn.client, &namespace, job_id, status_upper.as_deref(), all).await
                }
            }
        }
        Commands::Counters { command } => {
            let config = config::Config::load()?;
            let conn = db::connect(&config.database, config.system_namespace()).await?;
            match command {
                CountersCommands::List => cmd::counters::list(&conn.client).await,
            }
        }
        Commands::Usage { command } => {
            let config = config::Config::load()?;
            let conn = db::connect(&config.database, config.system_namespace()).await?;
            match command {
                UsageCommands::List => {
                    let totals = cmd::usage::ClusterTotals::from_db(&conn.client).await;
                    cmd::usage::list(&conn.client, &totals).await
                }
                UsageCommands::Reset { namespace, all } => {
                    cmd::usage::reset(&conn.client, namespace.as_deref(), all).await
                }
            }
        }
        Commands::Weight { command } => {
            let config = config::Config::load()?;
            let conn = db::connect(&config.database, config.system_namespace()).await?;
            match command {
                WeightCommands::List => cmd::weight::list(&conn.client).await,
                WeightCommands::Set { namespace, weight } => {
                    cmd::weight::set(&conn.client, &namespace, weight).await
                }
                WeightCommands::Reset { namespace } => {
                    cmd::weight::reset(&conn.client, &namespace).await
                }
                WeightCommands::Exclusive { namespace, release } => {
                    if release {
                        cmd::weight::release(&conn.client).await
                    } else {
                        let ns = namespace
                            .as_deref()
                            .ok_or_else(|| anyhow::anyhow!("Specify a namespace or use --release"))?;
                        // Fetch user namespaces from K8s
                        let k8s_client = k8s::client().await?;
                        let user_namespaces =
                            fetch_user_namespaces(&k8s_client).await?;
                        cmd::weight::exclusive(&conn.client, ns, &user_namespaces).await
                    }
                }
            }
        }
        Commands::Cluster { command } => {
            let config = config::Config::load()?;
            match command {
                ClusterCommands::Resources => {
                    let conn =
                        db::connect(&config.database, config.system_namespace()).await?;
                    cmd::cluster::resources(&conn.client).await
                }
                ClusterCommands::ShowQuota => {
                    let k8s_client = k8s::client().await?;
                    cmd::cluster::show_quota(&k8s_client).await
                }
                ClusterCommands::SetQuota { cpu, memory, gpu, force } => {
                    let conn =
                        db::connect(&config.database, config.system_namespace()).await?;
                    let k8s_client = k8s::client().await?;
                    cmd::cluster::set_quota(
                        &conn.client,
                        &k8s_client,
                        cpu,
                        memory.as_deref(),
                        gpu,
                        force,
                    )
                    .await
                }
            }
        }
        Commands::Db { command } => {
            let config = config::Config::load()?;
            let conn = db::connect(&config.database, config.system_namespace()).await?;
            match command {
                DbCommands::Migrate => cmd::db_migrate::migrate(&conn.client).await,
            }
        }

        // --- K8s-only commands ---
        Commands::Status => {
            let config = config::Config::load()?;
            let k8s_client = k8s::client().await?;
            cmd::status::run(&k8s_client, config.system_namespace()).await
        }
        Commands::Logs { component, tail } => {
            let config = config::Config::load()?;
            let k8s_client = k8s::client().await?;
            cmd::logs::run(&k8s_client, config.system_namespace(), &component, tail).await
        }
        Commands::Config { command } => {
            let config = config::Config::load()?;
            let k8s_client = k8s::client().await?;
            match command {
                ConfigCommands::Show => {
                    cmd::config_show::run(&k8s_client, config.system_namespace()).await
                }
            }
        }
        Commands::Cli { command } => {
            let config = config::Config::load()?;
            match command {
                CliCommands::List => {
                    cmd::cli_list::run(config.system_namespace()).await
                }
                CliCommands::Deploy { binary, version, latest } => {
                    cmd::cli_deploy::run(config.system_namespace(), &binary, &version, latest).await
                }
                CliCommands::Remove { version } => {
                    cmd::cli_remove::run(config.system_namespace(), &version).await
                }
            }
        }
    }
}

async fn fetch_user_namespaces(k8s_client: &kube::Client) -> Result<Vec<String>> {
    use k8s_openapi::api::core::v1::Namespace;
    use kube::api::ListParams;
    use kube::Api;

    let ns_api: Api<Namespace> = Api::all(k8s_client.clone());
    let lp = ListParams::default().labels("cjob.io/user-namespace=true");
    let ns_list = ns_api.list(&lp).await?;

    let names: Vec<String> = ns_list
        .items
        .iter()
        .filter_map(|ns| ns.metadata.name.clone())
        .collect();

    Ok(names)
}

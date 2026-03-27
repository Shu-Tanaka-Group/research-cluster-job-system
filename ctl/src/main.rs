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
    /// Manage database schema
    Db {
        #[command(subcommand)]
        command: DbCommands,
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
enum DbCommands {
    /// Run idempotent schema migration
    Migrate,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        // --- DB-only commands ---
        Commands::Jobs { command } => {
            let config = config::Config::load()?;
            let client = db::connect(&config.database).await?;
            match command {
                JobsCommands::List { namespace, status } => {
                    cmd::jobs::list(&client, namespace.as_deref(), status.as_deref()).await
                }
                JobsCommands::Stalled => cmd::jobs::stalled(&client).await,
                JobsCommands::Remaining => cmd::jobs::remaining(&client).await,
                JobsCommands::Summary => cmd::jobs::summary(&client).await,
            }
        }
        Commands::Counters { command } => {
            let config = config::Config::load()?;
            let client = db::connect(&config.database).await?;
            match command {
                CountersCommands::List => cmd::counters::list(&client).await,
            }
        }
        Commands::Usage { command } => {
            let config = config::Config::load()?;
            let db_client = db::connect(&config.database).await?;
            match command {
                UsageCommands::List => {
                    // Try to fetch cluster totals from K8s ConfigMap
                    let totals = match k8s::client().await {
                        Ok(k8s_client) => {
                            let fetcher = cmd::config_show::parse_cluster_totals(
                                &k8s_client,
                                config.system_namespace(),
                            );
                            fetcher.fetch().await
                        }
                        Err(_) => {
                            eprintln!("Warning: K8s unavailable. Using default cluster totals.");
                            cmd::usage::ClusterTotals::default()
                        }
                    };
                    cmd::usage::list(&db_client, &totals).await
                }
                UsageCommands::Reset { namespace, all } => {
                    cmd::usage::reset(&db_client, namespace.as_deref(), all).await
                }
            }
        }
        Commands::Weight { command } => {
            let config = config::Config::load()?;
            let db_client = db::connect(&config.database).await?;
            match command {
                WeightCommands::List => cmd::weight::list(&db_client).await,
                WeightCommands::Set { namespace, weight } => {
                    cmd::weight::set(&db_client, &namespace, weight).await
                }
                WeightCommands::Reset { namespace } => {
                    cmd::weight::reset(&db_client, &namespace).await
                }
                WeightCommands::Exclusive { namespace, release } => {
                    if release {
                        cmd::weight::release(&db_client).await
                    } else {
                        let ns = namespace
                            .as_deref()
                            .ok_or_else(|| anyhow::anyhow!("Specify a namespace or use --release"))?;
                        // Fetch user namespaces from K8s
                        let k8s_client = k8s::client().await?;
                        let user_namespaces =
                            fetch_user_namespaces(&k8s_client).await?;
                        cmd::weight::exclusive(&db_client, ns, &user_namespaces).await
                    }
                }
            }
        }
        Commands::Db { command } => {
            let config = config::Config::load()?;
            let client = db::connect(&config.database).await?;
            match command {
                DbCommands::Migrate => cmd::db_migrate::migrate(&client).await,
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

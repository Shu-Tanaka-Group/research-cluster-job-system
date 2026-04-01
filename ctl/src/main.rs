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
    /// Manage CJob system components
    System {
        #[command(subcommand)]
        command: SystemCommands,
    },
    /// Manage cjob-config ConfigMap
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
    /// Manage user namespaces
    User {
        #[command(subcommand)]
        command: UserCommands,
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
        /// Sort by field (NAMESPACE, CREATED, FINISHED)
        #[arg(long)]
        sort: Option<String>,
        /// Reverse sort order (descending)
        #[arg(long)]
        reverse: bool,
        /// Output format (wide: show resources and node)
        #[arg(short, long)]
        output: Option<String>,
    },
    /// Show DISPATCHED jobs that appear stuck
    Stalled {
        /// Sort by field (NAMESPACE, CREATED)
        #[arg(long)]
        sort: Option<String>,
        /// Reverse sort order (descending)
        #[arg(long)]
        reverse: bool,
    },
    /// Show remaining time for RUNNING jobs
    Remaining {
        /// Sort by field (NAMESPACE, CREATED)
        #[arg(long)]
        sort: Option<String>,
        /// Reverse sort order (descending)
        #[arg(long)]
        reverse: bool,
    },
    /// Show detailed status of a specific job
    Status {
        /// Target namespace (required)
        #[arg(long)]
        namespace: String,
        /// Job ID (required)
        #[arg(long)]
        job_id: i32,
    },
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
    List {
        /// Filter by namespace
        #[arg(long)]
        namespace: Option<String>,
    },
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
    /// Set a config value in cjob-config ConfigMap
    Set {
        /// Config key name (e.g. DISPATCH_BATCH_SIZE)
        key: String,
        /// Value to set (mutually exclusive with --from-file)
        #[arg(conflicts_with = "from_file")]
        value: Option<String>,
        /// Read value from file (for JSON values like RESOURCE_FLAVORS)
        #[arg(long, conflicts_with = "value")]
        from_file: Option<String>,
        /// Skip confirmation prompt
        #[arg(long)]
        yes: bool,
    },
    /// Dump cjob-config ConfigMap as clean YAML (suitable for kubectl apply)
    Dump,
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
    /// Show ResourceFlavor resource usage
    FlavorUsage,
    /// Show current ClusterQueue nominalQuota
    ShowQuota,
    /// Update ClusterQueue nominalQuota for a specific ResourceFlavor
    SetQuota {
        /// ResourceFlavor name (e.g. cpu, gpu)
        #[arg(long)]
        flavor: String,
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
enum UserCommands {
    /// List user namespaces
    List {
        /// Show only CJob-enabled namespaces
        #[arg(long, conflicts_with = "disabled")]
        enabled: bool,
        /// Show only CJob-disabled namespaces
        #[arg(long, conflicts_with = "enabled")]
        disabled: bool,
    },
    /// Enable CJob for namespace(s)
    Enable {
        /// Target namespace(s) (e.g. user-alice user-bob)
        #[arg(long, required = true, num_args = 1..)]
        namespace: Vec<String>,
    },
    /// Disable CJob for namespace(s)
    Disable {
        /// Target namespace(s) (e.g. user-alice user-bob)
        #[arg(long, required = true, num_args = 1..)]
        namespace: Vec<String>,
    },
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
        /// Update latest to this version (not allowed for pre-release versions)
        #[arg(long)]
        release: bool,
    },
    /// Remove deployed CLI version(s) from PVC
    Remove {
        /// Version(s) to remove (e.g. 1.1.0 1.2.0)
        #[arg(required = true)]
        versions: Vec<String>,
    },
    /// Change the latest version pointer
    SetLatest {
        /// Version to set as latest (must already be deployed)
        version: String,
    },
}

#[derive(Subcommand)]
enum SystemCommands {
    /// Stop the CJob system for maintenance
    Stop {
        /// Skip confirmation prompt
        #[arg(long)]
        yes: bool,
    },
    /// Start the CJob system after maintenance
    Start {
        /// Number of Submit API replicas (default: 2)
        #[arg(long, default_value_t = cmd::system::DEFAULT_SUBMIT_API_REPLICAS)]
        submit_api_replicas: i32,
    },
    /// Rolling restart a component
    Restart {
        /// Component name (dispatcher, watcher, submit-api)
        component: String,
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
                JobsCommands::List { namespace, status, sort, reverse, output } => {
                    let status_upper = status.map(|s| s.to_uppercase());
                    let wide = match output.as_deref() {
                        Some("wide") => true,
                        Some(other) => {
                            anyhow::bail!("Unknown output format '{}'. Valid values: wide", other);
                        }
                        None => false,
                    };
                    cmd::jobs::list(&conn.client, namespace.as_deref(), status_upper.as_deref(), sort.as_deref(), reverse, wide).await
                }
                JobsCommands::Status { namespace, job_id } => cmd::jobs::status(&conn.client, &namespace, job_id).await,
                JobsCommands::Stalled { sort, reverse } => cmd::jobs::stalled(&conn.client, sort.as_deref(), reverse).await,
                JobsCommands::Remaining { sort, reverse } => cmd::jobs::remaining(&conn.client, sort.as_deref(), reverse).await,
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
                UsageCommands::List { namespace } => {
                    let totals = cmd::usage::ClusterTotals::from_db(&conn.client).await;
                    cmd::usage::list(&conn.client, &totals, namespace.as_deref()).await
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
                        let label = config.user_namespace_label();
                        let user_namespaces =
                            fetch_user_namespaces(&k8s_client, label).await?;
                        cmd::weight::exclusive(&conn.client, ns, &user_namespaces, label).await
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
                ClusterCommands::FlavorUsage => {
                    let k8s_client = k8s::client().await?;
                    cmd::cluster::flavor_usage(&k8s_client).await
                }
                ClusterCommands::ShowQuota => {
                    let k8s_client = k8s::client().await?;
                    cmd::cluster::show_quota(&k8s_client).await
                }
                ClusterCommands::SetQuota { flavor, cpu, memory, gpu, force } => {
                    let conn =
                        db::connect(&config.database, config.system_namespace()).await?;
                    let k8s_client = k8s::client().await?;
                    cmd::cluster::set_quota(
                        &conn.client,
                        &k8s_client,
                        &flavor,
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
        Commands::System { command } => {
            let config = config::Config::load()?;
            let k8s_client = k8s::client().await?;
            match command {
                SystemCommands::Stop { yes } => {
                    let conn = db::connect(&config.database, config.system_namespace()).await?;
                    cmd::system::stop::run(&k8s_client, &conn.client, config.system_namespace(), yes).await
                }
                SystemCommands::Start { submit_api_replicas } => {
                    cmd::system::start::run(&k8s_client, config.system_namespace(), submit_api_replicas).await
                }
                SystemCommands::Restart { component } => {
                    cmd::system::restart::run(&k8s_client, config.system_namespace(), &component).await
                }
                SystemCommands::Status => {
                    cmd::system::status::run(&k8s_client, config.system_namespace()).await
                }
                SystemCommands::Logs { component, tail } => {
                    cmd::system::logs::run(&k8s_client, config.system_namespace(), &component, tail).await
                }
            }
        }
        Commands::Config { command } => {
            let config = config::Config::load()?;
            let k8s_client = k8s::client().await?;
            match command {
                ConfigCommands::Show => {
                    cmd::config::show::run(&k8s_client, config.system_namespace()).await
                }
                ConfigCommands::Set { key, value, from_file, yes } => {
                    cmd::config::set::run(
                        &k8s_client,
                        config.system_namespace(),
                        &key,
                        value.as_deref(),
                        from_file.as_deref(),
                        yes,
                    ).await
                }
                ConfigCommands::Dump => {
                    cmd::config::dump::run(&k8s_client, config.system_namespace()).await
                }
            }
        }
        Commands::Cli { command } => {
            let config = config::Config::load()?;
            match command {
                CliCommands::List => {
                    cmd::cli::list::run(config.system_namespace()).await
                }
                CliCommands::Deploy { binary, version, release } => {
                    cmd::cli::deploy::run(config.system_namespace(), &binary, &version, release).await
                }
                CliCommands::Remove { versions } => {
                    cmd::cli::remove::run(config.system_namespace(), &versions).await
                }
                CliCommands::SetLatest { version } => {
                    cmd::cli::set_latest::run(config.system_namespace(), &version).await
                }
            }
        }
        Commands::User { command } => {
            let k8s_client = k8s::client().await?;
            match command {
                UserCommands::List { enabled, disabled } => {
                    cmd::user::list(&k8s_client, enabled, disabled).await
                }
                UserCommands::Enable { namespace } => {
                    cmd::user::enable(&k8s_client, &namespace).await
                }
                UserCommands::Disable { namespace } => {
                    cmd::user::disable(&k8s_client, &namespace).await
                }
            }
        }
    }
}

async fn fetch_user_namespaces(k8s_client: &kube::Client, label_selector: &str) -> Result<Vec<String>> {
    use k8s_openapi::api::core::v1::Namespace;
    use kube::api::ListParams;
    use kube::Api;

    let ns_api: Api<Namespace> = Api::all(k8s_client.clone());
    let lp = ListParams::default().labels(label_selector);
    let ns_list = ns_api.list(&lp).await?;

    let names: Vec<String> = ns_list
        .items
        .iter()
        .filter_map(|ns| ns.metadata.name.clone())
        .collect();

    Ok(names)
}

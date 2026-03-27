use anyhow::{Context, Result};
use serde::Deserialize;
use std::path::PathBuf;

#[derive(Deserialize)]
pub struct Config {
    pub database: DatabaseConfig,
    pub kubernetes: Option<KubernetesConfig>,
}

#[derive(Deserialize)]
pub struct DatabaseConfig {
    pub host: String,
    pub port: u16,
    pub database: String,
    pub user: String,
    pub password: String,
}

#[derive(Deserialize)]
pub struct KubernetesConfig {
    pub namespace: Option<String>,
}

impl Config {
    pub fn load() -> Result<Self> {
        let path = Self::config_path()?;
        let content = std::fs::read_to_string(&path)
            .with_context(|| format!("Config file not found: {}", path.display()))?;
        let config: Config =
            toml::from_str(&content).with_context(|| "Failed to parse config file")?;
        Ok(config)
    }

    pub fn system_namespace(&self) -> &str {
        self.kubernetes
            .as_ref()
            .and_then(|k| k.namespace.as_deref())
            .unwrap_or("cjob-system")
    }

    fn config_path() -> Result<PathBuf> {
        let config_dir = dirs::config_dir()
            .context("Could not determine config directory")?;
        Ok(config_dir.join("cjobctl").join("config.toml"))
    }
}

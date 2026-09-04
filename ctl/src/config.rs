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
    pub database: String,
    pub user: String,
    pub password: String,
}

#[derive(Deserialize)]
pub struct KubernetesConfig {
    pub namespace: Option<String>,
    pub user_namespace_label: Option<String>,
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

    pub fn user_namespace_label(&self) -> &str {
        self.kubernetes
            .as_ref()
            .and_then(|k| k.user_namespace_label.as_deref())
            .unwrap_or("cjob.io/user-namespace=true")
    }

    /// Returns the path to the admin config file.
    /// Uses $XDG_CONFIG_HOME/cjobctl/config.toml, defaulting to ~/.config/cjobctl/config.toml.
    fn config_path() -> Result<PathBuf> {
        let config_dir = if let Ok(xdg) = std::env::var("XDG_CONFIG_HOME") {
            PathBuf::from(xdg)
        } else {
            let home = std::env::var("HOME").context("HOME environment variable is not set")?;
            PathBuf::from(home).join(".config")
        };
        Ok(config_dir.join("cjobctl").join("config.toml"))
    }
}

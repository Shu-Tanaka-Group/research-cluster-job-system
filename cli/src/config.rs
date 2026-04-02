use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct CjobConfig {
    #[serde(default)]
    pub env: EnvConfig,
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct EnvConfig {
    #[serde(default)]
    pub exclude: Vec<String>,
}

/// Known config key type.
pub enum KeyType {
    List,
    #[allow(dead_code)]
    Scalar,
}

/// Returns the key type for a known table/key combination, or None if unknown.
pub fn lookup_key_type(table: &str, key: &str) -> Option<KeyType> {
    match (table, key) {
        ("env", "exclude") => Some(KeyType::List),
        _ => None,
    }
}

/// Returns the path to the user config file.
/// Uses $XDG_CONFIG_HOME/cjob/config.toml, defaulting to ~/.config/cjob/config.toml.
pub fn config_path() -> Result<PathBuf> {
    let config_dir = if let Ok(xdg) = std::env::var("XDG_CONFIG_HOME") {
        PathBuf::from(xdg)
    } else {
        dirs::config_dir()
            .ok_or_else(|| anyhow::anyhow!("ホームディレクトリを特定できませんでした"))?
    };
    Ok(config_dir.join("cjob").join("config.toml"))
}

/// Loads the config from the default path. Returns default config if file does not exist.
pub fn load() -> Result<CjobConfig> {
    let path = config_path()?;
    if !path.exists() {
        return Ok(CjobConfig::default());
    }
    let content = std::fs::read_to_string(&path)
        .with_context(|| format!("設定ファイルの読み込みに失敗しました: {}", path.display()))?;
    parse_toml(&content)
}

/// Parses a TOML string into CjobConfig.
pub fn parse_toml(content: &str) -> Result<CjobConfig> {
    toml::from_str(content).context("設定ファイルのパースに失敗しました")
}

/// Saves the config to the default path, creating parent directories if needed.
pub fn save(config: &CjobConfig) -> Result<()> {
    let path = config_path()?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("ディレクトリの作成に失敗しました: {}", parent.display()))?;
    }
    let content =
        toml::to_string_pretty(config).context("設定のシリアライズに失敗しました")?;
    std::fs::write(&path, content)
        .with_context(|| format!("設定ファイルの書き込みに失敗しました: {}", path.display()))?;
    Ok(())
}

/// Filters environment variables by removing keys listed in config.env.exclude.
pub fn filter_env(
    mut env: HashMap<String, String>,
    config: &CjobConfig,
) -> HashMap<String, String> {
    for key in &config.env.exclude {
        env.remove(key);
    }
    env
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_config() {
        let config = CjobConfig::default();
        assert!(config.env.exclude.is_empty());
    }

    #[test]
    fn test_roundtrip_toml() {
        let config = CjobConfig {
            env: EnvConfig {
                exclude: vec!["SECRET".into(), "TOKEN".into()],
            },
        };
        let toml_str = toml::to_string_pretty(&config).unwrap();
        let parsed: CjobConfig = toml::from_str(&toml_str).unwrap();
        assert_eq!(parsed.env.exclude, vec!["SECRET", "TOKEN"]);
    }

    #[test]
    fn test_parse_empty_toml() {
        let config = parse_toml("").unwrap();
        assert!(config.env.exclude.is_empty());
    }

    #[test]
    fn test_unknown_keys_ignored() {
        let toml_str = r#"
[env]
exclude = ["A"]
unknown_key = "value"

[unknown_table]
foo = "bar"
"#;
        let config = parse_toml(toml_str).unwrap();
        assert_eq!(config.env.exclude, vec!["A"]);
    }

    #[test]
    fn test_filter_env_removes_excluded() {
        let mut env = HashMap::new();
        env.insert("A".into(), "1".into());
        env.insert("B".into(), "2".into());
        env.insert("C".into(), "3".into());

        let config = CjobConfig {
            env: EnvConfig {
                exclude: vec!["A".into(), "B".into()],
            },
        };

        let filtered = filter_env(env, &config);
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered.get("C").unwrap(), "3");
    }

    #[test]
    fn test_filter_env_empty_exclude() {
        let mut env = HashMap::new();
        env.insert("A".into(), "1".into());
        env.insert("B".into(), "2".into());

        let config = CjobConfig::default();
        let filtered = filter_env(env, &config);
        assert_eq!(filtered.len(), 2);
    }

    #[test]
    fn test_filter_env_missing_key() {
        let mut env = HashMap::new();
        env.insert("A".into(), "1".into());

        let config = CjobConfig {
            env: EnvConfig {
                exclude: vec!["NONEXISTENT".into()],
            },
        };

        let filtered = filter_env(env, &config);
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered.get("A").unwrap(), "1");
    }

    #[test]
    fn test_config_path_with_xdg() {
        // Temporarily set XDG_CONFIG_HOME
        let original = std::env::var("XDG_CONFIG_HOME").ok();
        std::env::set_var("XDG_CONFIG_HOME", "/tmp/test_xdg");
        let path = config_path().unwrap();
        assert_eq!(path, PathBuf::from("/tmp/test_xdg/cjob/config.toml"));
        // Restore
        match original {
            Some(val) => std::env::set_var("XDG_CONFIG_HOME", val),
            None => std::env::remove_var("XDG_CONFIG_HOME"),
        }
    }

    #[test]
    fn test_lookup_key_type_known() {
        assert!(matches!(lookup_key_type("env", "exclude"), Some(KeyType::List)));
    }

    #[test]
    fn test_lookup_key_type_unknown() {
        assert!(lookup_key_type("unknown", "key").is_none());
        assert!(lookup_key_type("env", "unknown").is_none());
    }
}

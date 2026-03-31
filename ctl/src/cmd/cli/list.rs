use anyhow::Result;
use semver::Version;
use std::cmp::Ordering;

use super::{cleanup_pod, create_temp_pod, run_kubectl, PVC_MOUNT_PATH};

/// Parse ls output into a list of (Version, original_string) pairs.
/// Filters out the "latest" file entry. Unparseable entries are returned separately.
fn parse_versions(ls_output: &str) -> (Vec<Version>, Vec<String>) {
    let mut versions = Vec::new();
    let mut unparseable = Vec::new();

    for entry in ls_output.split_whitespace() {
        if entry == "latest" {
            continue;
        }
        match entry.parse::<Version>() {
            Ok(v) => versions.push(v),
            Err(_) => unparseable.push(entry.to_string()),
        }
    }

    (versions, unparseable)
}

/// Sort versions in descending order.
/// Within the same base version (major.minor.patch), pre-release versions
/// appear before the release version (e.g., 1.3.0-beta.1 above 1.3.0).
fn sort_versions(versions: &mut [Version]) {
    versions.sort_by(|a, b| {
        let base_cmp = (b.major, b.minor, b.patch).cmp(&(a.major, a.minor, a.patch));
        if base_cmp != Ordering::Equal {
            return base_cmp;
        }
        // Same base version: pre-release comes first (before release)
        match (a.pre.is_empty(), b.pre.is_empty()) {
            (true, false) => Ordering::Greater,  // a is release, b is pre -> b first
            (false, true) => Ordering::Less,     // a is pre, b is release -> a first
            _ => b.pre.cmp(&a.pre),              // both pre-release: higher pre-release first
        }
    });
}

pub async fn run(namespace: &str) -> Result<()> {
    println!("Fetching CLI versions from PVC...");

    let pod_name = create_temp_pod(namespace, "list").await?;

    let result = list_versions(namespace, &pod_name).await;

    println!("  Cleaning up temporary pod...");
    cleanup_pod(namespace, &pod_name).await;

    result
}

async fn list_versions(namespace: &str, pod_name: &str) -> Result<()> {
    // Get directory listing
    let ls_output = run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "ls", PVC_MOUNT_PATH,
    ])
    .await?;

    // Get current latest version
    let latest = run_kubectl(&[
        "exec", pod_name,
        "--namespace", namespace,
        "--", "cat", &format!("{}/latest", PVC_MOUNT_PATH),
    ])
    .await
    .unwrap_or_default();
    let latest = latest.trim();

    let (mut versions, unparseable) = parse_versions(&ls_output);
    sort_versions(&mut versions);

    // Print header
    println!("{:<19}LATEST", "VERSION");

    // Print sorted versions
    for v in &versions {
        let v_str = v.to_string();
        if v_str == latest {
            println!("{:<19}← latest", v_str);
        } else {
            println!("{}", v_str);
        }
    }

    // Print unparseable entries at the end
    for entry in &unparseable {
        if entry == latest {
            println!("{:<19}← latest", entry);
        } else {
            println!("{}", entry);
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_normal_versions() {
        let (versions, unparseable) = parse_versions("1.0.0  1.1.0  1.2.0  latest");
        assert_eq!(versions.len(), 3);
        assert!(unparseable.is_empty());
        assert_eq!(versions[0], Version::parse("1.0.0").unwrap());
        assert_eq!(versions[1], Version::parse("1.1.0").unwrap());
        assert_eq!(versions[2], Version::parse("1.2.0").unwrap());
    }

    #[test]
    fn parse_filters_latest() {
        let (versions, unparseable) = parse_versions("latest  1.0.0");
        assert_eq!(versions.len(), 1);
        assert!(unparseable.is_empty());
    }

    #[test]
    fn parse_empty_input() {
        let (versions, unparseable) = parse_versions("");
        assert!(versions.is_empty());
        assert!(unparseable.is_empty());
    }

    #[test]
    fn parse_unparseable_entries() {
        let (versions, unparseable) = parse_versions("1.0.0  not-a-version  latest");
        assert_eq!(versions.len(), 1);
        assert_eq!(unparseable, vec!["not-a-version"]);
    }

    #[test]
    fn parse_with_prerelease() {
        let (versions, _) = parse_versions("1.3.0  1.3.0-beta.1  1.2.0");
        assert_eq!(versions.len(), 3);
    }

    #[test]
    fn sort_descending_stable() {
        let mut versions = vec![
            Version::parse("1.1.0").unwrap(),
            Version::parse("1.3.0").unwrap(),
            Version::parse("1.2.0").unwrap(),
        ];
        sort_versions(&mut versions);
        assert_eq!(versions[0], Version::parse("1.3.0").unwrap());
        assert_eq!(versions[1], Version::parse("1.2.0").unwrap());
        assert_eq!(versions[2], Version::parse("1.1.0").unwrap());
    }

    #[test]
    fn sort_prerelease_before_release() {
        let mut versions = vec![
            Version::parse("1.3.0").unwrap(),
            Version::parse("1.3.0-beta.1").unwrap(),
        ];
        sort_versions(&mut versions);
        // Pre-release should appear first (above release)
        assert_eq!(versions[0], Version::parse("1.3.0-beta.1").unwrap());
        assert_eq!(versions[1], Version::parse("1.3.0").unwrap());
    }

    #[test]
    fn sort_prerelease_ordering() {
        let mut versions = vec![
            Version::parse("1.3.0-beta.1").unwrap(),
            Version::parse("1.3.0-beta.2").unwrap(),
            Version::parse("1.3.0").unwrap(),
        ];
        sort_versions(&mut versions);
        assert_eq!(versions[0], Version::parse("1.3.0-beta.2").unwrap());
        assert_eq!(versions[1], Version::parse("1.3.0-beta.1").unwrap());
        assert_eq!(versions[2], Version::parse("1.3.0").unwrap());
    }

    #[test]
    fn sort_full_example() {
        // Matches the design doc example
        let mut versions = vec![
            Version::parse("1.1.0").unwrap(),
            Version::parse("1.3.0").unwrap(),
            Version::parse("1.2.0").unwrap(),
            Version::parse("1.3.0-beta.1").unwrap(),
        ];
        sort_versions(&mut versions);
        assert_eq!(versions[0], Version::parse("1.3.0-beta.1").unwrap());
        assert_eq!(versions[1], Version::parse("1.3.0").unwrap());
        assert_eq!(versions[2], Version::parse("1.2.0").unwrap());
        assert_eq!(versions[3], Version::parse("1.1.0").unwrap());
    }
}

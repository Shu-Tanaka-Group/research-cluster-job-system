use anyhow::{bail, Result};

/// Parse job ID expressions like "1", "1-5", "1,3,5", "1-5,8,10-12"
pub fn parse_job_ids(expr: &str) -> Result<Vec<u32>> {
    let mut ids = Vec::new();

    for part in expr.split(',') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }

        if let Some((start_str, end_str)) = part.split_once('-') {
            let start: u32 = start_str
                .trim()
                .parse()
                .map_err(|_| anyhow::anyhow!("invalid job_id: {}", start_str.trim()))?;
            let end: u32 = end_str
                .trim()
                .parse()
                .map_err(|_| anyhow::anyhow!("invalid job_id: {}", end_str.trim()))?;
            if start > end {
                bail!("invalid range: {}-{}", start, end);
            }
            for id in start..=end {
                ids.push(id);
            }
        } else {
            let id: u32 = part
                .parse()
                .map_err(|_| anyhow::anyhow!("invalid job_id: {}", part))?;
            ids.push(id);
        }
    }

    ids.sort();
    ids.dedup();
    Ok(ids)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_single() {
        assert_eq!(parse_job_ids("5").unwrap(), vec![5]);
    }

    #[test]
    fn test_range() {
        assert_eq!(parse_job_ids("1-5").unwrap(), vec![1, 2, 3, 4, 5]);
    }

    #[test]
    fn test_list() {
        assert_eq!(parse_job_ids("1,3,5").unwrap(), vec![1, 3, 5]);
    }

    #[test]
    fn test_combined() {
        assert_eq!(
            parse_job_ids("1-3,5,8-10").unwrap(),
            vec![1, 2, 3, 5, 8, 9, 10]
        );
    }

    #[test]
    fn test_dedup() {
        assert_eq!(parse_job_ids("1,1,2,2-3").unwrap(), vec![1, 2, 3]);
    }

    #[test]
    fn test_invalid_range() {
        assert!(parse_job_ids("5-3").is_err());
    }

    #[test]
    fn test_invalid_id() {
        assert!(parse_job_ids("abc").is_err());
    }
}

use anyhow::Result;
use tokio_postgres::Client;

pub async fn list(client: &Client) -> Result<()> {
    let rows = client
        .query(
            "SELECT namespace, next_id FROM user_job_counters ORDER BY namespace",
            &[],
        )
        .await?;

    if rows.is_empty() {
        println!("No job counters found.");
        return Ok(());
    }

    println!("{:<20} {}", "NAMESPACE", "NEXT_ID");
    for row in &rows {
        let ns: &str = row.get(0);
        let next_id: i32 = row.get(1);
        println!("{:<20} {}", ns, next_id);
    }
    Ok(())
}

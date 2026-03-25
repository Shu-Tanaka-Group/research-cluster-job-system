use anyhow::{bail, Context, Result};
use reqwest::StatusCode;
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize)]
pub struct JobSubmitRequest {
    pub command: String,
    pub image: String,
    pub cwd: String,
    pub env: std::collections::HashMap<String, String>,
    pub resources: ResourceSpec,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct ResourceSpec {
    pub cpu: String,
    pub memory: String,
    pub gpu: u32,
}

#[derive(Debug, Deserialize)]
pub struct JobSubmitResponse {
    pub job_id: u32,
    pub status: String,
}

#[derive(Debug, Deserialize)]
pub struct JobListResponse {
    pub jobs: Vec<JobSummary>,
    pub total_count: u32,
}

#[derive(Debug, Deserialize)]
pub struct JobSummary {
    pub job_id: u32,
    pub status: String,
    pub command: String,
    pub created_at: String,
    pub finished_at: Option<String>,
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
pub struct JobDetailResponse {
    pub job_id: u32,
    pub status: String,
    pub namespace: String,
    pub command: String,
    pub cwd: String,
    pub k8s_job_name: Option<String>,
    pub log_dir: Option<String>,
    pub created_at: String,
    pub dispatched_at: Option<String>,
    pub finished_at: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct CancelRequest {
    pub job_ids: Vec<u32>,
}

#[derive(Debug, Deserialize)]
pub struct CancelResponse {
    pub cancelled: Vec<u32>,
    pub skipped: Vec<u32>,
    pub not_found: Vec<u32>,
}

#[derive(Debug, Serialize)]
pub struct DeleteRequest {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub job_ids: Option<Vec<u32>>,
}

#[derive(Debug, Deserialize)]
pub struct SkippedItem {
    pub job_id: u32,
    pub reason: String,
}

#[derive(Debug, Deserialize)]
pub struct DeleteResponse {
    pub deleted: Vec<u32>,
    pub skipped: Vec<SkippedItem>,
    pub not_found: Vec<u32>,
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
pub struct ErrorResponse {
    pub detail: Option<String>,
    pub message: Option<String>,
    pub blocking_job_ids: Option<Vec<u32>>,
}

pub struct CjobClient {
    base_url: String,
    token: String,
    http: reqwest::Client,
}

impl CjobClient {
    pub fn new(token: String) -> Result<Self> {
        let base_url = std::env::var("CJOB_API_URL")
            .unwrap_or_else(|_| "http://submit-api.cjob-system.svc.cluster.local:8080".to_string());

        let http = reqwest::Client::builder()
            .danger_accept_invalid_certs(true)
            .build()
            .context("HTTP クライアントの初期化に失敗しました")?;

        Ok(Self {
            base_url,
            token,
            http,
        })
    }

    fn auth_header(&self) -> String {
        format!("Bearer {}", self.token)
    }

    pub async fn submit_job(&self, req: &JobSubmitRequest) -> Result<JobSubmitResponse> {
        let resp = self
            .http
            .post(format!("{}/v1/jobs", self.base_url))
            .header("Authorization", self.auth_header())
            .json(req)
            .send()
            .await
            .context("API への接続に失敗しました")?;

        handle_error_response(&resp.status(), resp).await
    }

    pub async fn list_jobs(
        &self,
        status: Option<&str>,
        limit: Option<u32>,
        order: Option<&str>,
    ) -> Result<JobListResponse> {
        let mut url = format!("{}/v1/jobs", self.base_url);
        let mut params = Vec::new();
        if let Some(s) = status {
            params.push(format!("status={}", s));
        }
        if let Some(l) = limit {
            params.push(format!("limit={}", l));
        }
        if let Some(o) = order {
            params.push(format!("order={}", o));
        }
        if !params.is_empty() {
            url = format!("{}?{}", url, params.join("&"));
        }

        let resp = self
            .http
            .get(&url)
            .header("Authorization", self.auth_header())
            .send()
            .await
            .context("API への接続に失敗しました")?;

        handle_error_response(&resp.status(), resp).await
    }

    pub async fn get_job(&self, job_id: u32) -> Result<JobDetailResponse> {
        let resp = self
            .http
            .get(format!("{}/v1/jobs/{}", self.base_url, job_id))
            .header("Authorization", self.auth_header())
            .send()
            .await
            .context("API への接続に失敗しました")?;

        handle_error_response(&resp.status(), resp).await
    }

    pub async fn cancel_single(&self, job_id: u32) -> Result<serde_json::Value> {
        let resp = self
            .http
            .post(format!("{}/v1/jobs/{}/cancel", self.base_url, job_id))
            .header("Authorization", self.auth_header())
            .send()
            .await
            .context("API への接続に失敗しました")?;

        handle_error_response(&resp.status(), resp).await
    }

    pub async fn cancel_bulk(&self, job_ids: &[u32]) -> Result<CancelResponse> {
        let req = CancelRequest {
            job_ids: job_ids.to_vec(),
        };
        let resp = self
            .http
            .post(format!("{}/v1/jobs/cancel", self.base_url))
            .header("Authorization", self.auth_header())
            .json(&req)
            .send()
            .await
            .context("API への接続に失敗しました")?;

        handle_error_response(&resp.status(), resp).await
    }

    pub async fn delete_jobs(&self, job_ids: Option<Vec<u32>>) -> Result<DeleteResponse> {
        let req = DeleteRequest { job_ids };
        let resp = self
            .http
            .post(format!("{}/v1/jobs/delete", self.base_url))
            .header("Authorization", self.auth_header())
            .json(&req)
            .send()
            .await
            .context("API への接続に失敗しました")?;

        handle_error_response(&resp.status(), resp).await
    }

    pub async fn reset(&self) -> Result<serde_json::Value> {
        let resp = self
            .http
            .post(format!("{}/v1/reset", self.base_url))
            .header("Authorization", self.auth_header())
            .send()
            .await
            .context("API への接続に失敗しました")?;

        handle_error_response(&resp.status(), resp).await
    }
}

async fn handle_error_response<T: serde::de::DeserializeOwned>(
    _status: &StatusCode,
    resp: reqwest::Response,
) -> Result<T> {
    let status_code = resp.status();
    if status_code.is_success() {
        return resp.json::<T>().await.context("レスポンスの解析に失敗しました");
    }

    let body: ErrorResponse = resp
        .json()
        .await
        .unwrap_or(ErrorResponse {
            detail: Some(format!("HTTP {}", status_code)),
            message: None,
            blocking_job_ids: None,
        });

    let msg = body
        .detail
        .or(body.message)
        .unwrap_or_else(|| format!("HTTP {}", status_code));

    match status_code {
        StatusCode::UNAUTHORIZED => bail!("認証エラー: {}", msg),
        StatusCode::NOT_FOUND => bail!("エラー: {}", msg),
        StatusCode::CONFLICT => bail!("{}", msg),
        StatusCode::TOO_MANY_REQUESTS => bail!("{}", msg),
        _ => bail!("エラー ({}): {}", status_code, msg),
    }
}

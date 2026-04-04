# CJob 認証・認可設計書

## 1. 方針の概要

CJob システムの認証・認可は以下の3層で実現する。
Keycloak を追加で使用せず、Kubernetes ネイティブの機能で完結させる。

| 層 | 手段 | 役割 |
|---|---|---|
| L3/L4 | NetworkPolicy | User namespace 以外からの Submit API アクセスを遮断 |
| L7 認証 | ServiceAccount JWT + TokenReview | リクエストが本当にどの namespace から来たかを K8s が保証 |
| L7 認可 | Submit API のチェック | JWT の namespace と操作対象ジョブの namespace を照合 |

### 採用しなかった方針とその理由

#### NetworkPolicy のみによる制御

NetworkPolicy は L3/L4 の疎通制御のみを行う。
アプリケーション層で「どのユーザーか」を識別する手段を持たないため、
リクエストボディの `namespace` フィールドを偽装した他ユーザーへの操作を防ぐことができない。
第1防衛線としては有効だが、単独では認証・認可に不十分である。

#### CLI による隠蔽

CLI はユーザーが動かす User Pod 内で実行されるため、
ユーザーはシェルアクセスにより API エンドポイントや仕様を調査できる。
「ユーザーが仕組みを知らないだろう」という期待に基づく
security through obscurity であり、採用しない。

---

## 2. 前提

- 各ユーザーは独立した namespace（例: `alice`）を持つ
- ユーザー namespace にはラベル `cjob.io/user-namespace=true` とアノテーション `cjob.io/username` を付与する
- User Pod は JupyterHub + KubeSpawner で作成される
- JupyterHub Pod 作成時に Keycloak による認証が完了している
- CLI を実行できるのは JupyterHub の User Pod からのみである
- User Pod から kubectl や K8s API を直接使う運用はない

---

## 3. ServiceAccount の設計

### 3.1 User Pod 用 ServiceAccount

各 namespace に `computing-user` という名前の ServiceAccount を作成する。

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: computing-user
  namespace: alice  # 各ユーザーの namespace（任意の名前を使用可能）
```

JupyterHub KubeSpawner の設定で User Pod にこの ServiceAccount を付与する。

```yaml
# JupyterHub config.yaml
hub:
  config:
    KubeSpawner:
      service_account: computing-user
```

これにより、User Pod の `/var/run/secrets/kubernetes.io/serviceaccount/` に
K8s が署名した JWT と namespace ファイルが自動的に mount される。

### 3.2 Submit API 用 ServiceAccount

Submit API が動く namespace（例: `cjob-system`）に専用の ServiceAccount を作成する。

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: submit-api-sa
  namespace: cjob-system
```

TokenReview API を呼ぶために、および namespace のアノテーション（`cjob.io/username`）を読み取るために ClusterRole と ClusterRoleBinding を付与する。

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: token-reviewer
rules:
  - apiGroups: ["authentication.k8s.io"]
    resources: ["tokenreviews"]
    verbs: ["create"]
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: submit-api-token-reviewer
subjects:
  - kind: ServiceAccount
    name: submit-api-sa
    namespace: cjob-system
roleRef:
  kind: ClusterRole
  name: token-reviewer
  apiGroup: rbac.authorization.k8s.io
```

---

## 4. namespace 作成時の設定

ユーザー namespace の作成手順は [deployment.md §11](deployment.md#11-namespace-作成スクリプト完成版) を参照。認証・認可に関わる要点を以下にまとめる。

- 各 namespace に `computing-user` ServiceAccount を作成し、JupyterHub KubeSpawner で User Pod に付与する
- namespace にラベル `cjob.io/user-namespace=true` を付与することで、NetworkPolicy が Submit API への通信を許可する
- namespace にアノテーション `cjob.io/username` を付与することで、Submit API がユーザー名を解決できる
- namespace 名は任意（例: `user-alice`, `lab-physics`）。識別はラベルとアノテーションで行う

---

## 5. NetworkPolicy の設計

各 User namespace から Submit API への通信のみを許可する。

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-submit-api
  namespace: cjob-system
spec:
  podSelector:
    matchLabels:
      app: submit-api
  ingress:
    - from:
        - namespaceSelector:
            matchLabels:
              cjob.io/user-namespace: "true"
      ports:
        - protocol: TCP
          port: 8080
```

User namespace 作成時にラベルを付与する（§4 参照）。

---

## 6. CLI 側の実装

JWT と namespace は Pod 内の固定パスから取得する。
K8s API への問い合わせは不要である。

※ CLI の実際の実装は Rust（`std::fs`・`reqwest` クレート等）で行う。以下は概念説明のための擬似コードである。

```
JWT_PATH       = "/var/run/secrets/kubernetes.io/serviceaccount/token"
NAMESPACE_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

fn get_token() -> String:
    JWT_PATH のファイルを読んでトリムして返す

fn get_namespace() -> String:
    NAMESPACE_PATH のファイルを読んでトリムして返す

// API リクエスト時
Authorization: Bearer <get_token() の返り値>
```

---

## 7. Submit API 側の実装

### 7.1 JWT 検証（認証）

K8s の TokenReview API を用いて JWT を検証し、namespace を確定する。

```python
from kubernetes import client, config

def verify_token(token: str) -> str:
    """
    JWT を検証し、リクエスト元の namespace を返す。
    検証失敗時は例外を投げる。
    """
    config.load_incluster_config()
    auth_api = client.AuthenticationV1Api()

    review = client.V1TokenReview(
        spec=client.V1TokenReviewSpec(token=token)
    )
    result = auth_api.create_token_review(review)

    if not result.status.authenticated:
        raise PermissionError("Invalid token")

    extra = result.status.user.extra or {}
    ns_list = extra.get(
        "authentication.kubernetes.io/pod-namespace", []
    )
    if not ns_list:
        raise PermissionError("Namespace not found in token")

    return ns_list[0]  # 例: "alice"
```

### 7.2 namespace 照合（認可）

JWT から確定した namespace で DB を検索することで、認可を実現する。
`jobs` テーブルの主キーは `(namespace, job_id)` であるため、JWT の namespace と job_id の組で一意にジョブを特定できる。
該当レコードがなければ、存在しないジョブへのアクセスと他ユーザーのジョブへのアクセスを区別せず 404 を返す（ジョブの存在自体を隠すことで情報漏洩を防ぐ）。

```python
@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: int, token: str = Depends(extract_bearer)):
    namespace = verify_token(token)   # K8s が保証した namespace
    job = db.get_job(namespace, job_id)  # PK (namespace, job_id) で検索

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # キャンセル処理...
```

全エンドポイントで同様に、JWT の namespace を検索条件に含める。

---

## 8. 信頼の根拠

| 要素 | 説明 |
|---|---|
| JWT の発行 | K8s が Pod 作成時に自動発行し、K8s の秘密鍵で署名する |
| JWT の偽造 | ユーザーはトークンを読めるが、秘密鍵を持たないため偽造できない |
| namespace の確定 | Submit API が K8s の TokenReview API に問い合わせるため、クライアントの申告に依存しない |
| ネットワーク制御 | NetworkPolicy により User namespace 以外からの到達を遮断する |

---

## 9. フロー全体図

```
[ユーザーログイン]
  Keycloak で認証 → JupyterHub が User Pod を作成
    └─ spec.serviceAccountName: computing-user
    └─ JWT が /var/run/secrets/... に自動 mount

[cjob コマンド実行]
  CLI が JWT と namespace を固定パスから読み取る
    └─ Authorization: Bearer <JWT> を付与して Submit API へリクエスト

[NetworkPolicy]
  User namespace 以外からのリクエストをネットワーク層で遮断

[Submit API]
  TokenReview で JWT を検証 → namespace を確定（認証）
    └─ job.namespace と JWT の namespace を照合（認可）
    └─ 一致する場合のみ処理を続行
```

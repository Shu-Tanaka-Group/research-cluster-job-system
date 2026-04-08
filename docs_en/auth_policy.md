> *This document was auto-translated from the [Japanese original](../docs/auth_policy.md) by Claude and may contain errors. Refer to the original for the authoritative content.*

# CJob Authentication and Authorization Design

## 1. Policy Overview

The CJob system implements authentication and authorization in the following 3 layers.
No additional Keycloak is used; everything is handled using Kubernetes-native features.

| Layer | Mechanism | Role |
|---|---|---|
| L3/L4 | NetworkPolicy | Block Submit API access from outside User namespaces |
| L7 Authentication | ServiceAccount JWT + TokenReview | K8s guarantees which namespace the request truly originates from |
| L7 Authorization | Submit API checks | Match the JWT namespace against the target job's namespace |

### Approaches Not Adopted and Reasons

#### Control via NetworkPolicy Only

NetworkPolicy only handles L3/L4 connectivity control.
It has no means to identify "which user" at the application layer,
so it cannot prevent operations on other users' resources by spoofing the `namespace` field in the request body.
While effective as a first line of defense, it is insufficient for authentication and authorization on its own.

#### Concealment via CLI

Since the CLI runs inside a User Pod operated by the user,
the user can investigate API endpoints and specifications via shell access.
This is security through obscurity — relying on the expectation that "the user won't know how it works" — and is therefore not adopted.

---

## 2. Prerequisites

- Each user has an independent namespace (e.g., `alice`)
- User namespaces are assigned the label `cjob.io/user-namespace=true` and the annotation `cjob.io/username`
- User Pods are created by JupyterHub + KubeSpawner
- Keycloak authentication is completed when the JupyterHub Pod is created
- The CLI can only be executed from the JupyterHub User Pod
- Users do not directly use kubectl or the K8s API from User Pods

---

## 3. ServiceAccount Design

### 3.1 ServiceAccount for User Pods

A ServiceAccount named `computing-user` is created in each namespace.

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: computing-user
  namespace: alice  # Each user's namespace (any name can be used)
```

This ServiceAccount is assigned to User Pods via the JupyterHub KubeSpawner configuration.

```yaml
# JupyterHub config.yaml
hub:
  config:
    KubeSpawner:
      service_account: computing-user
```

This causes K8s to automatically mount a K8s-signed JWT and a namespace file at
`/var/run/secrets/kubernetes.io/serviceaccount/` in the User Pod.

### 3.2 ServiceAccount for Submit API

A dedicated ServiceAccount is created in the namespace where the Submit API runs (e.g., `cjob-system`).

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: submit-api-sa
  namespace: cjob-system
```

A ClusterRole and ClusterRoleBinding are assigned to allow calling the TokenReview API and reading namespace annotations (`cjob.io/username`).

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

## 4. Configuration at Namespace Creation

Refer to [deployment.md §11](deployment.md#11-namespace-creation-script-complete-version) for the procedure to create user namespaces. Key points related to authentication and authorization are summarized below.

- Create the `computing-user` ServiceAccount in each namespace and assign it to User Pods via JupyterHub KubeSpawner
- Assigning the label `cjob.io/user-namespace=true` to the namespace allows NetworkPolicy to permit communication to the Submit API
- Assigning the annotation `cjob.io/username` to the namespace allows the Submit API to resolve the username
- Namespace names are arbitrary (e.g., `user-alice`, `lab-physics`). Identification is done via labels and annotations

---

## 5. NetworkPolicy Design

Only communication from each User namespace to the Submit API is permitted.

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

Labels are assigned at User namespace creation time (see §4).

---

## 6. CLI-Side Implementation

The JWT and namespace are obtained from fixed paths inside the Pod.
No queries to the K8s API are required.

Note: The actual CLI implementation is in Rust (using `std::fs`, `reqwest` crates, etc.). The following is pseudocode for conceptual explanation.

```
JWT_PATH       = "/var/run/secrets/kubernetes.io/serviceaccount/token"
NAMESPACE_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

fn get_token() -> String:
    Read the file at JWT_PATH, trim, and return

fn get_namespace() -> String:
    Read the file at NAMESPACE_PATH, trim, and return

// When making API requests
Authorization: Bearer <return value of get_token()>
```

---

## 7. Submit API-Side Implementation

### 7.1 JWT Verification (Authentication)

The K8s TokenReview API is used to verify the JWT and determine the namespace.

```python
from kubernetes import client, config

def verify_token(token: str) -> str:
    """
    Verify the JWT and return the namespace of the requester.
    Raises an exception on verification failure.
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

    return ns_list[0]  # e.g., "alice"
```

### 7.2 Namespace Matching (Authorization)

Authorization is achieved by querying the DB using the namespace determined from the JWT.
Since the primary key of the `jobs` table is `(namespace, job_id)`, a job can be uniquely identified by the combination of JWT namespace and job_id.
If no record is found, a 404 is returned without distinguishing between access to a non-existent job and access to another user's job (hiding the existence of the job prevents information leakage).

```python
@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: int, token: str = Depends(extract_bearer)):
    namespace = verify_token(token)   # Namespace guaranteed by K8s
    job = db.get_job(namespace, job_id)  # Query by PK (namespace, job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    # Cancellation processing...
```

Similarly, the JWT namespace is included as a search condition in all endpoints.

---

## 8. Communication Channel Between CLI and API

### 8.1 Current Policy

Communication between the CLI and the Submit API uses in-cluster HTTP (plaintext). Since the in-cluster network is protected by NetworkPolicy (§5) and there is no exposure outside the cluster, TLS is not introduced at this time.

### 8.2 TLS Certificate Verification (`CJOB_INSECURE_SKIP_VERIFY`)

The CLI controls TLS certificate verification via the environment variable `CJOB_INSECURE_SKIP_VERIFY`. The default is to skip verification (equivalent to `true`). This setting has no practical effect for HTTP communication.

| Value | Behavior |
|---|---|
| Not set | Skip verification (default) |
| `0` or `false` | Perform verification |
| Other values | Skip verification |

> **Note**: If `CJOB_API_URL` is changed to an HTTPS endpoint, first change the default value of `skip_tls_verify()` in the CLI to `false` (verification enabled). Leaving the current default (skip verification) in place means TLS certificates will not be verified, making the system vulnerable to MITM attacks.

---

## 9. Basis of Trust

| Element | Description |
|---|---|
| JWT Issuance | K8s automatically issues the JWT at Pod creation and signs it with K8s's private key |
| JWT Forgery | Users can read the token but cannot forge it since they do not have the private key |
| Namespace Determination | The Submit API queries the K8s TokenReview API, so it does not rely on client-provided claims |
| Network Control | NetworkPolicy blocks access from outside User namespaces |

---

## 10. Overall Flow Diagram

```
[User Login]
  Authenticate with Keycloak → JupyterHub creates User Pod
    └─ spec.serviceAccountName: computing-user
    └─ JWT is automatically mounted at /var/run/secrets/...

[cjob Command Execution]
  CLI reads JWT and namespace from fixed paths
    └─ Sends request to Submit API with Authorization: Bearer <JWT>

[NetworkPolicy]
  Blocks requests from outside User namespaces at the network layer

[Submit API]
  Verifies JWT via TokenReview → determines namespace (authentication)
    └─ Matches job.namespace against JWT namespace (authorization)
    └─ Proceeds only if they match
```

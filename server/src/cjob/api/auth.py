import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from kubernetes import client as k8s_client

logger = logging.getLogger(__name__)

USERNAME_ANNOTATION = "cjob.io/username"


@dataclass
class UserInfo:
    namespace: str
    username: str


def extract_bearer(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return auth_header[7:]


def verify_token(token: str = Depends(extract_bearer)) -> str:
    """Verify ServiceAccount JWT via K8s TokenReview API.

    Returns the namespace from the authenticated token.
    """
    auth_api = k8s_client.AuthenticationV1Api()

    review = k8s_client.V1TokenReview(
        spec=k8s_client.V1TokenReviewSpec(token=token)
    )

    try:
        result = auth_api.create_token_review(review)
    except Exception:
        logger.exception("TokenReview API call failed")
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not result.status.authenticated:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Extract namespace from the service account info
    # username format: system:serviceaccount:<namespace>:<sa-name>
    username = result.status.user.username or ""
    parts = username.split(":")
    if len(parts) != 4 or parts[0] != "system" or parts[1] != "serviceaccount":
        raise HTTPException(status_code=401, detail="Unauthorized")

    return parts[2]


def get_namespace(namespace: str = Depends(verify_token)) -> str:
    return namespace


def get_user_info(token: str = Depends(extract_bearer)) -> UserInfo:
    """Verify token and resolve username from namespace annotation.

    Returns UserInfo with namespace and username.
    """
    namespace = verify_token(token)

    core_v1 = k8s_client.CoreV1Api()
    try:
        ns_obj = core_v1.read_namespace(name=namespace)
    except Exception:
        logger.exception("Failed to read namespace %s", namespace)
        raise HTTPException(status_code=500, detail="Internal server error")

    annotations = ns_obj.metadata.annotations or {}
    username = annotations.get(USERNAME_ANNOTATION)
    if not username:
        raise HTTPException(
            status_code=403,
            detail="Namespace is not configured as a CJob user namespace",
        )

    return UserInfo(namespace=namespace, username=username)

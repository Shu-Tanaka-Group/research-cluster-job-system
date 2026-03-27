from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from cjob.api.auth import extract_bearer


def _make_request(auth_header=None):
    """Build a mock FastAPI Request with the given Authorization header."""
    request = MagicMock()
    headers = {}
    if auth_header is not None:
        headers["Authorization"] = auth_header
    request.headers.get = lambda key, default="": headers.get(key, default)
    return request


class TestExtractBearer:
    def test_valid_token(self):
        request = _make_request("Bearer my-jwt-token")
        assert extract_bearer(request) == "my-jwt-token"

    def test_valid_token_with_dots(self):
        token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
        request = _make_request(f"Bearer {token}")
        assert extract_bearer(request) == token

    def test_missing_header(self):
        request = _make_request()
        with pytest.raises(HTTPException) as exc_info:
            extract_bearer(request)
        assert exc_info.value.status_code == 401

    def test_empty_header(self):
        request = _make_request("")
        with pytest.raises(HTTPException) as exc_info:
            extract_bearer(request)
        assert exc_info.value.status_code == 401

    def test_no_bearer_prefix(self):
        request = _make_request("Basic dXNlcjpwYXNz")
        with pytest.raises(HTTPException) as exc_info:
            extract_bearer(request)
        assert exc_info.value.status_code == 401

    def test_bearer_lowercase_rejected(self):
        request = _make_request("bearer my-token")
        with pytest.raises(HTTPException) as exc_info:
            extract_bearer(request)
        assert exc_info.value.status_code == 401

    def test_bearer_only_no_token(self):
        request = _make_request("Bearer ")
        # "Bearer " with trailing space returns empty string (valid extraction)
        assert extract_bearer(request) == ""

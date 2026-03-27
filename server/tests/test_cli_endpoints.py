from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from cjob.api.app import create_app
from cjob.config import Settings


@contextmanager
def _make_client(tmp_path):
    """Create a TestClient with CLI_BINARY_DIR pointed to tmp_path."""
    settings = Settings(
        POSTGRES_PASSWORD="test",
        CLI_BINARY_DIR=str(tmp_path),
    )
    with patch("cjob.api.routes.get_settings", return_value=settings):
        app = create_app()
        yield TestClient(app)


def _setup_pvc(tmp_path, version="1.2.0", binary_content=b"\x7fELFfakebin"):
    """Create PVC directory structure in tmp_path."""
    (tmp_path / "latest").write_text(f"{version}\n")
    version_dir = tmp_path / version
    version_dir.mkdir()
    (version_dir / "cjob").write_bytes(binary_content)


class TestCliVersion:
    def test_returns_version(self, tmp_path):
        _setup_pvc(tmp_path)
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/version")
            assert resp.status_code == 200
            assert resp.json() == {"version": "1.2.0"}

    def test_no_latest_file_returns_404(self, tmp_path):
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/version")
            assert resp.status_code == 404

    def test_no_auth_required(self, tmp_path):
        _setup_pvc(tmp_path)
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/version")
            assert resp.status_code == 200


class TestCliDownload:
    def test_returns_binary(self, tmp_path):
        binary_content = b"\x7fELFtestbinary"
        _setup_pvc(tmp_path, binary_content=binary_content)
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/download")
            assert resp.status_code == 200
            assert resp.content == binary_content
            assert "application/octet-stream" in resp.headers["content-type"]

    def test_no_latest_file_returns_404(self, tmp_path):
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/download")
            assert resp.status_code == 404

    def test_latest_exists_but_no_binary_returns_404(self, tmp_path):
        (tmp_path / "latest").write_text("1.2.0\n")
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/download")
            assert resp.status_code == 404

    def test_no_auth_required(self, tmp_path):
        _setup_pvc(tmp_path)
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/download")
            assert resp.status_code == 200

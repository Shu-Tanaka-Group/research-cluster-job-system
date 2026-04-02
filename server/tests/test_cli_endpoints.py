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


def _setup_pvc(
    tmp_path,
    versions=None,
    latest_version="1.2.0",
    binary_content=b"\x7fELFfakebin",
):
    """Create PVC directory structure in tmp_path."""
    if versions is None:
        versions = [latest_version]
    (tmp_path / "latest").write_text(f"{latest_version}\n")
    for v in versions:
        version_dir = tmp_path / v
        version_dir.mkdir(exist_ok=True)
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


class TestCliVersions:
    def test_returns_all_versions_sorted(self, tmp_path):
        versions = ["1.0.0", "1.2.0", "1.1.0", "1.3.0-beta.1"]
        _setup_pvc(tmp_path, versions=versions, latest_version="1.2.0")
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/versions")
            assert resp.status_code == 200
            data = resp.json()
            assert data["versions"] == [
                "1.3.0-beta.1",
                "1.2.0",
                "1.1.0",
                "1.0.0",
            ]
            assert data["latest"] == "1.2.0"

    def test_no_latest_file_returns_404(self, tmp_path):
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/versions")
            assert resp.status_code == 404

    def test_no_auth_required(self, tmp_path):
        versions = ["1.2.0"]
        _setup_pvc(tmp_path, versions=versions)
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/versions")
            assert resp.status_code == 200

    def test_excludes_latest_file(self, tmp_path):
        _setup_pvc(tmp_path, versions=["1.2.0"])
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/versions")
            data = resp.json()
            assert "latest" not in data["versions"]

    def test_only_latest_file_returns_empty_versions(self, tmp_path):
        (tmp_path / "latest").write_text("1.2.0\n")
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/versions")
            assert resp.status_code == 200
            data = resp.json()
            assert data["versions"] == []
            assert data["latest"] == "1.2.0"

    def test_skips_invalid_directory_names(self, tmp_path):
        _setup_pvc(tmp_path, versions=["1.2.0"])
        (tmp_path / "not-a-version").mkdir()
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/versions")
            data = resp.json()
            assert data["versions"] == ["1.2.0"]


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

    def test_download_specific_version(self, tmp_path):
        binary_v1 = b"\x7fELFv1"
        binary_v2 = b"\x7fELFv2"
        _setup_pvc(tmp_path, versions=["1.1.0"], latest_version="1.2.0")
        (tmp_path / "1.1.0" / "cjob").write_bytes(binary_v1)
        (tmp_path / "1.2.0").mkdir(exist_ok=True)
        (tmp_path / "1.2.0" / "cjob").write_bytes(binary_v2)
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/download", params={"version": "1.1.0"})
            assert resp.status_code == 200
            assert resp.content == binary_v1

    def test_download_nonexistent_version_returns_404(self, tmp_path):
        _setup_pvc(tmp_path)
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/download", params={"version": "9.9.9"})
            assert resp.status_code == 404

    def test_download_invalid_version_format_returns_400(self, tmp_path):
        _setup_pvc(tmp_path)
        with _make_client(tmp_path) as client:
            resp = client.get(
                "/v1/cli/download", params={"version": "../../etc/passwd"}
            )
            assert resp.status_code == 400

    def test_download_without_version_uses_latest(self, tmp_path):
        binary_content = b"\x7fELFlatest"
        _setup_pvc(tmp_path, binary_content=binary_content)
        with _make_client(tmp_path) as client:
            resp = client.get("/v1/cli/download")
            assert resp.status_code == 200
            assert resp.content == binary_content

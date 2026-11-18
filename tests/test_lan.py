"""HTTPS on the LAN (scripts/serve_lan.py) and the CA download a phone installs."""

import importlib.util
import shutil
import ssl
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.core.config import ROOT, settings
from app.main import app

_spec = importlib.util.spec_from_file_location("serve_lan", ROOT / "scripts" / "serve_lan.py")
serve_lan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(serve_lan)

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")


def _verify(certs, crt, ip):
    return subprocess.run(
        ["openssl", "verify", "-CAfile", str(certs / "ca.crt"), "-purpose", "sslserver", "-verify_ip", ip, str(crt)],
        capture_output=True, text=True,
    )


def test_ca_download_404_without_ca(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "certs_dir", tmp_path)
    with TestClient(app) as client:
        assert client.get("/ca.crt").status_code == 404


@needs_openssl
def test_server_cert_verifies_for_the_lan_ip(tmp_path):
    crt, _ = serve_lan.issue_server_cert(tmp_path, ["192.168.1.20", "127.0.0.1"])
    out = _verify(tmp_path, crt, "192.168.1.20")
    assert out.returncode == 0, out.stderr
    assert _verify(tmp_path, crt, "192.168.1.21").returncode != 0  # only the IPs asked for

    # A new IP gets a new server cert from the same CA, so the phone keeps trusting it.
    ca_before = (tmp_path / "ca.crt").read_bytes()
    crt, _ = serve_lan.issue_server_cert(tmp_path, ["10.0.0.5"])
    assert (tmp_path / "ca.crt").read_bytes() == ca_before
    assert _verify(tmp_path, crt, "10.0.0.5").returncode == 0
    for key in ("ca.key", "server.key"):
        assert (tmp_path / key).stat().st_mode & 0o077 == 0  # readable by this user only


@needs_openssl
def test_ca_download_is_der_and_never_the_key(tmp_path, monkeypatch):
    serve_lan.ensure_ca(tmp_path)
    monkeypatch.setattr(settings, "certs_dir", tmp_path)
    with TestClient(app) as client:
        res = client.get("/ca.crt")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/x-x509-ca-cert"
    assert res.content == ssl.PEM_cert_to_DER_cert((tmp_path / "ca.crt").read_text())
    assert b"PRIVATE" not in res.content

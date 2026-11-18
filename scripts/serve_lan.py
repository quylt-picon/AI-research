"""Serve the demo over HTTPS on the LAN, so a phone can use the microphone.

Browsers only allow the microphone on https:// or on localhost: opening
http://192.168.x.x:8100 on a phone shows the page, but the call can't start.
This script:

  1. makes a local CA once (certs/ca.crt + certs/ca.key; the key never leaves
     this machine),
  2. issues a server certificate for this machine's current LAN IP on every
     start (a new IP just works, and the CA the phone trusts stays the same),
  3. starts uvicorn on 0.0.0.0 with that certificate.

    uv run python scripts/serve_lan.py                  # port 8100, auto-reload
    uv run python scripts/serve_lan.py --ip 192.168.1.20 --port 8443

On the phone, open https://<ip>:<port>/ca.crt once and trust it (README: "Dùng trên điện thoại").
Without that, Android Chrome works after accepting the warning, but iPhone
Safari refuses the WebSocket and the call can't connect.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402

CA_DAYS = 825  # iOS rejects longer-lived TLS certificates from user-installed CAs
SERVER_DAYS = 365

CA_CONF = """\
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3_ca
[dn]
CN = Worker Report Live dev CA
O = worker-report-live (local demo only)
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
"""

SERVER_CONF = """\
[req]
distinguished_name = dn
prompt = no
[dn]
CN = {cn}
"""

# What iOS requires of a TLS server certificate: names in SAN, serverAuth EKU,
# RSA >= 2048, SHA-256, validity <= 825 days.
SERVER_EXT = """\
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = {san}
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid, issuer
"""


def lan_ip() -> str:
    """The address other devices on this network reach us at (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def _openssl(*args: str) -> None:
    exe = shutil.which("openssl")
    if exe is None:
        sys.exit("Không tìm thấy openssl (macOS: brew install openssl).")
    proc = subprocess.run([exe, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"openssl {args[0]} lỗi:\n{proc.stderr}")


def ensure_ca(certs: Path) -> tuple[Path, Path]:
    """The CA the phone trusts. Made once: re-making it means re-installing it on every phone."""
    crt, key = certs / "ca.crt", certs / "ca.key"
    if crt.is_file() and key.is_file():
        return crt, key
    certs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "ca.cnf"
        conf.write_text(CA_CONF)
        _openssl("req", "-x509", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256",
                 "-days", str(CA_DAYS), "-config", str(conf), "-keyout", str(key), "-out", str(crt))
    key.chmod(0o600)
    return crt, key


def issue_server_cert(certs: Path, ips: list[str]) -> tuple[Path, Path]:
    """A fresh certificate for these IPs (+ localhost), signed by the local CA."""
    ca_crt, ca_key = ensure_ca(certs)
    crt, key = certs / "server.crt", certs / "server.key"
    san = ", ".join([*(f"IP:{ip}" for ip in dict.fromkeys(ips)), "DNS:localhost"])
    with tempfile.TemporaryDirectory() as tmp:
        conf, ext, csr = Path(tmp) / "server.cnf", Path(tmp) / "server.ext", Path(tmp) / "server.csr"
        conf.write_text(SERVER_CONF.format(cn=ips[0]))
        ext.write_text(SERVER_EXT.format(san=san))
        _openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-config", str(conf),
                 "-keyout", str(key), "-out", str(csr))
        _openssl("x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
                 "-set_serial", str(secrets.randbits(63)), "-days", str(SERVER_DAYS), "-sha256",
                 "-extfile", str(ext), "-out", str(crt))
    key.chmod(0o600)
    return crt, key


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ip", help="IP điện thoại sẽ truy cập (mặc định: tự dò IP LAN của máy này)")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--no-reload", action="store_true", help="tắt auto-reload khi sửa code")
    args = parser.parse_args()

    ip = args.ip or lan_ip()
    ipaddress.ip_address(ip)  # fail early on a typo
    crt, key = issue_server_cert(settings.certs_dir, [ip, "127.0.0.1"])

    base = f"https://{ip}:{args.port}"
    print(f"""
  Máy tính:      https://localhost:{args.port}
  Điện thoại:    {base}
  Lần đầu trên điện thoại, cài chứng chỉ:  {base}/ca.crt
                 (iPhone: Cài đặt > Cài đặt chung > VPN & Quản lý thiết bị > cài hồ sơ,
                  rồi Cài đặt chung > Giới thiệu > Cài đặt tin cậy chứng nhận > bật)
  Điện thoại phải dùng cùng Wi-Fi. Nếu macOS hỏi có cho Python nhận kết nối không, chọn Allow.
  Ai cùng mạng cũng mở được trang này (không có đăng nhập), nên đừng chạy ở Wi-Fi công cộng.
""", flush=True)

    import uvicorn

    uvicorn.run(
        "app.main:app", host="0.0.0.0", port=args.port, app_dir=str(ROOT),
        reload=not args.no_reload, reload_dirs=[str(ROOT / "app")],
        ssl_certfile=str(crt), ssl_keyfile=str(key),
    )


if __name__ == "__main__":
    os.chdir(ROOT)
    main()

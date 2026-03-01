#!/usr/bin/env python3
"""
Tests for subscription parsing robustness in scripts/html2xray.py
"""

import base64
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "html2xray.py"


def _vmess_link() -> str:
    vmess_obj = {
        "v": "2",
        "ps": "n1",
        "add": "example.com",
        "port": "443",
        "id": "11111111-1111-1111-1111-111111111111",
        "aid": "0",
        "net": "ws",
        "type": "none",
        "host": "example.com",
        "path": "/",
        "tls": "tls",
    }
    payload = json.dumps(vmess_obj, ensure_ascii=False).encode("utf-8")
    return "vmess://" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _vless_link() -> str:
    return (
        "vless://11111111-1111-1111-1111-111111111111@example.com:443"
        "?encryption=none&security=tls&type=ws&host=example.com&path=%2F#node"
    )


def _run_html2xray(input_text: str, tmp_path: Path) -> dict:
    src = tmp_path / "subscription.txt"
    out = tmp_path / "generated.json"
    src.write_text(input_text, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(src), str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"html2xray failed (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    return json.loads(out.read_text(encoding="utf-8"))


def test_parses_markdown_wrapped_links(tmp_path):
    vmess = _vmess_link()
    vless = _vless_link()
    text = f"({vless}), and [{vmess}]"

    cfg = _run_html2xray(text, tmp_path)

    proxy_outbounds = [o for o in cfg["outbounds"] if o.get("tag", "").startswith("node")]
    assert len(proxy_outbounds) == 2


def test_parses_html_with_amp_escaping(tmp_path):
    vmess = _vmess_link()
    vless = _vless_link().replace("&security=tls", "&amp;security=tls")
    text = f'<a href="{vless}">v</a><div>{vmess}</div>'

    cfg = _run_html2xray(text, tmp_path)

    proxy_outbounds = [o for o in cfg["outbounds"] if o.get("tag", "").startswith("node")]
    assert len(proxy_outbounds) == 2


def test_parses_base64_blob_subscription(tmp_path):
    vmess = _vmess_link()
    vless = _vless_link()
    raw = f"{vmess}\n{vless}\n".encode("utf-8")
    blob = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    cfg = _run_html2xray(blob, tmp_path)

    proxy_outbounds = [o for o in cfg["outbounds"] if o.get("tag", "").startswith("node")]
    assert len(proxy_outbounds) == 2


import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

MAC_RE = re.compile(r"^[0-9a-f]{12}$")


def canonical_mac(raw: str) -> str:
    stripped = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
    if not MAC_RE.match(stripped):
        raise ValueError(f"invalid MAC: {raw!r}")
    return stripped


def validate_ipxe_url(url: str) -> str:
    p = urlparse(url)
    if p.scheme != "http":
        raise ValueError("ipxe_url must be http:// (iPXE TLS ciphers too old for OCP ingress)")
    if not p.netloc:
        raise ValueError("ipxe_url missing host")
    if any(c in url for c in "\r\n"):
        raise ValueError("ipxe_url contains control characters")
    return url


def mac_path(base_dir: Path, mac: str) -> Path:
    p = (base_dir / f"{mac}.ipxe").resolve()
    if p.parent != base_dir.resolve():
        raise ValueError("path escapes pxe-map dir")
    return p


def write_mapping(base_dir: Path, mac: str, ipxe_url: str) -> None:
    mac = canonical_mac(mac)
    ipxe_url = validate_ipxe_url(ipxe_url)
    content = f"#!ipxe\nchain {ipxe_url}\n"
    path = mac_path(base_dir, mac)
    tmp = path.with_suffix(".ipxe.tmp")
    tmp.write_text(content)
    os.replace(tmp, path)                                      # atomic rename
    try:
        subprocess.run(["restorecon", str(path)], check=False)  # httpd_sys_content_t
    except FileNotFoundError:
        pass  # restorecon not present (e.g. non-SELinux dev/test host)


def read_mapping(base_dir: Path, mac: str) -> str:
    mac = canonical_mac(mac)
    path = mac_path(base_dir, mac)
    content = path.read_text()
    return content.split("chain ", 1)[1].rstrip("\n")


def list_mappings(base_dir: Path) -> list[tuple[str, str]]:
    results = []
    for entry in base_dir.glob("*.ipxe"):
        mac = entry.name[: -len(".ipxe")]
        if MAC_RE.match(mac):
            results.append((mac, read_mapping(base_dir, mac)))
    return sorted(results)


def delete_mapping(base_dir: Path, mac: str) -> None:
    mac = canonical_mac(mac)
    path = mac_path(base_dir, mac)
    path.unlink(missing_ok=True)

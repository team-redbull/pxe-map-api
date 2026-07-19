import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

MAC_RE = re.compile(r"^[0-9a-f]{12}$")


class MappingFormatError(Exception):
    """An on-disk .ipxe file doesn't match the format write_mapping produces."""


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
    base_dir = base_dir.resolve()
    p = (base_dir / f"{mac}.ipxe").resolve()
    if not p.is_relative_to(base_dir) or p.parent != base_dir:
        raise ValueError("path escapes pxe-map dir")
    return p


def _write_tmp(path: Path, content: str) -> Path:
    # Unique name (not a fixed <mac>.ipxe.tmp) so concurrent writers to the
    # same MAC never share a temp file.
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.stem}.", suffix=".ipxe.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
    except BaseException:
        os.unlink(tmp_name)
        raise
    return Path(tmp_name)


def _restorecon(path: Path) -> None:
    try:
        subprocess.run(["restorecon", str(path)], check=False)  # httpd_sys_content_t
    except FileNotFoundError:
        pass  # restorecon not present (e.g. non-SELinux dev/test host)


def write_mapping(base_dir: Path, mac: str, ipxe_url: str) -> None:
    """Atomic upsert: create or overwrite."""
    mac = canonical_mac(mac)
    ipxe_url = validate_ipxe_url(ipxe_url)
    content = f"#!ipxe\nchain {ipxe_url}\n"
    path = mac_path(base_dir, mac)
    tmp = _write_tmp(path, content)
    os.replace(tmp, path)  # atomic rename
    _restorecon(path)


def write_mapping_exclusive(base_dir: Path, mac: str, ipxe_url: str) -> None:
    """Atomic strict create: raises FileExistsError if the mapping already
    exists, instead of the caller doing a separate check-then-write (which
    races under concurrent creates for the same MAC)."""
    mac = canonical_mac(mac)
    ipxe_url = validate_ipxe_url(ipxe_url)
    content = f"#!ipxe\nchain {ipxe_url}\n"
    path = mac_path(base_dir, mac)
    tmp = _write_tmp(path, content)
    try:
        os.link(tmp, path)  # atomic: fails with FileExistsError if path exists
    finally:
        os.unlink(tmp)
    _restorecon(path)


def read_mapping(base_dir: Path, mac: str) -> str:
    mac = canonical_mac(mac)
    path = mac_path(base_dir, mac)
    content = path.read_text()
    try:
        return content.split("chain ", 1)[1].rstrip("\n")
    except IndexError:
        raise MappingFormatError(f"{path} does not contain a 'chain' line") from None


def list_mappings(base_dir: Path) -> list[tuple[str, str]]:
    results = []
    for entry in base_dir.glob("*.ipxe"):
        mac = entry.name[: -len(".ipxe")]
        if MAC_RE.match(mac):
            try:
                results.append((mac, read_mapping(base_dir, mac)))
            except MappingFormatError:
                continue  # skip a corrupt file rather than failing the whole listing
    return sorted(results)


def delete_mapping(base_dir: Path, mac: str) -> None:
    mac = canonical_mac(mac)
    path = mac_path(base_dir, mac)
    path.unlink(missing_ok=True)

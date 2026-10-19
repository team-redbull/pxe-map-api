import fcntl
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

MAC_RE = re.compile(r"^[0-9a-f]{12}$")

# A Kubernetes object name (RFC 1123 subdomain): what an MCE cluster name and
# an InfraEnv name both are, and the only shape either may take as a path
# segment under the pxe-map dir. Like canonical_mac, a name that doesn't match
# is rejected, never lowercased or stripped into one that does.
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
NAME_MAX_LEN = 253

# The relative chain target a MAC file holds when it points at an InfraEnv
# boot entry instead of a URL: `<mce_cluster>/<infra_env>.ipxe`. iPXE resolves
# a relative URI against the script it is running, so this lands on the same
# nginx `/pxe-map/` tree the MAC file itself was served from.
_INFRA_ENV_TARGET_RE = re.compile(r"^(?P<mce_cluster>[^/]+)/(?P<infra_env>[^/]+)\.ipxe$")

_IMAGE_CREATED_AT_PREFIX = "# image-created-at: "


class MappingFormatError(Exception):
    """An on-disk .ipxe file doesn't match the format write_mapping produces."""


class ForeignMappingError(Exception):
    """A conditional delete found the MAC chaining to another MCE's boot entry."""


class StaleInfraEnvBootError(Exception):
    """An InfraEnv boot entry already names a newer discovery image than the
    one a write offered. Not FileExistsError: overwriting is normal here, only
    going backwards in time is refused."""


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


def _is_name(raw: str) -> bool:
    return len(raw) <= NAME_MAX_LEN and NAME_RE.match(raw) is not None


def validate_name(raw: str, what: str) -> str:
    if not _is_name(raw):
        raise ValueError(f"invalid {what}: {raw!r} (must be a lowercase Kubernetes object name)")
    return raw


def infra_env_target(mce_cluster: str, infra_env: str) -> str:
    """The chain target of a MAC file pointing at an InfraEnv boot entry."""
    mce_cluster = validate_name(mce_cluster, "mce_cluster")
    infra_env = validate_name(infra_env, "infra_env")
    return f"{mce_cluster}/{infra_env}.ipxe"


def parse_infra_env_target(target: str) -> tuple[str, str] | None:
    """(mce_cluster, infra_env) if `target` is an InfraEnv boot entry, else None."""
    m = _INFRA_ENV_TARGET_RE.match(target)
    if m is None or not (_is_name(m["mce_cluster"]) and _is_name(m["infra_env"])):
        return None
    return m["mce_cluster"], m["infra_env"]


def validate_chain_target(target: str) -> str:
    """What a MAC file may chain to: an http:// URL, or an InfraEnv boot entry."""
    if parse_infra_env_target(target) is not None:
        return target
    return validate_ipxe_url(target)


def mac_path(base_dir: Path, mac: str) -> Path:
    base_dir = base_dir.resolve()
    p = (base_dir / f"{mac}.ipxe").resolve()
    if not p.is_relative_to(base_dir) or p.parent != base_dir:
        raise ValueError("path escapes pxe-map dir")
    return p


def infra_env_path(base_dir: Path, mce_cluster: str, infra_env: str) -> Path:
    """`<base_dir>/<mce_cluster>/<infra_env>.ipxe`, exactly one level down."""
    mce_cluster = validate_name(mce_cluster, "mce_cluster")
    infra_env = validate_name(infra_env, "infra_env")
    base_dir = base_dir.resolve()
    p = (base_dir / mce_cluster / f"{infra_env}.ipxe").resolve()
    if not p.is_relative_to(base_dir) or p.parent.parent != base_dir:
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


def write_mapping(base_dir: Path, mac: str, target: str) -> None:
    """Atomic upsert: create or overwrite. `target` is an http:// iPXE URL or
    an InfraEnv boot entry from infra_env_target()."""
    mac = canonical_mac(mac)
    target = validate_chain_target(target)
    content = f"#!ipxe\nchain {target}\n"
    path = mac_path(base_dir, mac)
    tmp = _write_tmp(path, content)
    # Under the same lock as delete_mapping's compare-then-unlink, so a
    # conditional delete can never remove a mapping written after it looked.
    with _locked_dir(path.parent):
        os.replace(tmp, path)  # atomic rename
    _restorecon(path)


def write_mapping_exclusive(base_dir: Path, mac: str, target: str) -> None:
    """Atomic strict create: raises FileExistsError if the mapping already
    exists, instead of the caller doing a separate check-then-write (which
    races under concurrent creates for the same MAC)."""
    mac = canonical_mac(mac)
    target = validate_chain_target(target)
    content = f"#!ipxe\nchain {target}\n"
    path = mac_path(base_dir, mac)
    tmp = _write_tmp(path, content)
    try:
        with _locked_dir(path.parent):
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


def delete_mapping(base_dir: Path, mac: str, mce_cluster: str | None = None) -> None:
    """Remove one MAC's mapping; absent is success.

    With `mce_cluster` (the caller's MCE) it is CONDITIONAL: a mapping that
    chains to ANOTHER MCE's InfraEnv boot entry raises ForeignMappingError and
    stays - remove-server on MCE-A must never take the boot away from a host
    MCE-B has since mapped. Anything else goes: this MCE's own entry, a direct
    URL from before boot entries existed, or a corrupt file. The compare and
    the unlink happen under the lock MAC writes take, so nothing written in
    between is removed.
    """
    mac = canonical_mac(mac)
    path = mac_path(base_dir, mac)
    if mce_cluster is None:
        path.unlink(missing_ok=True)
        return
    mce_cluster = validate_name(mce_cluster, "mce_cluster")
    with _locked_dir(path.parent):
        try:
            target = read_mapping(base_dir, mac)
        except FileNotFoundError:
            return
        except MappingFormatError:
            target = None
        ref = parse_infra_env_target(target) if target is not None else None
        if ref is not None and ref[0] != mce_cluster:
            raise ForeignMappingError(
                f"{mac} chains to {target}, a boot entry of MCE {ref[0]} - left in place"
            )
        path.unlink(missing_ok=True)


@contextmanager
def _locked_dir(directory: Path):
    """Exclusive flock on a directory, so a read-compare-replace inside it is
    atomic against every other writer - including the other threads of this
    process: FastAPI runs sync routes in a threadpool, and flock locks taken
    through separate open() calls exclude each other even within one process."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def write_infra_env_boot(
    base_dir: Path, mce_cluster: str, infra_env: str, ipxe_url: str, image_created_at: datetime
) -> None:
    """Point one InfraEnv's boot entry at its current iPXE script.

    Every MAC file mapped to this InfraEnv chains to this one file, so
    publishing a newer discovery image is one write here rather than one per
    host. Compare-and-set on `image_created_at` (the InfraEnv's
    `status.createdTime`): an equal or newer image overwrites, an older one
    raises StaleInfraEnvBootError and leaves the file alone - so of several
    installs racing to publish, the newest image always wins regardless of
    which request lands last, and a retry of the winner is still a success.
    """
    ipxe_url = validate_ipxe_url(ipxe_url)
    if image_created_at.tzinfo is None:
        raise ValueError("image_created_at must carry a timezone")
    path = infra_env_path(base_dir, mce_cluster, infra_env)
    path.parent.mkdir(mode=0o755, exist_ok=True)
    _restorecon(path.parent)
    content = (
        f"#!ipxe\n{_IMAGE_CREATED_AT_PREFIX}{image_created_at.isoformat()}\nchain {ipxe_url}\n"
    )
    with _locked_dir(path.parent):
        try:
            _, stored_at = _parse_infra_env_boot(path)
        except FileNotFoundError:
            stored_at = None
        if stored_at is not None and stored_at > image_created_at:
            raise StaleInfraEnvBootError(
                f"{mce_cluster}/{infra_env} already names an image created at "
                f"{stored_at.isoformat()}, newer than {image_created_at.isoformat()}"
            )
        tmp = _write_tmp(path, content)
        os.replace(tmp, path)  # atomic rename
    _restorecon(path)


def _parse_infra_env_boot(path: Path) -> tuple[str, datetime]:
    lines = path.read_text().splitlines()
    created = [line for line in lines if line.startswith(_IMAGE_CREATED_AT_PREFIX)]
    chain = [line for line in lines if line.startswith("chain ")]
    if len(created) != 1 or len(chain) != 1:
        raise MappingFormatError(f"{path} is not an InfraEnv boot entry")
    try:
        stored_at = datetime.fromisoformat(created[0][len(_IMAGE_CREATED_AT_PREFIX):])
    except ValueError:
        raise MappingFormatError(f"{path} has an unreadable image-created-at line") from None
    return chain[0][len("chain "):], stored_at


def read_infra_env_boot(base_dir: Path, mce_cluster: str, infra_env: str) -> tuple[str, datetime]:
    """(ipxe_url, image_created_at) of one InfraEnv boot entry."""
    return _parse_infra_env_boot(infra_env_path(base_dir, mce_cluster, infra_env))

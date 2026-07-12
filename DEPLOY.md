# Deploying pxe-map-api on a site PXE VM

Target: RHEL 9.4, SELinux enforcing, one instance per site, already running
dnsmasq (TFTP) and nginx serving `/var/www/pxe-map/` read-only (see
`SPEC.md` for that existing setup - this guide only covers this service).

## 0. Prerequisites

- Root (or sudo) access on the PXE VM.
- The repo copied onto the VM (air-gapped: no `git clone` from the
  internet, no `pip install` from PyPI). Bring it over via your usual
  offline transfer method (e.g. `scp` from a jump host, or a tarball via
  removable media).
- A local pip package mirror reachable from the VM, containing
  `fastapi==0.115.6`, `uvicorn==0.34.0`, `pydantic==2.10.4` and their
  transitive deps (see `requirements.txt`).
- `python3` (3.11+) and `semanage`/`restorecon` (RHEL 9.4 has these by
  default; `semanage` comes from `policycoreutils-python-utils` if
  missing: `dnf install policycoreutils-python-utils`).

## 1. Copy the repo onto the VM

```bash
scp -r pxe-map-api/ root@<pxe-vm>:/root/pxe-map-api-src
ssh root@<pxe-vm>
```

## 2. Point install.sh at your local mirror

Edit `deploy/install.sh` on the VM (or before copying it over) and set:

```bash
LOCAL_MIRROR="/path/to/your/local/pip-mirror"
```

to wherever your air-gapped mirror actually lives (a local directory, an
internal `pip.conf` index-url, or a mounted share with wheel files -
`--find-links` accepts a local path).

## 3. Run the installer

```bash
cd /root/pxe-map-api-src
sudo deploy/install.sh
```

This does all of the following, in order:

1. Creates the `pxe-map-api` system user (no login shell, no home dir).
2. Creates `/var/www/pxe-map` (dir `0755`, existing files re-chmod'd to
   `0644`), owned by `pxe-map-api`.
3. Registers the persistent SELinux fcontext rule
   (`semanage fcontext -a -t httpd_sys_content_t "/var/www/pxe-map(/.*)?"`)
   and runs `restorecon -Rv` once so the label survives reboots and
   `restorecon` reruns.
4. Copies `app/` and `requirements.txt` into `/opt/pxe-map-api`, creates a
   venv there, and installs deps from `LOCAL_MIRROR` with `pip install
   --no-index` (never touches the network).
5. Creates `/etc/pxe-map-api/env` from `deploy/env.example` (only if it
   doesn't already exist - reruns won't clobber a token you've already set).
6. Installs `deploy/pxe-map-api.service` to
   `/etc/systemd/system/pxe-map-api.service`, runs `systemctl daemon-reload`,
   and `systemctl enable`s it (does not start it yet).

If any step fails (e.g. `semanage` missing, or the mirror path is wrong),
fix the underlying issue and rerun - it's idempotent.

## 4. Configure the token and bind address

```bash
sudo vi /etc/pxe-map-api/env
```

Set a real token:

```
PXE_MAP_TOKEN=$(openssl rand -hex 32)
```

(run that command and paste the output in, or generate it separately -
don't leave `changeme`.)

Review `PXE_MAP_BIND` (default `0.0.0.0:8080`, listens on every interface).
See README.md's "Bind address" section for narrowing options (firewall
rule, specific management IP, or localhost + SSH tunnel) - at minimum,
confirm this VM's firewall doesn't expose 8080 on the network segment
booting hosts use.

## 5. Start it

```bash
sudo systemctl start pxe-map-api
sudo systemctl status pxe-map-api
sudo journalctl -u pxe-map-api -f   # tail logs while testing
```

## 6. Verify end-to-end on the VM

```bash
TOKEN=$(sudo grep PXE_MAP_TOKEN /etc/pxe-map-api/env | cut -d= -f2)
PORT=$(sudo grep PXE_MAP_BIND /etc/pxe-map-api/env | cut -d: -f2)

curl -sS localhost:${PORT}/healthz
# -> {"status":"ok"}

curl -sS -X PUT localhost:${PORT}/pxe-map/AA:BB:CC:DD:EE:01 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ipxe_url":"http://example.invalid/ipxe-script"}'
# -> 200 {"mac":"aabbccddee01","ipxe_url":"..."}

cat /var/www/pxe-map/aabbccddee01.ipxe
# -> #!ipxe
#    chain http://example.invalid/ipxe-script

ls -Z /var/www/pxe-map/aabbccddee01.ipxe
# -> should show httpd_sys_content_t

# confirm nginx (already configured, not part of this service) can serve it:
curl -sS http://localhost/pxe-map/aabbccddee01.ipxe

curl -sS -X DELETE localhost:${PORT}/pxe-map/aabbccddee01 \
  -H "Authorization: Bearer $TOKEN"
# -> 204, and the file is gone
```

If the `ls -Z` context isn't `httpd_sys_content_t`, the SELinux fcontext
rule from step 3 didn't take - rerun
`semanage fcontext -a -t httpd_sys_content_t "/var/www/pxe-map(/.*)?"` and
`restorecon -Rv /var/www/pxe-map` manually and check `sudo ausearch -m avc
-ts recent` for denials.

## 7. Point the central BMH-creation endpoint at it

Give the central endpoint's operators:

- This VM's reachable address and port (`PXE_MAP_BIND`).
- The token from `/etc/pxe-map-api/env`, delivered out-of-band (not over
  chat/email in plaintext if you can avoid it).

They call this service directly; there's nothing further to configure here.

## Upgrading

```bash
# on the VM, with a newer copy of the repo in /root/pxe-map-api-src
sudo systemctl stop pxe-map-api
sudo deploy/install.sh   # re-copies app/, reinstalls deps, refreshes the unit file
sudo systemctl start pxe-map-api
```

`install.sh` won't overwrite `/etc/pxe-map-api/env` if it already exists,
so your token and bind settings survive upgrades.

## Uninstalling

```bash
sudo systemctl disable --now pxe-map-api
sudo rm /etc/systemd/system/pxe-map-api.service
sudo systemctl daemon-reload
sudo rm -rf /opt/pxe-map-api /etc/pxe-map-api
# /var/www/pxe-map and its SELinux fcontext rule are left in place
# deliberately - it holds the source-of-truth files. Remove manually if
# you're decommissioning the whole VM:
#   sudo semanage fcontext -d "/var/www/pxe-map(/.*)?"
#   sudo rm -rf /var/www/pxe-map
#   sudo userdel pxe-map-api
```

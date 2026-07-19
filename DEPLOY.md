# Deploying pxe-map-api to the site PXE VMs (Ansible)

Target: 5 site PXE VMs (RHEL 9.4, SELinux enforcing), one per site, each
already running dnsmasq (TFTP) and nginx serving `/var/www/pxe-map/`
read-only (see `SPEC.md` for that existing setup - this guide only covers
this service). All 5 are installed and updated from the single
`ansible/` playbook in this repo.

## 0. Prerequisites

- A control node with SSH access to all 5 site VMs and `sudo`/root on each.
- `ansible-core` >= 2.15 on the control node:
  `python3 -m pip install --user ansible-core` (or your distro package).
- The required collections:
  ```bash
  cd ansible
  ansible-galaxy collection install -r requirements.yml
  ```
- A local pip package mirror reachable from every site VM, containing
  `fastapi==0.115.6`, `uvicorn==0.34.0`, `pydantic==2.10.4` and their
  transitive deps (see `requirements.txt`). Air-gapped: the role runs
  `pip install --no-index --find-links=<mirror>` - it never touches PyPI.
- You run `ansible-playbook` from within a checkout of this repo (the
  role's default `pxe_map_api_src_dir` assumes `ansible/` is a
  subdirectory of it, and copies `app/` + `requirements.txt` from there).

## 1. Point the inventory at your real VMs

Edit `ansible/inventory/hosts.yml` and replace the 5 placeholder
`ansible_host` values (`pxe-site1.example.com` ... `pxe-site5.example.com`)
with your real per-site PXE VM hostnames or IPs. The `pxe-site1` ...
`pxe-site5` names on the left are just inventory aliases - rename them to
match your sites if that's clearer (and rename the matching
`inventory/host_vars/<name>/` directory to match).

## 2. Set the local pip mirror

Edit `ansible/inventory/group_vars/pxe_map_api_sites.yml`:

```yaml
pxe_map_api_local_mirror: /path/to/your/local/pip-mirror
```

Override per-site in `inventory/host_vars/<site>/vars.yml` if a site's
mirror path differs.

## 3. Generate and vault a token per site

**Do not reuse one token across all 5 sites** - if one site's PXE VM is
compromised, the others must not be. For each site:

```bash
cd ansible/inventory/host_vars/pxe-site1   # repeat for site2..site5
cp vault.yml.example vault.yml
openssl rand -hex 32   # paste the output as pxe_map_api_token in vault.yml
ansible-vault encrypt vault.yml
```

You'll be prompted for a vault password (same or different per site, your
choice) - keep it somewhere safe; you need it on every future playbook run.
`vault.yml` (encrypted) is safe to commit; never commit the plaintext.

If you deliberately want a site to run unauthenticated (not recommended -
see `CLAUDE.md`), skip the token and set `pxe_map_api_allow_no_auth: true`
in that site's `vars.yml` instead. The playbook's preflight check refuses
to deploy an empty token otherwise.

## 4. Review the bind address per site

Default (`ansible/roles/pxe_map_api/defaults/main.yml`):
`pxe_map_api_bind: "0.0.0.0:8080"` - listens on every interface. This is
wider than SPEC.md's "narrowest sensible option" guidance (the service
only needs to be reachable by the central BMH-creation endpoint, never by
booting hosts). Bearer-token auth still protects every route except
`/healthz`, but for defense in depth, override per site in
`inventory/host_vars/<site>/vars.yml`:

```yaml
pxe_map_api_bind: "10.10.1.5:8080"   # this site's management-facing IP
```

or firewall 8080 down to the central endpoint's source IP on each VM.
Never bind on the interface that serves PXE/TFTP/nginx traffic to booting
hosts.

## 5. Run it

```bash
cd ansible
ansible-playbook site.yml --ask-vault-pass
```

(`--vault-password-file <path>` instead of `--ask-vault-pass` if you keep
the vault password in a file - keep that file out of git.)

This targets all 5 sites in parallel by default. To roll out to one site
first: `ansible-playbook site.yml --ask-vault-pass --limit pxe-site1`.

The role, in order, on each VM:

1. Fails the run (preflight) if that site has no token and hasn't
   explicitly opted into `pxe_map_api_allow_no_auth`, or if `app/` isn't
   found on the control node.
2. Creates the `pxe-map-api` system user (no login shell, no home dir).
3. Creates `/var/www/pxe-map` (dir `0755`, existing files re-chmod'd to
   `0644`), owned by that user.
4. Registers the persistent SELinux fcontext rule
   (`httpd_sys_content_t` on `/var/www/pxe-map(/.*)?`) and runs
   `restorecon -Rv` so the label survives reboots and reruns. Skipped
   automatically on a non-SELinux host (e.g. testing in a container).
5. Copies `app/` and `requirements.txt` into `/opt/pxe-map-api`, creates a
   venv there, and installs deps from the local mirror with
   `pip install --no-index` (never touches the network).
6. Renders `/etc/pxe-map-api/env` from that site's `vars.yml`/`vault.yml`
   (`PXE_MAP_DIR`, `PXE_MAP_BIND`, `PXE_MAP_TOKEN`) and the systemd unit,
   both from Jinja2 templates in `roles/pxe_map_api/templates/`.
7. Restarts the service if the env file, unit file, or app code changed;
   enables and starts it either way.

It's idempotent - rerun any time (e.g. after a code change, or to pick up
a rotated token) and only what actually changed gets touched.

## 6. Verify end-to-end on one VM

```bash
ssh <site-vm>
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
rule didn't take - rerun the playbook (step 5 above) and check
`sudo ausearch -m avc -ts recent` for denials.

Also: `sudo journalctl -u pxe-map-api -f` to tail logs while testing - see
`app/main.py`'s request-logging middleware for what gets logged per
request.

## 7. Point the central BMH-creation endpoint at it

Give the central endpoint's operators, per site:

- That VM's reachable address and port (`PXE_MAP_BIND`).
- The token from `inventory/host_vars/<site>/vault.yml`, delivered
  out-of-band (not over chat/email in plaintext if you can avoid it) -
  `ansible-vault view inventory/host_vars/<site>/vault.yml` to read it.

They call each site's service directly; there's nothing further to
configure here. This service does not do site-selection - that's the
caller's job.

## Upgrading / rotating a token

```bash
cd ansible
ansible-playbook site.yml --ask-vault-pass          # all 5 sites
ansible-playbook site.yml --ask-vault-pass --limit pxe-site3   # just one
```

Re-copies `app/`, reinstalls deps, re-renders the env/unit files, and
restarts the service only if something actually changed. To rotate a
site's token: `ansible-vault edit inventory/host_vars/<site>/vault.yml`,
then rerun the playbook for that site.

## Uninstalling

Not automated (removal is rare and destructive enough to want a human
doing it deliberately, not a playbook flag). On the VM:

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

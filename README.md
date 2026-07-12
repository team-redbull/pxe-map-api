# pxe-map-api

Manages the per-MAC iPXE chain files in `/var/www/pxe-map/` on a site PXE VM.
One instance runs per site, owns that VM's `/var/www/pxe-map/` directory, and
is called by the central BMH-creation endpoint to create/update/delete MAC ->
InfraEnv mappings. See `SPEC.md` for full background.

The files on disk are the source of truth; there is no reconciliation from
cluster state.

## API

Bearer token required on all routes except `/healthz`.

| Method | Path              | Body                    | Success | Errors             |
|--------|-------------------|--------------------------|---------|---------------------|
| POST   | `/pxe-map`        | `{"mac", "ipxe_url"}`    | 201     | 409 exists, 422 invalid |
| GET    | `/pxe-map/{mac}`  | -                        | 200     | 404                 |
| GET    | `/pxe-map`        | -                        | 200     | -                    |
| PUT    | `/pxe-map/{mac}`  | `{"ipxe_url"}`           | 200     | 422 invalid          |
| PATCH  | `/pxe-map/{mac}`  | `{"ipxe_url"}`           | 200     | 404, 422             |
| DELETE | `/pxe-map/{mac}`  | -                        | 204     | (idempotent, see below) |
| GET    | `/healthz`        | -                        | 200     | (no auth)            |

`{mac}` is accepted in any common format (colons, dashes, bare hex, mixed
case) and canonicalized server-side to 12 lowercase hex chars with no
separators - the exact form iPXE's `${net0/mac:hexraw}` produces, since that's
what nginx looks up at boot time.

`PUT` is the idempotent upsert (create or replace) and is what the caller
uses for the common case. `POST` and `PATCH` exist for callers that want
strict create/update guarantees instead.

`DELETE` on an absent mapping returns `204`, not `404`. This is deliberate
(see the code comment in `app/main.py`): the caller uses this on the
BMH-deletion path, and idempotent retries should not fail.

## Running locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # includes test deps

export PXE_MAP_DIR=/tmp/pxe-map-test
export PXE_MAP_TOKEN=dev-token
mkdir -p "$PXE_MAP_DIR"

.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

`tests/test_mapping.py` covers the correctness-critical pure functions
(MAC canonicalization, URL validation, atomic write, path-traversal
rejection, idempotent delete). `tests/test_api.py` covers the HTTP layer:
full CRUD round-trip, auth, and the POST/PATCH/PUT status-code semantics.

## Deployment (RHEL 9.4, SELinux enforcing)

See `DEPLOY.md` for the full step-by-step VM deployment guide (prereqs,
running `install.sh`, verifying end-to-end, upgrading, uninstalling).
Quick version:

```bash
sudo deploy/install.sh
```

This creates the `pxe-map-api` system user, creates `/var/www/pxe-map`
(`0755`, files `0644`, owned by that user), registers the persistent
SELinux fcontext (`httpd_sys_content_t`) and runs `restorecon`, installs the
app under `/opt/pxe-map-api` with its own venv (installed from a local
package mirror - air-gapped, no PyPI access), and installs + enables the
`pxe-map-api` systemd unit. It does not disable SELinux or loosen
permissions on the directory.

Before starting the service:

1. Set `PXE_MAP_TOKEN` in `/etc/pxe-map-api/env` to a strong random value
   (e.g. `openssl rand -hex 32`). Never commit or hardcode it - it's read
   from the environment via systemd's `EnvironmentFile`.
2. Review `PXE_MAP_BIND` (see "Bind address" below).
3. `systemctl start pxe-map-api`

### Bind address

Default: `PXE_MAP_BIND=0.0.0.0:8080` - listens on every interface on the
VM. This is wider than SPEC.md's "narrowest sensible option" guidance
(the service only needs to be reachable by the central BMH-creation
endpoint, never by booting hosts). Bearer-token auth still protects every
route except `/healthz`, but if you want defense in depth:

- Restrict with a host firewall, e.g.
  `firewall-cmd --add-rich-rule='rule family="ipv4" source address=<bmh-endpoint-ip> port port=8080 protocol=tcp accept'`
  and drop 8080 by default.
- Or narrow `PXE_MAP_BIND` to the site VM's management-facing interface
  IP:port instead of `0.0.0.0`.
- Or bind `127.0.0.1:8080` and reach it over an SSH tunnel / bastion if
  the central endpoint doesn't need direct network access.

Whichever you choose, never bind on the interface that serves PXE/TFTP/nginx
traffic to booting hosts.

### nginx (existing, not managed by this service)

This service only writes files; nginx (already configured on the PXE VM)
serves them read-only:

```nginx
server {
    listen 80;
    root /var/www;
    location /pxe-map/ { autoindex off; try_files $uri =404; }
}
```

## Out of scope

No DHCP, IPMI/BMH/NMStateConfig creation, or site-selection logic - all of
that lives in the calling central endpoint. No cluster-state reconciliation.
No HTTPS on this API (the booting-host path is plain HTTP served by nginx,
unrelated to this service).

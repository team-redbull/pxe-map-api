# pxe-map-api

Manages the per-MAC iPXE chain files in `/var/www/pxe-map/` on a site PXE VM.
One instance runs per site, owns that VM's `/var/www/pxe-map/` directory, and
is called by the central BMH-creation endpoint to create/update/delete MAC ->
InfraEnv mappings. See `SPEC.md` for full background.

The files on disk are the source of truth; there is no reconciliation from
cluster state.

## API

Bearer token required on all routes except `/healthz` - unless
`PXE_MAP_TOKEN` is left unset, in which case auth is disabled entirely (a
startup warning is logged; see "Local env" in `CLAUDE.md`).

| Method | Path              | Body                    | Success | Errors             |
|--------|-------------------|--------------------------|---------|---------------------|
| POST   | `/pxe-map`        | `{"mac", "ipxe_url"}`    | 201     | 409 exists, 422 invalid |
| GET    | `/pxe-map/{mac}`  | -                        | 200     | 404 absent, 422 invalid mac |
| GET    | `/pxe-map`        | -                        | 200     | -                    |
| PUT    | `/pxe-map/{mac}`  | `{"ipxe_url"}`           | 200     | 422 invalid          |
| PATCH  | `/pxe-map/{mac}`  | `{"ipxe_url"}`           | 200     | 404 absent, 422 invalid |
| DELETE | `/pxe-map/{mac}`  | -                        | 204     | 422 invalid mac (idempotent otherwise, see below) |
| GET    | `/healthz`        | -                        | 200     | (no auth)            |

`{mac}` is accepted in any common format (colons, dashes, bare hex, mixed
case) and canonicalized server-side to 12 lowercase hex chars with no
separators - the exact form iPXE's `${net0/mac:hexraw}` produces, since that's
what nginx looks up at boot time. A malformed `{mac}` (e.g. `3js`) is
rejected with `422` on every route that takes one, before anything else in
the route runs.

`PUT` is the idempotent upsert (create or replace) and is what the caller
uses for the common case. `POST` and `PATCH` exist for callers that want
strict create/update guarantees instead.

`DELETE` on an absent-but-valid mapping returns `204`, not `404`. This is
deliberate (see the code comment in `app/routers/mappings.py`): the caller
uses this on the BMH-deletion path, and idempotent retries should not fail.
A malformed MAC on `DELETE` still returns `422`, same as every other route.

## Running locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx   # pytest/httpx are test-only, not in requirements.txt

cp .env.example .env   # fill in real values
mkdir -p "$(grep PXE_MAP_DIR .env | cut -d= -f2)"

.venv/bin/python -m app.main
```

`.env` is loaded automatically (`python-dotenv`, see `app/config.py`) - no
`export`/`source` needed. It's only ever read locally: production loads
its environment from systemd's `EnvironmentFile=` before this process even
starts (see `DEPLOY.md`), and `load_dotenv()`'s default `override=False`
means it never touches a variable systemd already set.

`python -m app.main` also picks up `PXE_MAP_BIND` from `.env` to set the
bind address, so no `--host`/`--port` flags are needed. If you'd rather
use the uvicorn CLI directly (`uvicorn app.main:app`), you must still pass
`--host`/`--port` explicitly - it resolves those before importing this
module, so `.env` can't reach them that way.

Either way, the server now refuses to start (raises before accepting any
connections) if `PXE_MAP_DIR` isn't set - it doesn't wait for the first
request to fail.

## Tests

```bash
.venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/python -m pytest tests/ -v
```

`tests/test_mapping.py` covers the correctness-critical pure functions
(MAC canonicalization, URL validation, atomic write, path-traversal
rejection, idempotent delete). `tests/test_api.py` covers the HTTP layer:
full CRUD round-trip, auth, and the POST/PATCH/PUT status-code semantics.

## Deployment (RHEL 9.4, SELinux enforcing, 5 site VMs via Ansible)

Deployment automation lives in a **separate** repo,
`pxe-map-api-ansible` - see `DEPLOY.md` here for the short version and a
pointer, and that repo's own `DEPLOY.md` for the full step-by-step guide
(prereqs, inventory setup, per-site tokens via `ansible-vault`, verifying
end-to-end, upgrading). Quick version, with both repos checked out as
siblings:

```bash
cd ../pxe-map-api-ansible
ansible-galaxy collection install -r requirements.yml
# edit inventory/hosts.yml (real hostnames) and, per site, generate +
# ansible-vault-encrypt a token in inventory/host_vars/<site>/vault.yml -
# see DEPLOY.md steps 1-4 for the full walkthrough
ansible-playbook site.yml --ask-vault-pass
```

This creates the `pxe-map-api` system user, creates `/var/www/pxe-map`
(`0755`, files `0644`, owned by that user), registers the persistent
SELinux fcontext (`httpd_sys_content_t`) and runs `restorecon`, installs
this app under `/opt/pxe-map-api` with its own venv (installed from a local
package mirror - air-gapped, no PyPI access), renders `/etc/pxe-map-api/env`
and the systemd unit from that site's inventory vars, and enables +
starts the `pxe-map-api` service. It does not disable SELinux or loosen
permissions on the directory. It's idempotent and targets all 5 sites in
one run (or `--limit <site>` for just one).

The playbook refuses to run against a site with no `PXE_MAP_TOKEN` unless
that site's inventory explicitly sets `pxe_map_api_allow_no_auth: true` -
see "Bind address" below and `CLAUDE.md` for why that default exists.

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
  IP:port instead of `0.0.0.0` - set `pxe_map_api_bind` in that site's
  `inventory/host_vars/<site>/vars.yml` in the `pxe-map-api-ansible` repo.
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

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
| POST   | `/pxe-map`        | `{"mac", <target>}`      | 201     | 409 exists, 422 invalid |
| GET    | `/pxe-map/{mac}`  | -                        | 200     | 404 absent, 422 invalid mac |
| GET    | `/pxe-map`        | -                        | 200     | -                    |
| PUT    | `/pxe-map/{mac}`  | `{<target>}`             | 200     | 422 invalid          |
| PATCH  | `/pxe-map/{mac}`  | `{<target>}`             | 200     | 404 absent, 422 invalid |
| DELETE | `/pxe-map/{mac}`  | -                        | 204     | 422 invalid mac (idempotent otherwise, see below) |
| DELETE | `/pxe-map/{mac}?mce_cluster=<mce>` | -        | 204     | 409 the MAC chains to ANOTHER MCE's boot entry (kept), 422 invalid |
| PUT    | `/pxe-map/{mce_cluster}/{infra_env}` | `{"ipxe_url", "image_created_at"}` | 200 | 409 older image, 422 invalid |
| GET    | `/pxe-map/{mce_cluster}/{infra_env}` | -     | 200     | 404 absent, 422 invalid name |
| GET    | `/healthz`        | -                        | 200     | (no auth)            |

`<target>` is EXACTLY ONE of `"ipxe_url": "http://..."` (the MAC chains
straight to that URL) or `"infra_env": {"mce_cluster", "name"}` (the MAC
chains to that InfraEnv's boot entry - see below). Responses carry whichever
one the MAC has; the other key is left out.

### InfraEnv boot entries

`PUT /pxe-map/{mce_cluster}/{infra_env}` writes
`/var/www/pxe-map/<mce_cluster>/<infra_env>.ipxe`:

```
#!ipxe
# image-created-at: 2026-10-19T10:00:05+00:00
chain http://<assisted-service-ipxe route>/...?file_name=ipxe-script&api_key=...
```

A MAC mapped with `"infra_env"` holds a relative `chain
<mce_cluster>/<infra_env>.ipxe`, which iPXE resolves against the MAC file's own
URL - so it lands on the same nginx `/pxe-map/` tree, and this service never
needs to know its own public hostname. Every MAC of an InfraEnv chains through
one entry, so publishing a regenerated discovery image is one write instead of
one per host. The MAC may be mapped before its entry exists (install-server
does exactly that, and publishes the entry once the host's NMStateConfig is in
the image, before anything powers the host on).

`image_created_at` is the InfraEnv's `status.createdTime` and the write is a
compare-and-set on it, under a `flock` on the MCE directory: an equal or newer
image overwrites (so a retry is a 200), an OLDER one is refused with `409` and
leaves the file alone. Of several installs racing to publish, the newest image
wins no matter whose request lands last. `mce_cluster` and `infra_env` must be
lowercase Kubernetes object names (rejected, never fixed up), which also keeps
them from escaping the pxe-map dir.

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

`DELETE /pxe-map/{mac}?mce_cluster=<mce>` is the CONDITIONAL form
remove-server uses: if the MAC chains to the boot entry of a DIFFERENT MCE it
answers `409` and keeps the file (that MCE mapped the machine since), and
otherwise deletes it - this MCE's entry, a direct-URL entry, or a corrupt
file. The read-compare-unlink runs under the same `flock` that MAC writes
take, so a mapping written concurrently is never removed by mistake.

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
rejection, idempotent delete, InfraEnv boot entries incl. the compare-and-set
under concurrent writers). `tests/test_api.py` covers the HTTP layer:
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
serves them read-only. The `location` below already covers the
`<mce_cluster>/` subdirectories that InfraEnv boot entries live in:

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

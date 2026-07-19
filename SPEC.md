0. Assume zero context — what this service is

You (Claude Code) are building one small FastAPI service. It manages small
text files in a single directory. That's the whole service. Do not over-build it.

But it sits inside a larger system, and you need that context to make the right
safety decisions, so here is the whole picture.

The larger project (background — you are NOT building this part)

We provision bare-metal servers into disconnected (air-gapped) OpenShift
HyperShift hosted clusters via MCE (multicluster engine). Some servers are
Cisco UCS managed by Fabric Interconnect and have no virtual media, so we
cannot mount the discovery ISO — they must network boot (PXE). We built an
external PXE pipeline for them. It works like this:


A server is told (over IPMI) to PXE-boot, then power-cycled.
Its UEFI firmware sends a DHCP request. An external Windows DHCP server
(with an ACI DHCP relay in the path) answers with a next-server and a boot
filename.
Stage 1: the firmware gets ipxe.efi and downloads it over TFTP from a
per-site PXE VM (RHEL 9.4) running dnsmasq in TFTP-only mode.
ipxe.efi runs and sends a second DHCP request, this time identifying
itself with DHCP option 77 = iPXE. The Windows DHCP policy matches that and
returns the filename boot.ipxe.
Stage 2: iPXE downloads boot.ipxe (also TFTP from the PXE VM). That file
is global and identical for every host. Its only job is:


   #!ipxe
   chain http://<pxe-vm-ip>/pxe-map/${net0/mac:hexraw}.ipxe

${net0/mac:hexraw} = the booting NIC's MAC as 12 lowercase hex chars with no
separators, e.g. aabbccddee01.
6. iPXE therefore makes an HTTP GET to the PXE VM's nginx, for a file
named after its own MAC: /pxe-map/aabbccddee01.ipxe. nginx serves
/var/www/pxe-map/ read-only over HTTP.
7. That per-MAC file contains one line: chain <url-of-this-host's-InfraEnv-ipxe-script>.
It points the host at the correct cluster's boot artifacts (kernel, initrd,
rootfs) served over HTTP by the hub cluster.
8. The host boots the discovery image and registers as an Agent in MCE.

The already-existing PXE VM setup (context for your file/SELinux decisions)

On each per-site PXE VM (RHEL 9.4, SELinux enforcing):


dnsmasq is configured TFTP-only (port=0, enable-tftp,
tftp-root=/var/lib/tftpboot), serving ipxe.efi and boot.ipxe.
nginx serves /var/www/pxe-map/ read-only over HTTP:


nginx  server {
      listen 80;
      root /var/www;
      location /pxe-map/ { autoindex off; try_files $uri =404; }
  }


Files in /var/www/pxe-map/ must have SELinux type httpd_sys_content_t for
nginx to read them.
The VM is backed up hourly (external to this service).


What THIS service does

The per-MAC files in /var/www/pxe-map/ currently get created by hand. This
service replaces that with an API so our central automation can create/update/
delete them programmatically. One instance of this service runs on each site's
PXE VM, owning that VM's /var/www/pxe-map/ directory.

The caller is a separate central "BMH-creation endpoint" (NOT in this repo, do
not build it). When it provisions a host it also calls this service to write the
host's MAC→InfraEnv mapping; when it deletes a host it calls this service to
delete the mapping. The caller decides which site VM to call and which HTTP
method to use.

Source of truth = the files on disk. No reconciliation from cluster state.


1. Tech + constraints


Python, FastAPI + uvicorn + pydantic. Keep dependencies minimal.
Runs on RHEL 9.4, SELinux enforcing. Do NOT disable SELinux or make the
directory world-writable. Handle file contexts correctly (see §5).
Air-gapped: no runtime network calls to PyPI or anywhere. Install deps from
a local mirror; vendor if needed.
The service is the only writer to /var/www/pxe-map/, runs as its own
dedicated user, and files are owned by that user (not world-writable).



2. Files to produce (name them exactly)

Note: this tree is the original build spec. The actual layout evolved as
the service grew (see CLAUDE.md's "Architecture" section for the current,
accurate file-by-file breakdown - `app/` is now several files following
FastAPI's own multi-file layout, and `deploy/` was replaced by an
`ansible/` playbook covering all 5 site VMs - see §7 below and DEPLOY.md).
The rest of this document (§0-6, §8-10) still describes *why* the app's
safety checks exist and is unchanged.

pxe-map-api/
├── app/
│   ├── __init__.py
│   ├── main.py            # FastAPI app + routes + auth dependency
│   ├── mapping.py         # core logic: canonicalize, validate, atomic write, delete, list
│   └── config.py          # env-driven config (dir, token, bind)
├── tests/
│   ├── test_mapping.py    # unit tests for mapping.py (the correctness-critical bits)
│   └── test_api.py        # API tests incl. auth
├── ansible/                # deployment automation, see §7 and DEPLOY.md
├── requirements.txt
└── README.md


3. API surface (full CRUD)

Bearer token required on all routes except /healthz. {mac} accepted in any
common format, canonicalized server-side.

MethodPathBodySuccessErrorsPOST/pxe-map{ "mac": "...", "ipxe_url": "..." }201 {mac,ipxe_url}409 if exists, 422 invalidGET/pxe-map/{mac}—200 {mac,ipxe_url}404GET/pxe-map—200 [{mac,ipxe_url},...]—PUT/pxe-map/{mac}{ "ipxe_url": "..." }200 {mac,ipxe_url}422 invalidPATCH/pxe-map/{mac}{ "ipxe_url": "..." }200 {mac,ipxe_url}404, 422DELETE/pxe-map/{mac}—204see noteGET/healthz—200(no auth)


PUT is the idempotent upsert — create or replace. This is the method the
caller uses for the common case ("if the mapping exists change it, else create
it"). POST (strict create, 409 on exists) and PATCH (strict update, 404 on
absent) are provided for cases where the caller wants those guarantees.
DELETE on absent mapping: make it idempotent → 204 (friendlier for
automation retries on the BMH-deletion path). Leave a code comment noting this
is a deliberate choice over strict 404.



4. Core logic (app/mapping.py) — this is where correctness lives

Implement these as pure, unit-testable functions, independent of FastAPI.

4.1 MAC canonicalization — reject, never sanitize

pythonimport re
MAC_RE = re.compile(r"^[0-9a-f]{12}$")

def canonical_mac(raw: str) -> str:
    stripped = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
    if not MAC_RE.match(stripped):
        raise ValueError(f"invalid MAC: {raw!r}")
    return stripped

Output MUST equal iPXE's ${net0/mac:hexraw} form: 12 lowercase hex, no
separators. Any mismatch = 404 at boot = silent reboot loop on real hardware.
This is the single most important function in the service. Reject anything that
isn't exactly 12 hex chars after stripping separators; do not try to "fix" it.

4.2 URL validation — it goes into a file a machine executes

pythonfrom urllib.parse import urlparse

def validate_ipxe_url(url: str) -> str:
    p = urlparse(url)
    if p.scheme != "http":
        raise ValueError("ipxe_url must be http:// (iPXE TLS ciphers too old for OCP ingress)")
    if not p.netloc:
        raise ValueError("ipxe_url missing host")
    if any(c in url for c in "\r\n"):
        raise ValueError("ipxe_url contains control characters")
    return url

Enforce http:// and reject https://: iPXE's TLS cipher list is too old for
OpenShift ingress and an https chain URL fails silently at boot. Reject CR/LF
so a caller can't inject extra iPXE commands into the file.

4.3 Path construction — MAC becomes a filename (injection vector)

pythonfrom pathlib import Path

def mac_path(base_dir: Path, mac: str) -> Path:
    p = (base_dir / f"{mac}.ipxe").resolve()
    if p.parent != base_dir.resolve():
        raise ValueError("path escapes pxe-map dir")
    return p

4.4 Atomic write + SELinux relabel

pythonimport os, subprocess

def write_mapping(base_dir: Path, mac: str, ipxe_url: str) -> None:
    mac = canonical_mac(mac)
    ipxe_url = validate_ipxe_url(ipxe_url)
    content = f"#!ipxe\nchain {ipxe_url}\n"
    path = mac_path(base_dir, mac)
    tmp = path.with_suffix(".ipxe.tmp")
    tmp.write_text(content)
    os.replace(tmp, path)                                    # atomic rename
    subprocess.run(["restorecon", str(path)], check=False)   # httpd_sys_content_t


Atomic write (temp + os.replace): a host PXE-booting mid-write always sees
a complete old or complete new file, never a half-written one.
restorecon after write applies the SELinux context so nginx can read it.


Also implement read_mapping, list_mappings, delete_mapping (delete is
idempotent — deleting an absent file is not an error), all going through
canonical_mac / mac_path.


5. SELinux (enforcing — do it right, once)

install.sh runs this one-time so the label is persistent:

bashsemanage fcontext -a -t httpd_sys_content_t "/var/www/pxe-map(/.*)?"
restorecon -Rv /var/www/pxe-map

Then the per-write restorecon in §4.4 keeps every new file labeled correctly.
This gives nginx read access without loosening SELinux or making the dir
world-writable. Do not use setenforce 0 or a permissive shortcut anywhere.


6. Auth


Static bearer token, checked on every route except /healthz.
Token read from env (PXE_MAP_TOKEN), provided via systemd EnvironmentFile.
Never hardcode it.
FastAPI dependency compares Authorization: Bearer <t> using
secrets.compare_digest (constant-time). 401 on mismatch/absent.



7. Packaging / deploy

Superseded by an Ansible playbook (`ansible/`, see DEPLOY.md) that installs
this service on all 5 site VMs, but the requirements it satisfies are
unchanged from the original design:

pxe-map-api.service (rendered from ansible/roles/pxe_map_api/templates/
pxe-map-api.service.j2): runs as user pxe-map-api,
EnvironmentFile=/etc/pxe-map-api/env, Restart=on-failure.
Bind address from PXE_MAP_BIND. The API must be reachable by the central BMH
endpoint but does NOT need to be reachable by booting hosts — bind as narrowly
as the network allows (localhost + tunnel, or the management-facing interface).
Document the choice in README; default to the narrowest sensible option.
(Current default is wider than this guidance for operational convenience -
see README.md's "Bind address" section for the tradeoff and how to narrow
it per site.)
The ansible role: creates the pxe-map-api user; creates /var/www/pxe-map
owned by it (dir 0755, files 0644); runs the SELinux fcontext + restorecon;
renders and installs the env file + systemd unit from per-site inventory
vars; enables and starts the service. One token per site
(ansible-vault-encrypted), not shared across sites.
Air-gapped: requirements.txt pinned; installed from a local mirror via
`pip install --no-index --find-links=<mirror>`, same as before.



8. Tests (definition of done — these catch the silent-failure bugs)

tests/test_mapping.py:


canonical_mac: AA:BB:CC:DD:EE:01, aa-bb-cc-dd-ee-01, AABBCCDDEE01 all
→ aabbccddee01; reject 11 chars, 13 chars, non-hex, empty, and a traversal
string like ../../etc/passwd.
validate_ipxe_url: reject https://..., a URL with no host, and one with an
embedded \n; accept a normal http:// URL.
write_mapping writes exactly #!ipxe\nchain <url>\n and leaves no .tmp.
mac_path rejects any input resolving outside the base dir.
delete_mapping is idempotent (no error on absent file).


tests/test_api.py:
6. Full CRUD round-trip with a valid token.
7. 401 with no/wrong token on a protected route; /healthz works without.
8. POST existing mac → 409; PATCH absent mac → 404; PUT twice is
idempotent and returns 200 both times.

All tests must pass. Run them and show passing output as proof.


9. End-to-end verification (prove the feature works)

After build, run this sequence and show output:

bash# 1. start the service with a test token and a temp PXE_MAP_DIR
# 2. PUT a mapping
curl -sS -X PUT localhost:PORT/pxe-map/AA:BB:CC:DD:EE:01 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ipxe_url":"http://assisted-image-service-ipxe.apps.example/api/.../ipxe-script"}'
# expect 200

# 3. confirm the file on disk is named canonically and has exactly 2 lines
cat $PXE_MAP_DIR/aabbccddee01.ipxe
# expect:
#   #!ipxe
#   chain http://assisted-image-service-ipxe.apps.example/api/.../ipxe-script

# 4. GET it back
curl -sS localhost:PORT/pxe-map/aabbccddee01 -H "Authorization: Bearer $TOKEN"
# expect 200 {mac, ipxe_url}

# 5. reject https
curl -sS -X PUT localhost:PORT/pxe-map/aabbccddee02 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ipxe_url":"https://foo/bar"}'
# expect 422

# 6. DELETE, then GET → 404
curl -sS -X DELETE localhost:PORT/pxe-map/aabbccddee01 -H "Authorization: Bearer $TOKEN"
curl -sS -o /dev/null -w "%{http_code}\n" localhost:PORT/pxe-map/aabbccddee01 -H "Authorization: Bearer $TOKEN"
# expect 404

The filename in step 3 being aabbccddee01.ipxe (canonical) when the input was
AA:BB:CC:DD:EE:01 is the key proof — that canonicalization is what makes iPXE's
${net0/mac:hexraw} lookup succeed at boot.


10. Out of scope — do NOT build


No reconciliation from cluster state (files are source of truth by decision).
No DHCP logic — Windows DHCP is separate and already configured.
No IPMI / BMH / NMStateConfig creation — that's the calling endpoint.
No site-selection logic — the caller picks which site VM to hit.
No HTTPS on this API unless §7's bind decision requires it; the booting-host
path is plain HTTP served by nginx, not this service.
Don't add abstractions, extra config, or error handling for cases that can't
occur. Validate only at the boundary (the API input). Keep it minimal.





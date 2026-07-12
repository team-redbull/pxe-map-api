# pxe-map-api

FastAPI service that manages per-MAC iPXE chain files in a directory (one
file per MAC, named `<mac>.ipxe`). Full background and requirements are in
`SPEC.md` - read it before making non-trivial changes; it explains *why*
each safety check in `app/mapping.py` exists.

## Commands

```bash
# tests (pytest.ini puts the repo root on sys.path so this works from anywhere)
.venv/bin/pytest -v

# run locally
set -a; source .env; set +a   # see "Local env" below
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Local env

Config is read directly from `os.environ` (`app/config.py`) - there is no
dotenv auto-loading, by design, to keep runtime deps minimal for the
air-gapped RHEL deployment. A `.env` file is **not** picked up automatically.

1. `cp .env.example .env` (`.env` is git-ignored)
2. Load it before running the server: `set -a; source .env; set +a`

Production uses `deploy/env.example` -> `/etc/pxe-map-api/env`, loaded via
systemd's `EnvironmentFile=` instead - see `deploy/install.sh`.

## Architecture

- `app/mapping.py` - pure functions, no FastAPI dependency. This is where
  correctness lives: MAC canonicalization (reject, never sanitize), URL
  validation (http:// only, no CR/LF), path construction (traversal
  rejection), atomic write + `restorecon`. Unit tested in
  `tests/test_mapping.py`.
- `app/main.py` - FastAPI routes + bearer-token auth dependency
  (`secrets.compare_digest`). Thin - translates `mapping.py` exceptions
  into HTTP status codes. Tested in `tests/test_api.py`.
- `app/config.py` - env-driven config, read once at import time.

## Conventions / constraints (do not relax without re-reading SPEC.md)

- No new runtime dependencies beyond `requirements.txt` without a strong
  reason - deploys are air-gapped, installed from a local package mirror.
- Never accept `https://` for `ipxe_url` - iPXE's TLS ciphers are too old
  for OCP ingress; this must stay a hard rejection, not a warning.
- `canonical_mac` rejects malformed input, it does not try to fix it - a
  wrong MAC means a silent PXE-boot reboot loop on real hardware.
- `DELETE` on an absent mapping is a deliberate `204`, not `404` (see
  comment in `app/main.py`) - don't "fix" this to be strict.
- Don't disable SELinux or loosen `/var/www/pxe-map` permissions; the
  `restorecon` call in `write_mapping` is required, not optional (it's
  wrapped in `try/except FileNotFoundError` only so tests run on
  non-SELinux hosts).

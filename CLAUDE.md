# pxe-map-api

FastAPI service that manages per-MAC iPXE chain files in a directory (one
file per MAC, named `<mac>.ipxe`). Full background and requirements are in
`SPEC.md` - read it before making non-trivial changes; it explains *why*
each safety check in `app/mapping.py` exists.

## Commands

```bash
# tests (pytest.ini puts the repo root on sys.path so this works from anywhere)
.venv/bin/pytest -v

# run locally - reads .env automatically, host/port come from PXE_MAP_BIND
.venv/bin/python -m app.main

# equivalent, but --host/--port must be given explicitly since the uvicorn
# CLI resolves them before it ever imports this module (so .env can't reach
# them there - see "Local env" below)
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Local env

`app/config.py` calls `load_dotenv()` (`python-dotenv`) at import time, so
`.env` is picked up automatically - no `export`/`source` needed. This is a
**deliberate reversal** of the original design (which avoided the
dependency entirely for a minimal air-gapped runtime) - confirmed with the
user; don't revert to manual sourcing without checking first.
`load_dotenv()`'s default `override=False` means it never clobbers a
variable already present in the environment, so this is a no-op in
production (systemd's `EnvironmentFile=` has already populated the
environment before the process starts, and there's no `.env` file there
anyway) - the new dependency only ever does anything for local dev.

1. `cp .env.example .env` (`.env` is git-ignored)
2. Fill in real values and just run the server - no sourcing step.

Run with `python -m app.main` (see "Commands" above), not the uvicorn CLI,
if you want `PXE_MAP_BIND` from `.env` to actually control the bind
address with no `--host`/`--port` flags - `app/main.py`'s `if __name__ ==
"__main__"` block reads it after `get_config()` has already run
`load_dotenv()`. The uvicorn CLI form still works but always needs
`--host`/`--port` explicitly: it resolves those from real environment
variables (`UVICORN_HOST`/`UVICORN_PORT`) before it ever imports this
module, so a `.env` file can't reach them there.

`app/main.py`'s FastAPI `lifespan` calls `get_config()` before the server
starts accepting connections, so a missing `PXE_MAP_DIR` aborts startup
with the `RuntimeError` from `app/config.py` under both run forms above -
it does not wait for the first request.

Production (5 site VMs) is deployed from a **separate** repo,
`pxe-map-api-ansible` (checked out as a sibling directory - the Ansible
role copies `app/` and `requirements.txt` from here into each VM). It renders
`/etc/pxe-map-api/env` from per-site inventory vars, loaded via systemd's
`EnvironmentFile=`. Per-site tokens live `ansible-vault`-encrypted there,
not hand-edited on the VM after install. See that repo's `DEPLOY.md` for
the full guide; see its `CLAUDE.md` if you're changing the deployment
automation itself - none of that lives in this repo anymore.

`PXE_MAP_DIR` is required - `Config()` raises `RuntimeError` at startup
(not a bare `KeyError`) if it's unset. `PXE_MAP_BIND` is optional, defaults
to `0.0.0.0:8080`. `PXE_MAP_TOKEN` is optional too: if unset (or blank),
`require_auth` (`app/dependencies.py`) lets every request through
unauthenticated, and `Config` logs a `WARNING` at startup naming the bind
address - this is a deliberate, user-confirmed deviation from SPEC.md's
"auth is mandatory" assumption, not a bug. Don't make `PXE_MAP_TOKEN`
required again without checking with the user first.

## Architecture

Follows FastAPI's official "Bigger Applications - Multiple Files" layout
(https://fastapi.tiangolo.com/tutorial/bigger-applications/):

- `app/mapping.py` - pure functions, no FastAPI dependency. This is where
  correctness lives: MAC canonicalization (reject, never sanitize), URL
  validation (http:// only, no CR/LF), path construction (traversal
  rejection), atomic write (`write_mapping`) and atomic exclusive-create
  (`write_mapping_exclusive`) + `restorecon`. Raises plain `ValueError` /
  `FileNotFoundError` / `FileExistsError` / `MappingFormatError` - never
  FastAPI types. Unit tested in `tests/test_mapping.py`.
- `app/config.py` - env-driven config. `get_config()` is `@lru_cache`d and
  injected via `Depends`, not read once into a module global - tests
  override it per-test with `get_config.cache_clear()` after
  `monkeypatch.setenv(...)`.
- `app/schemas.py` - Pydantic request/response models. `MappingCreate` /
  `MappingUpdate` validate `mac`/`ipxe_url` with `field_validator`s that
  call straight into `app/mapping.py`, so invalid input never reaches a
  route body - FastAPI turns the `ValueError` into its standard 422
  automatically.
- `app/dependencies.py` - `require_auth` (bearer token via
  `secrets.compare_digest`, skipped entirely if `PXE_MAP_TOKEN` is unset -
  see "Local env" above) and `CanonicalMac`, an `Annotated` path-param type
  used by every route that takes a MAC (GET/PUT/PATCH/DELETE). A malformed
  MAC gets FastAPI's standard 422 "value error, invalid mac" before the
  route body runs, on every one of those routes - a deliberate,
  user-requested deviation from SPEC.md's route table, which originally
  had GET/PATCH return 404 instead for a malformed MAC. Don't reintroduce
  that split without checking with the user first.
- `app/exceptions.py` - registers global handlers
  (`FileNotFoundError`->404, `FileExistsError`->409,
  `MappingFormatError`->500, `ValueError`->422 safety net) so routes don't
  each need their own try/except around `mapping.py` calls.
- `app/routers/mappings.py` - the `/pxe-map*` routes, `APIRouter(prefix=...,
  dependencies=[Depends(require_auth)])` so auth is declared once, not
  per-route. Thin - routes call straight into `mapping.py` and let
  `app/exceptions.py` translate failures. Tested in `tests/test_api.py`.
- `app/routers/health.py` - `/healthz`, no auth.
- `app/main.py` - creates the `FastAPI()` app, registers the exception
  handlers, installs the request-logging middleware (`@app.middleware`,
  logs every request exactly once regardless of which router/handler
  produced the response), includes the routers.

There is no `deploy/` or `ansible/` directory in this repo - deployment
automation for all 5 site VMs lives in the separate `pxe-map-api-ansible`
repo (see "Local env" above and `DEPLOY.md`). Don't recreate deployment
tooling here; that repo copies `app/` and `requirements.txt` from a
checkout of this one.

## Conventions / constraints (do not relax without re-reading SPEC.md)

- No new runtime dependencies beyond `requirements.txt` without a strong
  reason - deploys are air-gapped, installed from a local package mirror
  (`python-dotenv` is the one user-confirmed exception - see "Local env").
  Any new addition means updating the mirror too, on all 5 sites - flag
  that in `pxe-map-api-ansible`'s `DEPLOY.md` "Prerequisites" list.
- Never accept `https://` for `ipxe_url` - iPXE's TLS ciphers are too old
  for OCP ingress; this must stay a hard rejection, not a warning.
- `canonical_mac` rejects malformed input, it does not try to fix it - a
  wrong MAC means a silent PXE-boot reboot loop on real hardware.
- `DELETE` on an absent mapping is a deliberate `204`; a malformed MAC on
  DELETE is `422` (a caller bug, not "already gone") - see comment in
  `app/routers/mappings.py`. Don't collapse these two cases back together.
- Don't disable SELinux or loosen `/var/www/pxe-map` permissions; the
  `restorecon` call in `write_mapping`/`write_mapping_exclusive` is
  required, not optional (it's wrapped in `try/except FileNotFoundError`
  only so tests run on non-SELinux hosts).
- `FileNotFoundError`/`FileExistsError`/`ValueError` are handled as global
  exception types in `app/exceptions.py` - this is only safe because
  `app/mapping.py` is the *only* filesystem/validation code in this
  service. If a future module raises one of these for an unrelated reason,
  it will be misreported as a mapping error; use a more specific exception
  type instead of reaching for these three.

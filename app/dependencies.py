import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AfterValidator

from app.config import Config, get_config
from app.mapping import canonical_mac

bearer_scheme = HTTPBearer(auto_error=False)

ConfigDep = Annotated[Config, Depends(get_config)]


def require_auth(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    config: ConfigDep,
) -> None:
    if config.pxe_map_token is None:
        # No PXE_MAP_TOKEN configured - Config already logged a startup
        # warning about this (app/config.py). Every request is allowed
        # through; this is not the secure default, it's an explicit opt-out.
        return
    if credentials is None or not secrets.compare_digest(
        credentials.credentials, config.pxe_map_token
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


# Every route that takes a MAC in the path (GET/PUT/PATCH/DELETE) types it
# as this instead of plain `str`, so a malformed MAC (e.g. "3js") is
# rejected with FastAPI's standard 422 "value error, invalid mac" response
# before the route body ever runs - the same validation POST already gets
# from the `field_validator`s in app/schemas.py, applied uniformly instead
# of each route re-implementing its own check.
CanonicalMac = Annotated[str, AfterValidator(canonical_mac)]

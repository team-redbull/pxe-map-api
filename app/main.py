import secrets

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_config
from app.mapping import (
    canonical_mac,
    delete_mapping,
    list_mappings,
    read_mapping,
    write_mapping,
)

app = FastAPI()
config = get_config()
bearer_scheme = HTTPBearer(auto_error=False)


def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    if credentials is None or not secrets.compare_digest(
        credentials.credentials, config.pxe_map_token
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


class MappingCreate(BaseModel):
    mac: str
    ipxe_url: str


class MappingUpdate(BaseModel):
    ipxe_url: str


class MappingOut(BaseModel):
    mac: str
    ipxe_url: str


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/pxe-map", status_code=201, dependencies=[Depends(require_auth)])
def create_mapping(body: MappingCreate) -> MappingOut:
    try:
        mac = canonical_mac(body.mac)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        read_mapping(config.pxe_map_dir, mac)
        raise HTTPException(status_code=409, detail="mapping already exists")
    except FileNotFoundError:
        pass

    try:
        write_mapping(config.pxe_map_dir, mac, body.ipxe_url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return MappingOut(mac=mac, ipxe_url=body.ipxe_url)


@app.get("/pxe-map/{mac}", dependencies=[Depends(require_auth)])
def get_mapping(mac: str) -> MappingOut:
    try:
        canonical = canonical_mac(mac)
        ipxe_url = read_mapping(config.pxe_map_dir, canonical)
    except (ValueError, FileNotFoundError):
        raise HTTPException(status_code=404, detail="mapping not found")
    return MappingOut(mac=canonical, ipxe_url=ipxe_url)


@app.get("/pxe-map", dependencies=[Depends(require_auth)])
def list_all_mappings() -> list[MappingOut]:
    return [
        MappingOut(mac=mac, ipxe_url=ipxe_url)
        for mac, ipxe_url in list_mappings(config.pxe_map_dir)
    ]


@app.put("/pxe-map/{mac}", dependencies=[Depends(require_auth)])
def upsert_mapping(mac: str, body: MappingUpdate) -> MappingOut:
    try:
        canonical = canonical_mac(mac)
        write_mapping(config.pxe_map_dir, canonical, body.ipxe_url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return MappingOut(mac=canonical, ipxe_url=body.ipxe_url)


@app.patch("/pxe-map/{mac}", dependencies=[Depends(require_auth)])
def update_mapping(mac: str, body: MappingUpdate) -> MappingOut:
    try:
        canonical = canonical_mac(mac)
    except ValueError:
        raise HTTPException(status_code=404, detail="mapping not found")

    try:
        read_mapping(config.pxe_map_dir, canonical)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="mapping not found")

    try:
        write_mapping(config.pxe_map_dir, canonical, body.ipxe_url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    return MappingOut(mac=canonical, ipxe_url=body.ipxe_url)


@app.delete("/pxe-map/{mac}", status_code=204, dependencies=[Depends(require_auth)])
def delete_mapping_route(mac: str):
    # Deliberate choice: delete is idempotent (204 even if the mapping never
    # existed) rather than strict 404, since the caller uses this on the
    # BMH-deletion path and retries should not fail.
    try:
        canonical = canonical_mac(mac)
    except ValueError:
        return Response(status_code=204)
    delete_mapping(config.pxe_map_dir, canonical)
    return Response(status_code=204)

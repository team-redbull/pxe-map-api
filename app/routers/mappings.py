from fastapi import APIRouter, Depends, Response, status

from app.dependencies import CanonicalMac, ConfigDep, require_auth
from app.mapping import (
    delete_mapping,
    list_mappings,
    read_mapping,
    write_mapping,
    write_mapping_exclusive,
)
from app.schemas import MappingCreate, MappingOut, MappingUpdate

# Auth applies to every route on this router - declared once here instead
# of on each route with dependencies=[Depends(require_auth)].
router = APIRouter(prefix="/pxe-map", tags=["mappings"], dependencies=[Depends(require_auth)])


@router.post("", status_code=status.HTTP_201_CREATED)
def create_mapping(body: MappingCreate, config: ConfigDep) -> MappingOut:
    # body.mac/body.ipxe_url are already validated+canonical (see
    # app/schemas.py); FileExistsError (409) and ValueError (422, should be
    # unreachable given the schema validation) are handled globally, see
    # app/exceptions.py.
    write_mapping_exclusive(config.pxe_map_dir, body.mac, body.ipxe_url)
    return MappingOut(mac=body.mac, ipxe_url=body.ipxe_url)


@router.get("/{mac}")
def get_mapping(mac: CanonicalMac, config: ConfigDep) -> MappingOut:
    # A malformed mac is rejected with 422 before this runs (see
    # CanonicalMac); a well-formed but absent mac raises FileNotFoundError
    # here, translated to 404 by the global handler in app/exceptions.py.
    ipxe_url = read_mapping(config.pxe_map_dir, mac)
    return MappingOut(mac=mac, ipxe_url=ipxe_url)


@router.get("")
def list_all_mappings(config: ConfigDep) -> list[MappingOut]:
    return [
        MappingOut(mac=mac, ipxe_url=ipxe_url) for mac, ipxe_url in list_mappings(config.pxe_map_dir)
    ]


@router.put("/{mac}")
def upsert_mapping(mac: CanonicalMac, body: MappingUpdate, config: ConfigDep) -> MappingOut:
    write_mapping(config.pxe_map_dir, mac, body.ipxe_url)
    return MappingOut(mac=mac, ipxe_url=body.ipxe_url)


@router.patch("/{mac}")
def update_mapping(mac: CanonicalMac, body: MappingUpdate, config: ConfigDep) -> MappingOut:
    read_mapping(config.pxe_map_dir, mac)  # existence probe; absent -> FileNotFoundError -> 404
    write_mapping(config.pxe_map_dir, mac, body.ipxe_url)
    return MappingOut(mac=mac, ipxe_url=body.ipxe_url)


@router.delete("/{mac}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mapping_route(mac: CanonicalMac, config: ConfigDep):
    # Deliberate choice: DELETE of an absent mapping is idempotent (204),
    # not a strict 404, since the caller uses this on the BMH-deletion path
    # and retries should not fail. A malformed MAC is a different failure
    # mode (a caller bug, not "already gone"): CanonicalMac rejects it with
    # FastAPI's standard 422 before this function body ever runs.
    delete_mapping(config.pxe_map_dir, mac)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import AfterValidator

from app.dependencies import CanonicalMac, ConfigDep, require_auth
from app.mapping import (
    delete_mapping,
    list_mappings,
    read_mapping,
    validate_name,
    write_mapping,
    write_mapping_exclusive,
)
from app.schemas import MappingCreate, MappingOut, MappingUpdate

OptionalMceCluster = Annotated[
    str | None,
    Query(),
    AfterValidator(lambda v: None if v is None else validate_name(v, "mce_cluster")),
]

# Auth applies to every route on this router - declared once here instead
# of on each route with dependencies=[Depends(require_auth)].
router = APIRouter(prefix="/pxe-map", tags=["mappings"], dependencies=[Depends(require_auth)])

# A mapping carries EITHER ipxe_url OR infra_env (app/schemas.py); leaving the
# unused one out keeps a URL mapping's response exactly what it always was.
_ONE_TARGET = {"response_model_exclude_none": True}


@router.post("", status_code=status.HTTP_201_CREATED, **_ONE_TARGET)
def create_mapping(body: MappingCreate, config: ConfigDep) -> MappingOut:
    # body.mac/body's target are already validated+canonical (see
    # app/schemas.py); FileExistsError (409) and ValueError (422, should be
    # unreachable given the schema validation) are handled globally, see
    # app/exceptions.py.
    target = body.chain_target()
    write_mapping_exclusive(config.pxe_map_dir, body.mac, target)
    return MappingOut.from_target(body.mac, target)


@router.get("/{mac}", **_ONE_TARGET)
def get_mapping(mac: CanonicalMac, config: ConfigDep) -> MappingOut:
    # A malformed mac is rejected with 422 before this runs (see
    # CanonicalMac); a well-formed but absent mac raises FileNotFoundError
    # here, translated to 404 by the global handler in app/exceptions.py.
    return MappingOut.from_target(mac, read_mapping(config.pxe_map_dir, mac))


@router.get("", **_ONE_TARGET)
def list_all_mappings(config: ConfigDep) -> list[MappingOut]:
    return [MappingOut.from_target(mac, target) for mac, target in list_mappings(config.pxe_map_dir)]


@router.put("/{mac}", **_ONE_TARGET)
def upsert_mapping(mac: CanonicalMac, body: MappingUpdate, config: ConfigDep) -> MappingOut:
    # An infra_env target is written whether or not that InfraEnv's boot
    # entry exists yet: install-server maps a host's MACs BEFORE it publishes
    # the entry, because the entry must wait for the host's NMStateConfig to
    # reach the discovery image. Nothing powers the host on until after both.
    target = body.chain_target()
    write_mapping(config.pxe_map_dir, mac, target)
    return MappingOut.from_target(mac, target)


@router.patch("/{mac}", **_ONE_TARGET)
def update_mapping(mac: CanonicalMac, body: MappingUpdate, config: ConfigDep) -> MappingOut:
    read_mapping(config.pxe_map_dir, mac)  # existence probe; absent -> FileNotFoundError -> 404
    target = body.chain_target()
    write_mapping(config.pxe_map_dir, mac, target)
    return MappingOut.from_target(mac, target)


@router.delete("/{mac}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mapping_route(mac: CanonicalMac, config: ConfigDep, mce_cluster: OptionalMceCluster = None):
    # Deliberate choice: DELETE of an absent mapping is idempotent (204),
    # not a strict 404, since the caller uses this on the BMH-deletion path
    # and retries should not fail. A malformed MAC is a different failure
    # mode (a caller bug, not "already gone"): CanonicalMac rejects it with
    # FastAPI's standard 422 before this function body ever runs.
    #
    # `?mce_cluster=<caller's MCE>` makes it conditional (remove-server): a
    # mapping chaining to ANOTHER MCE's boot entry is a 409 and stays. See
    # delete_mapping in app/mapping.py.
    delete_mapping(config.pxe_map_dir, mac, mce_cluster)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

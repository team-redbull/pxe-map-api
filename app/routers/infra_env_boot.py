from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import AfterValidator

from app.dependencies import ConfigDep, require_auth
from app.mapping import read_infra_env_boot, validate_name, write_infra_env_boot
from app.schemas import InfraEnvBootOut, InfraEnvBootUpdate

# Same prefix as the MAC routes, mirroring the on-disk layout: the boot entry
# for InfraEnv <infra_env> on MCE <mce_cluster> is served by nginx as
# /pxe-map/<mce_cluster>/<infra_env>.ipxe. Two path segments, so it never
# collides with the one-segment /pxe-map/{mac} routes.
router = APIRouter(prefix="/pxe-map", tags=["infra-env-boot"], dependencies=[Depends(require_auth)])

MceCluster = Annotated[str, AfterValidator(lambda v: validate_name(v, "mce_cluster"))]
InfraEnvName = Annotated[str, AfterValidator(lambda v: validate_name(v, "infra_env"))]


@router.put("/{mce_cluster}/{infra_env}")
def publish_infra_env_boot(
    mce_cluster: MceCluster, infra_env: InfraEnvName, body: InfraEnvBootUpdate, config: ConfigDep
) -> InfraEnvBootOut:
    # A body whose image_created_at is OLDER than the stored entry raises
    # StaleInfraEnvBootError -> 409 (app/exceptions.py); equal or newer
    # overwrites, so a retried publish is still a 200.
    write_infra_env_boot(
        config.pxe_map_dir, mce_cluster, infra_env, body.ipxe_url, body.image_created_at
    )
    return InfraEnvBootOut(
        mce_cluster=mce_cluster,
        infra_env=infra_env,
        ipxe_url=body.ipxe_url,
        image_created_at=body.image_created_at,
    )


@router.get("/{mce_cluster}/{infra_env}")
def get_infra_env_boot(
    mce_cluster: MceCluster, infra_env: InfraEnvName, config: ConfigDep
) -> InfraEnvBootOut:
    # Absent -> FileNotFoundError -> 404 via the global handler.
    ipxe_url, image_created_at = read_infra_env_boot(config.pxe_map_dir, mce_cluster, infra_env)
    return InfraEnvBootOut(
        mce_cluster=mce_cluster,
        infra_env=infra_env,
        ipxe_url=ipxe_url,
        image_created_at=image_created_at,
    )

from pydantic import AwareDatetime, BaseModel, field_validator, model_validator

from app.mapping import (
    canonical_mac,
    infra_env_target,
    parse_infra_env_target,
    validate_ipxe_url,
    validate_name,
)


class InfraEnvRef(BaseModel):
    """An InfraEnv boot entry, `<mce_cluster>/<name>.ipxe` under the pxe-map dir."""

    mce_cluster: str
    name: str

    @field_validator("mce_cluster")
    @classmethod
    def _validate_mce_cluster(cls, v: str) -> str:
        return validate_name(v, "mce_cluster")

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        return validate_name(v, "infra_env")


class _MappingTarget(BaseModel):
    """What a MAC chains to: EXACTLY ONE of a direct iPXE URL, or an InfraEnv
    boot entry that holds the URL for every MAC mapped to that InfraEnv."""

    ipxe_url: str | None = None
    infra_env: InfraEnvRef | None = None

    # Validated here (not in the route) so an invalid mac/url never reaches
    # the route body at all - FastAPI turns a ValueError raised here into
    # its standard 422 response automatically.
    @field_validator("ipxe_url")
    @classmethod
    def _validate_url(cls, v: str | None) -> str | None:
        return None if v is None else validate_ipxe_url(v)

    @model_validator(mode="after")
    def _exactly_one_target(self):
        if (self.ipxe_url is None) == (self.infra_env is None):
            raise ValueError("give exactly one of ipxe_url or infra_env")
        return self

    def chain_target(self) -> str:
        if self.infra_env is not None:
            return infra_env_target(self.infra_env.mce_cluster, self.infra_env.name)
        return self.ipxe_url


class MappingCreate(_MappingTarget):
    mac: str

    @field_validator("mac")
    @classmethod
    def _canonicalize_mac(cls, v: str) -> str:
        return canonical_mac(v)


class MappingUpdate(_MappingTarget):
    pass


class MappingOut(BaseModel):
    """`ipxe_url` for a MAC chaining straight to a URL, `infra_env` for one
    chaining to an InfraEnv boot entry. Routes serialize with
    response_model_exclude_none, so the absent one is left out entirely."""

    mac: str
    ipxe_url: str | None = None
    infra_env: InfraEnvRef | None = None

    @classmethod
    def from_target(cls, mac: str, target: str) -> "MappingOut":
        ref = parse_infra_env_target(target)
        if ref is None:
            return cls(mac=mac, ipxe_url=target)
        return cls(mac=mac, infra_env=InfraEnvRef(mce_cluster=ref[0], name=ref[1]))


class InfraEnvBootUpdate(BaseModel):
    ipxe_url: str
    # The InfraEnv's `status.createdTime` - the version a write is compared on.
    image_created_at: AwareDatetime

    @field_validator("ipxe_url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        return validate_ipxe_url(v)


class InfraEnvBootOut(BaseModel):
    mce_cluster: str
    infra_env: str
    ipxe_url: str
    image_created_at: AwareDatetime

from pydantic import BaseModel, field_validator

from app.mapping import canonical_mac, validate_ipxe_url


class MappingCreate(BaseModel):
    mac: str
    ipxe_url: str

    # Validated here (not in the route) so an invalid mac/url never reaches
    # the route body at all - FastAPI turns a ValueError raised here into
    # its standard 422 response automatically.
    @field_validator("mac")
    @classmethod
    def _canonicalize_mac(cls, v: str) -> str:
        return canonical_mac(v)

    @field_validator("ipxe_url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        return validate_ipxe_url(v)


class MappingUpdate(BaseModel):
    ipxe_url: str

    @field_validator("ipxe_url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        return validate_ipxe_url(v)


class MappingOut(BaseModel):
    mac: str
    ipxe_url: str

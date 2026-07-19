import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("pxe_map_api")


class Config:
    def __init__(self) -> None:
        # Required: the service has nowhere to write mappings without it,
        # so refuse to start rather than fail on the first request.
        try:
            self.pxe_map_dir = Path(os.environ["PXE_MAP_DIR"])
        except KeyError:
            raise RuntimeError(
                "PXE_MAP_DIR is not set. Copy .env.example to .env (or set "
                "it in the systemd EnvironmentFile) - this service will not "
                "start without a directory to write mappings to."
            ) from None

        self.pxe_map_bind = os.environ.get("PXE_MAP_BIND", "0.0.0.0:8080")

        # Optional: if unset, every route runs unauthenticated. Blank is
        # treated the same as unset (a stray `PXE_MAP_TOKEN=` line should
        # not become a token that's the empty string).
        self.pxe_map_token = os.environ.get("PXE_MAP_TOKEN") or None
        if self.pxe_map_token is None:
            logger.warning(
                "PXE_MAP_TOKEN is not set - every route is UNAUTHENTICATED. "
                "Anyone who can reach %s can rewrite PXE boot targets. Set "
                "PXE_MAP_TOKEN before exposing this beyond localhost.",
                self.pxe_map_bind,
            )


@lru_cache
def get_config() -> Config:
    return Config()

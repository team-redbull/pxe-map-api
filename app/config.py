import os
from pathlib import Path


class Config:
    def __init__(self) -> None:
        self.pxe_map_dir = Path(os.environ["PXE_MAP_DIR"])
        self.pxe_map_token = os.environ["PXE_MAP_TOKEN"]
        self.pxe_map_bind = os.environ.get("PXE_MAP_BIND", "0.0.0.0:8080")


def get_config() -> Config:
    return Config()

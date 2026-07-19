import pytest

from app.config import Config


class TestPxeMapDirRequired:
    def test_missing_pxe_map_dir_raises_runtime_error(self, monkeypatch):
        monkeypatch.delenv("PXE_MAP_DIR", raising=False)
        with pytest.raises(RuntimeError, match="PXE_MAP_DIR"):
            Config()


class TestPxeMapTokenOptional:
    def test_missing_token_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PXE_MAP_DIR", str(tmp_path))
        monkeypatch.delenv("PXE_MAP_TOKEN", raising=False)
        assert Config().pxe_map_token is None

    def test_blank_token_is_treated_as_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PXE_MAP_DIR", str(tmp_path))
        monkeypatch.setenv("PXE_MAP_TOKEN", "")
        assert Config().pxe_map_token is None

    def test_provided_token_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PXE_MAP_DIR", str(tmp_path))
        monkeypatch.setenv("PXE_MAP_TOKEN", "a-real-token")
        assert Config().pxe_map_token == "a-real-token"


class TestPxeMapBindDefault:
    def test_missing_bind_defaults_to_0000_8080(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PXE_MAP_DIR", str(tmp_path))
        monkeypatch.delenv("PXE_MAP_BIND", raising=False)
        assert Config().pxe_map_bind == "0.0.0.0:8080"

    def test_provided_bind_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PXE_MAP_DIR", str(tmp_path))
        monkeypatch.setenv("PXE_MAP_BIND", "127.0.0.1:9000")
        assert Config().pxe_map_bind == "127.0.0.1:9000"

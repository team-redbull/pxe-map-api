from pathlib import Path

import pytest

from app.mapping import (
    canonical_mac,
    delete_mapping,
    mac_path,
    read_mapping,
    validate_ipxe_url,
    write_mapping,
)


class TestCanonicalMac:
    @pytest.mark.parametrize(
        "raw",
        ["AA:BB:CC:DD:EE:01", "aa-bb-cc-dd-ee-01", "AABBCCDDEE01"],
    )
    def test_accepts_and_canonicalizes(self, raw):
        assert canonical_mac(raw) == "aabbccddee01"

    @pytest.mark.parametrize(
        "raw",
        [
            "AA:BB:CC:DD:EE:0",       # 11 chars
            "AA:BB:CC:DD:EE:001",     # 13 chars
            "AA:BB:CC:DD:EE:ZZ",      # non-hex
            "",                       # empty
            "../../etc/passwd",       # traversal
        ],
    )
    def test_rejects(self, raw):
        with pytest.raises(ValueError):
            canonical_mac(raw)


class TestValidateIpxeUrl:
    def test_accepts_normal_http_url(self):
        url = "http://assisted-image-service-ipxe.apps.example/api/ipxe-script"
        assert validate_ipxe_url(url) == url

    def test_rejects_https(self):
        with pytest.raises(ValueError):
            validate_ipxe_url("https://foo/bar")

    def test_rejects_no_host(self):
        with pytest.raises(ValueError):
            validate_ipxe_url("http://")

    def test_rejects_embedded_newline(self):
        with pytest.raises(ValueError):
            validate_ipxe_url("http://foo/bar\nchain http://evil/x")


class TestWriteMapping:
    def test_writes_exact_content_and_leaves_no_tmp(self, tmp_path: Path):
        write_mapping(tmp_path, "AA:BB:CC:DD:EE:01", "http://example/script")
        path = tmp_path / "aabbccddee01.ipxe"
        assert path.read_text() == "#!ipxe\nchain http://example/script\n"
        assert not (tmp_path / "aabbccddee01.ipxe.tmp").exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_round_trip_read(self, tmp_path: Path):
        write_mapping(tmp_path, "aabbccddee01", "http://example/script")
        assert read_mapping(tmp_path, "aabbccddee01") == "http://example/script"


class TestMacPath:
    def test_rejects_traversal_outside_base_dir(self, tmp_path: Path):
        with pytest.raises(ValueError):
            mac_path(tmp_path, "../outside")

    def test_valid_mac_stays_inside_base_dir(self, tmp_path: Path):
        p = mac_path(tmp_path, "aabbccddee01")
        assert p.parent == tmp_path.resolve()
        assert p.name == "aabbccddee01.ipxe"


class TestDeleteMapping:
    def test_idempotent_on_absent_file(self, tmp_path: Path):
        # must not raise even though nothing was ever written
        delete_mapping(tmp_path, "aabbccddee01")
        delete_mapping(tmp_path, "aabbccddee01")

    def test_deletes_existing_file(self, tmp_path: Path):
        write_mapping(tmp_path, "aabbccddee01", "http://example/script")
        path = tmp_path / "aabbccddee01.ipxe"
        assert path.exists()
        delete_mapping(tmp_path, "aabbccddee01")
        assert not path.exists()

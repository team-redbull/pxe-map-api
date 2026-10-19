from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.mapping import (
    ForeignMappingError,
    MappingFormatError,
    StaleInfraEnvBootError,
    canonical_mac,
    delete_mapping,
    infra_env_path,
    infra_env_target,
    list_mappings,
    mac_path,
    parse_infra_env_target,
    read_infra_env_boot,
    read_mapping,
    validate_ipxe_url,
    validate_name,
    write_infra_env_boot,
    write_mapping,
    write_mapping_exclusive,
)

MCE = "ocp4-mce-bat-yam-01"
INFRA_ENV = "cisco-m6-bat-yam-64c-512gb"
T0 = datetime(2026, 10, 19, 10, 0, 5, tzinfo=timezone.utc)


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


class TestWriteMappingExclusive:
    def test_creates_when_absent(self, tmp_path: Path):
        write_mapping_exclusive(tmp_path, "aabbccddee01", "http://example/a")
        assert read_mapping(tmp_path, "aabbccddee01") == "http://example/a"

    def test_raises_file_exists_error_and_leaves_original_untouched(self, tmp_path: Path):
        write_mapping_exclusive(tmp_path, "aabbccddee01", "http://example/a")
        with pytest.raises(FileExistsError):
            write_mapping_exclusive(tmp_path, "aabbccddee01", "http://example/b")
        assert read_mapping(tmp_path, "aabbccddee01") == "http://example/a"

    def test_leaves_no_tmp_file(self, tmp_path: Path):
        write_mapping_exclusive(tmp_path, "aabbccddee01", "http://example/a")
        assert list(tmp_path.glob("*.tmp")) == []


class TestReadMappingCorruptFile:
    def test_raises_mapping_format_error_when_no_chain_line(self, tmp_path: Path):
        (tmp_path / "aabbccddee01.ipxe").write_text("garbage, no such directive\n")
        with pytest.raises(MappingFormatError):
            read_mapping(tmp_path, "aabbccddee01")


class TestListMappingsSkipsCorruptFiles:
    def test_corrupt_file_does_not_break_the_listing(self, tmp_path: Path):
        write_mapping(tmp_path, "aabbccddee01", "http://example/a")
        (tmp_path / "aabbccddee02.ipxe").write_text("garbage\n")
        assert list_mappings(tmp_path) == [("aabbccddee01", "http://example/a")]


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


class TestConditionalDelete:
    """remove-server's delete: never takes a MAC away from ANOTHER MCE."""

    def test_this_mces_entry_is_removed(self, tmp_path: Path):
        write_mapping(tmp_path, "aabbccddee01", infra_env_target(MCE, INFRA_ENV))
        delete_mapping(tmp_path, "aabbccddee01", MCE)
        assert not (tmp_path / "aabbccddee01.ipxe").exists()

    def test_another_mces_entry_is_refused_and_kept(self, tmp_path: Path):
        write_mapping(tmp_path, "aabbccddee01", infra_env_target("ocp4-mce-other", INFRA_ENV))
        with pytest.raises(ForeignMappingError, match="ocp4-mce-other"):
            delete_mapping(tmp_path, "aabbccddee01", MCE)
        assert read_mapping(tmp_path, "aabbccddee01") == f"ocp4-mce-other/{INFRA_ENV}.ipxe"

    def test_a_direct_url_mapping_from_before_boot_entries_is_removed(self, tmp_path: Path):
        write_mapping(tmp_path, "aabbccddee01", "http://example/script")
        delete_mapping(tmp_path, "aabbccddee01", MCE)
        assert not (tmp_path / "aabbccddee01.ipxe").exists()

    def test_a_corrupt_mapping_is_removed(self, tmp_path: Path):
        (tmp_path / "aabbccddee01.ipxe").write_text("garbage\n")
        delete_mapping(tmp_path, "aabbccddee01", MCE)
        assert not (tmp_path / "aabbccddee01.ipxe").exists()

    def test_absent_is_success(self, tmp_path: Path):
        delete_mapping(tmp_path, "aabbccddee01", MCE)

    def test_an_invalid_mce_name_is_rejected(self, tmp_path: Path):
        write_mapping(tmp_path, "aabbccddee01", "http://example/script")
        with pytest.raises(ValueError):
            delete_mapping(tmp_path, "aabbccddee01", "../x")
        assert (tmp_path / "aabbccddee01.ipxe").exists()


class TestValidateName:
    @pytest.mark.parametrize("raw", [MCE, INFRA_ENV, "a", "a.b-c"])
    def test_accepts_kubernetes_object_names(self, raw):
        assert validate_name(raw, "x") == raw

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "..",
            ".",
            "../etc",
            "a/b",
            "Upper",       # rejected, never lowercased
            "-leading",
            "trailing-",
            "a" * 254,
        ],
    )
    def test_rejects(self, raw):
        with pytest.raises(ValueError):
            validate_name(raw, "x")


class TestInfraEnvTarget:
    def test_is_relative_to_the_mac_files_directory(self):
        assert infra_env_target(MCE, INFRA_ENV) == f"{MCE}/{INFRA_ENV}.ipxe"

    def test_round_trips_through_parse(self):
        assert parse_infra_env_target(infra_env_target(MCE, INFRA_ENV)) == (MCE, INFRA_ENV)

    @pytest.mark.parametrize(
        "target",
        ["http://example/script", "../x/y.ipxe", "a/b/c.ipxe", "Bad/name.ipxe", "a/b.txt"],
    )
    def test_anything_else_is_not_an_infra_env_target(self, target):
        assert parse_infra_env_target(target) is None


class TestMacMappedToAnInfraEnv:
    def test_the_mac_file_chains_to_the_infra_env_entry(self, tmp_path: Path):
        write_mapping(tmp_path, "aabbccddee01", infra_env_target(MCE, INFRA_ENV))
        assert (tmp_path / "aabbccddee01.ipxe").read_text() == (
            f"#!ipxe\nchain {MCE}/{INFRA_ENV}.ipxe\n"
        )
        assert read_mapping(tmp_path, "aabbccddee01") == f"{MCE}/{INFRA_ENV}.ipxe"

    @pytest.mark.parametrize("target", ["../x/y.ipxe", "a/b/c.ipxe", "relative-script"])
    def test_a_relative_target_that_is_not_an_infra_env_entry_is_rejected(self, tmp_path, target):
        with pytest.raises(ValueError):
            write_mapping(tmp_path, "aabbccddee01", target)
        assert list(tmp_path.iterdir()) == []

    def test_infra_env_entries_never_show_up_as_mac_mappings(self, tmp_path: Path):
        write_mapping(tmp_path, "aabbccddee01", "http://example/a")
        write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://example/b", T0)
        assert list_mappings(tmp_path) == [("aabbccddee01", "http://example/a")]


class TestInfraEnvPath:
    def test_is_one_directory_per_mce(self, tmp_path: Path):
        p = infra_env_path(tmp_path, MCE, INFRA_ENV)
        assert p == tmp_path.resolve() / MCE / f"{INFRA_ENV}.ipxe"

    @pytest.mark.parametrize("mce,infra_env", [("..", INFRA_ENV), (MCE, ".."), ("a/b", INFRA_ENV)])
    def test_rejects_traversal(self, tmp_path: Path, mce, infra_env):
        with pytest.raises(ValueError):
            infra_env_path(tmp_path, mce, infra_env)


class TestWriteInfraEnvBoot:
    def test_writes_the_script_with_its_image_time_and_no_tmp(self, tmp_path: Path):
        write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/ipxe-script?api_key=k", T0)
        path = tmp_path / MCE / f"{INFRA_ENV}.ipxe"
        assert path.read_text() == (
            "#!ipxe\n"
            "# image-created-at: 2026-10-19T10:00:05+00:00\n"
            "chain http://as/ipxe-script?api_key=k\n"
        )
        assert list((tmp_path / MCE).glob("*.tmp")) == []
        assert read_infra_env_boot(tmp_path, MCE, INFRA_ENV) == ("http://as/ipxe-script?api_key=k", T0)

    def test_a_newer_image_replaces_it(self, tmp_path: Path):
        write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/old", T0)
        later = T0 + timedelta(seconds=30)
        write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/new", later)
        assert read_infra_env_boot(tmp_path, MCE, INFRA_ENV) == ("http://as/new", later)

    def test_the_same_image_again_is_a_success(self, tmp_path: Path):
        # A retried publish of the winner must not fail.
        write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/a", T0)
        write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/a", T0)
        assert read_infra_env_boot(tmp_path, MCE, INFRA_ENV) == ("http://as/a", T0)

    def test_an_older_image_is_refused_and_leaves_the_newer_one(self, tmp_path: Path):
        # The install that read the InfraEnv first but published last must not
        # roll every host of the InfraEnv back to its older image.
        later = T0 + timedelta(seconds=30)
        write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/new", later)
        with pytest.raises(StaleInfraEnvBootError, match="newer"):
            write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/old", T0)
        assert read_infra_env_boot(tmp_path, MCE, INFRA_ENV) == ("http://as/new", later)

    def test_times_compare_across_timezones(self, tmp_path: Path):
        write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/a", T0)
        same_instant = T0.astimezone(timezone(timedelta(hours=3)))
        write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/b", same_instant)
        assert read_infra_env_boot(tmp_path, MCE, INFRA_ENV)[0] == "http://as/b"

    def test_https_is_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError):
            write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "https://as/a", T0)

    def test_a_naive_time_is_rejected(self, tmp_path: Path):
        with pytest.raises(ValueError):
            write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/a", T0.replace(tzinfo=None))

    def test_concurrent_publishes_leave_the_newest_image(self, tmp_path: Path):
        # Five installs publishing at once, in no particular order - the
        # threadpool FastAPI runs sync routes in does exactly this.
        times = [T0 + timedelta(seconds=s) for s in (3, 0, 4, 1, 2)] * 8

        def publish(at: datetime) -> None:
            try:
                write_infra_env_boot(tmp_path, MCE, INFRA_ENV, f"http://as/{at.second}", at)
            except StaleInfraEnvBootError:
                pass

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(publish, times))

        newest = max(times)
        assert read_infra_env_boot(tmp_path, MCE, INFRA_ENV) == (f"http://as/{newest.second}", newest)
        assert list((tmp_path / MCE).glob("*.tmp")) == []

    def test_two_infra_envs_of_one_mce_share_its_directory(self, tmp_path: Path):
        write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/a", T0)
        write_infra_env_boot(tmp_path, MCE, "dell-r650-tlv", "http://as/b", T0)
        assert sorted(p.name for p in (tmp_path / MCE).iterdir()) == [
            f"{INFRA_ENV}.ipxe",
            "dell-r650-tlv.ipxe",
        ]


class TestReadInfraEnvBoot:
    def test_absent_is_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            read_infra_env_boot(tmp_path, MCE, INFRA_ENV)

    def test_a_file_without_its_image_time_is_corrupt(self, tmp_path: Path):
        (tmp_path / MCE).mkdir()
        (tmp_path / MCE / f"{INFRA_ENV}.ipxe").write_text("#!ipxe\nchain http://as/a\n")
        with pytest.raises(MappingFormatError):
            read_infra_env_boot(tmp_path, MCE, INFRA_ENV)

    def test_a_corrupt_entry_is_not_silently_overwritten(self, tmp_path: Path):
        # The comparison cannot be made, so the write is refused (500) rather
        # than guessing the stored image is older.
        (tmp_path / MCE).mkdir()
        (tmp_path / MCE / f"{INFRA_ENV}.ipxe").write_text("garbage\n")
        with pytest.raises(MappingFormatError):
            write_infra_env_boot(tmp_path, MCE, INFRA_ENV, "http://as/a", T0)

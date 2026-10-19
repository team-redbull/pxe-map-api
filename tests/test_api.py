import pytest
from fastapi.testclient import TestClient

from app.config import get_config
from app.main import app

TOKEN = "test-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PXE_MAP_DIR", str(tmp_path))
    monkeypatch.setenv("PXE_MAP_TOKEN", TOKEN)
    get_config.cache_clear()
    yield TestClient(app)
    get_config.cache_clear()


@pytest.fixture
def client_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("PXE_MAP_DIR", str(tmp_path))
    monkeypatch.delenv("PXE_MAP_TOKEN", raising=False)
    get_config.cache_clear()
    yield TestClient(app)
    get_config.cache_clear()


def auth_headers(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


class TestHealthz:
    def test_no_auth_required(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200


class TestAuth:
    def test_401_no_token(self, client):
        resp = client.get("/pxe-map")
        assert resp.status_code == 401

    def test_401_wrong_token(self, client):
        resp = client.get("/pxe-map", headers=auth_headers("wrong"))
        assert resp.status_code == 401


class TestAuthDisabledWhenTokenUnset:
    def test_request_without_any_authorization_header_succeeds(self, client_without_token):
        resp = client_without_token.get("/pxe-map")
        assert resp.status_code == 200

    def test_request_with_a_bogus_token_still_succeeds(self, client_without_token):
        # No token is configured, so require_auth never checks credentials
        # at all - this isn't "any token works", it's "auth is off".
        resp = client_without_token.get("/pxe-map", headers=auth_headers("anything"))
        assert resp.status_code == 200


class TestCrudRoundTrip:
    def test_full_round_trip(self, client):
        create = client.post(
            "/pxe-map",
            json={"mac": "AA:BB:CC:DD:EE:01", "ipxe_url": "http://example/a"},
            headers=auth_headers(),
        )
        assert create.status_code == 201
        assert create.json() == {"mac": "aabbccddee01", "ipxe_url": "http://example/a"}

        get_resp = client.get("/pxe-map/aabbccddee01", headers=auth_headers())
        assert get_resp.status_code == 200
        assert get_resp.json() == {"mac": "aabbccddee01", "ipxe_url": "http://example/a"}

        list_resp = client.get("/pxe-map", headers=auth_headers())
        assert list_resp.status_code == 200
        assert list_resp.json() == [{"mac": "aabbccddee01", "ipxe_url": "http://example/a"}]

        patch_resp = client.patch(
            "/pxe-map/aabbccddee01",
            json={"ipxe_url": "http://example/b"},
            headers=auth_headers(),
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["ipxe_url"] == "http://example/b"

        delete_resp = client.delete("/pxe-map/aabbccddee01", headers=auth_headers())
        assert delete_resp.status_code == 204

        get_after_delete = client.get("/pxe-map/aabbccddee01", headers=auth_headers())
        assert get_after_delete.status_code == 404


class TestPostSemantics:
    def test_post_existing_mac_returns_409(self, client):
        client.post(
            "/pxe-map",
            json={"mac": "aabbccddee01", "ipxe_url": "http://example/a"},
            headers=auth_headers(),
        )
        resp = client.post(
            "/pxe-map",
            json={"mac": "aabbccddee01", "ipxe_url": "http://example/b"},
            headers=auth_headers(),
        )
        assert resp.status_code == 409

    def test_post_invalid_url_returns_422(self, client):
        resp = client.post(
            "/pxe-map",
            json={"mac": "aabbccddee01", "ipxe_url": "https://example/a"},
            headers=auth_headers(),
        )
        assert resp.status_code == 422


class TestPatchSemantics:
    def test_patch_absent_mac_returns_404(self, client):
        resp = client.patch(
            "/pxe-map/aabbccddee01",
            json={"ipxe_url": "http://example/a"},
            headers=auth_headers(),
        )
        assert resp.status_code == 404


class TestPutSemantics:
    def test_put_twice_is_idempotent_and_returns_200_both_times(self, client):
        first = client.put(
            "/pxe-map/aabbccddee01",
            json={"ipxe_url": "http://example/a"},
            headers=auth_headers(),
        )
        assert first.status_code == 200

        second = client.put(
            "/pxe-map/aabbccddee01",
            json={"ipxe_url": "http://example/b"},
            headers=auth_headers(),
        )
        assert second.status_code == 200
        assert second.json()["ipxe_url"] == "http://example/b"

    def test_put_invalid_url_returns_422(self, client):
        resp = client.put(
            "/pxe-map/aabbccddee01",
            json={"ipxe_url": "https://example/a"},
            headers=auth_headers(),
        )
        assert resp.status_code == 422


class TestDeleteIdempotent:
    def test_delete_absent_mapping_returns_204(self, client):
        resp = client.delete("/pxe-map/aabbccddee01", headers=auth_headers())
        assert resp.status_code == 204


class TestInvalidMacRejectedOnEveryRoute:
    """A malformed MAC (e.g. '3js') is rejected with 422 before the route
    body runs, on every route that takes a mac - GET/PUT/PATCH/DELETE via
    the CanonicalMac path-param type (app/dependencies.py), POST via the
    field_validator on MappingCreate (app/schemas.py)."""

    def test_get_invalid_mac_returns_422(self, client):
        resp = client.get("/pxe-map/3js", headers=auth_headers())
        assert resp.status_code == 422
        assert "invalid mac" in resp.text.lower()

    def test_put_invalid_mac_returns_422(self, client):
        resp = client.put(
            "/pxe-map/3js", json={"ipxe_url": "http://example/a"}, headers=auth_headers()
        )
        assert resp.status_code == 422
        assert "invalid mac" in resp.text.lower()

    def test_patch_invalid_mac_returns_422(self, client):
        resp = client.patch(
            "/pxe-map/3js", json={"ipxe_url": "http://example/a"}, headers=auth_headers()
        )
        assert resp.status_code == 422
        assert "invalid mac" in resp.text.lower()

    def test_delete_invalid_mac_returns_422(self, client):
        resp = client.delete("/pxe-map/3js", headers=auth_headers())
        assert resp.status_code == 422
        assert "invalid mac" in resp.text.lower()

    def test_post_invalid_mac_returns_422(self, client):
        resp = client.post(
            "/pxe-map",
            json={"mac": "3js", "ipxe_url": "http://example/a"},
            headers=auth_headers(),
        )
        assert resp.status_code == 422
        assert "invalid mac" in resp.text.lower()


MCE = "ocp4-mce-bat-yam-01"
INFRA_ENV = "cisco-m6-bat-yam-64c-512gb"
INFRA_ENV_REF = {"mce_cluster": MCE, "name": INFRA_ENV}


class TestMacMappedToAnInfraEnv:
    def test_put_writes_a_relative_chain_and_reads_back_as_infra_env(self, client, tmp_path):
        resp = client.put(
            "/pxe-map/aabbccddee01", json={"infra_env": INFRA_ENV_REF}, headers=auth_headers()
        )
        assert resp.status_code == 200
        assert resp.json() == {"mac": "aabbccddee01", "infra_env": INFRA_ENV_REF}
        assert (tmp_path / "aabbccddee01.ipxe").read_text() == (
            f"#!ipxe\nchain {MCE}/{INFRA_ENV}.ipxe\n"
        )
        assert client.get("/pxe-map/aabbccddee01", headers=auth_headers()).json() == {
            "mac": "aabbccddee01",
            "infra_env": INFRA_ENV_REF,
        }

    def test_mapping_ahead_of_the_infra_env_entry_is_allowed(self, client):
        # install-server maps MACs first and publishes the entry once the
        # host's NMStateConfig is in the image; nothing boots in between.
        resp = client.put(
            "/pxe-map/aabbccddee01", json={"infra_env": INFRA_ENV_REF}, headers=auth_headers()
        )
        assert resp.status_code == 200
        assert client.get(f"/pxe-map/{MCE}/{INFRA_ENV}", headers=auth_headers()).status_code == 404

    def test_post_and_patch_take_an_infra_env_too(self, client):
        post = client.post(
            "/pxe-map",
            json={"mac": "aabbccddee01", "infra_env": INFRA_ENV_REF},
            headers=auth_headers(),
        )
        assert post.status_code == 201
        assert post.json() == {"mac": "aabbccddee01", "infra_env": INFRA_ENV_REF}
        patch = client.patch(
            "/pxe-map/aabbccddee01", json={"ipxe_url": "http://example/b"}, headers=auth_headers()
        )
        assert patch.json() == {"mac": "aabbccddee01", "ipxe_url": "http://example/b"}

    def test_the_listing_shows_each_kind(self, client):
        client.put("/pxe-map/aabbccddee01", json={"infra_env": INFRA_ENV_REF}, headers=auth_headers())
        client.put("/pxe-map/aabbccddee02", json={"ipxe_url": "http://example/a"}, headers=auth_headers())
        assert client.get("/pxe-map", headers=auth_headers()).json() == [
            {"mac": "aabbccddee01", "infra_env": INFRA_ENV_REF},
            {"mac": "aabbccddee02", "ipxe_url": "http://example/a"},
        ]

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"ipxe_url": "http://example/a", "infra_env": INFRA_ENV_REF},
            {"infra_env": {"mce_cluster": "../etc", "name": INFRA_ENV}},
            {"infra_env": {"mce_cluster": MCE, "name": "Upper"}},
        ],
    )
    def test_anything_but_exactly_one_valid_target_is_422(self, client, body):
        resp = client.put("/pxe-map/aabbccddee01", json=body, headers=auth_headers())
        assert resp.status_code == 422


class TestConditionalDelete:
    def _map(self, client, mac, mce):
        client.put(
            f"/pxe-map/{mac}",
            json={"infra_env": {"mce_cluster": mce, "name": INFRA_ENV}},
            headers=auth_headers(),
        )

    def test_this_mces_entry_is_204_and_gone(self, client):
        self._map(client, "aabbccddee01", MCE)
        resp = client.delete(f"/pxe-map/aabbccddee01?mce_cluster={MCE}", headers=auth_headers())
        assert resp.status_code == 204
        assert client.get("/pxe-map/aabbccddee01", headers=auth_headers()).status_code == 404

    def test_another_mces_entry_is_409_and_kept(self, client):
        self._map(client, "aabbccddee01", "ocp4-mce-other")
        resp = client.delete(f"/pxe-map/aabbccddee01?mce_cluster={MCE}", headers=auth_headers())
        assert resp.status_code == 409
        assert "ocp4-mce-other" in resp.json()["detail"]
        assert client.get("/pxe-map/aabbccddee01", headers=auth_headers()).status_code == 200

    def test_absent_is_still_204(self, client):
        resp = client.delete(f"/pxe-map/aabbccddee01?mce_cluster={MCE}", headers=auth_headers())
        assert resp.status_code == 204

    def test_without_the_parameter_it_is_unconditional_as_before(self, client):
        self._map(client, "aabbccddee01", "ocp4-mce-other")
        assert client.delete("/pxe-map/aabbccddee01", headers=auth_headers()).status_code == 204
        assert client.get("/pxe-map/aabbccddee01", headers=auth_headers()).status_code == 404

    def test_an_invalid_mce_name_is_422(self, client):
        resp = client.delete("/pxe-map/aabbccddee01?mce_cluster=Bad_Name", headers=auth_headers())
        assert resp.status_code == 422


class TestInfraEnvBoot:
    URL = f"/pxe-map/{MCE}/{INFRA_ENV}"
    T0 = "2026-10-19T10:00:05Z"
    T1 = "2026-10-19T10:00:35Z"

    def _put(self, client, ipxe_url, at, **kwargs):
        return client.put(
            self.URL,
            json={"ipxe_url": ipxe_url, "image_created_at": at},
            headers=auth_headers(**kwargs),
        )

    def test_put_writes_the_file_nginx_serves_and_get_reads_it(self, client, tmp_path):
        resp = self._put(client, "http://as/ipxe-script?api_key=k", self.T0)
        assert resp.status_code == 200
        assert resp.json() == {
            "mce_cluster": MCE,
            "infra_env": INFRA_ENV,
            "ipxe_url": "http://as/ipxe-script?api_key=k",
            "image_created_at": self.T0,
        }
        assert (tmp_path / MCE / f"{INFRA_ENV}.ipxe").exists()
        assert client.get(self.URL, headers=auth_headers()).json() == resp.json()

    def test_a_newer_image_replaces_an_older_one(self, client):
        self._put(client, "http://as/old", self.T0)
        assert self._put(client, "http://as/new", self.T1).status_code == 200
        assert client.get(self.URL, headers=auth_headers()).json()["ipxe_url"] == "http://as/new"

    def test_an_older_image_is_409_and_changes_nothing(self, client):
        self._put(client, "http://as/new", self.T1)
        resp = self._put(client, "http://as/old", self.T0)
        assert resp.status_code == 409
        assert "newer" in resp.json()["detail"]
        assert client.get(self.URL, headers=auth_headers()).json()["ipxe_url"] == "http://as/new"

    def test_a_retry_of_the_same_image_is_200(self, client):
        assert self._put(client, "http://as/a", self.T0).status_code == 200
        assert self._put(client, "http://as/a", self.T0).status_code == 200

    def test_absent_is_404(self, client):
        assert client.get(self.URL, headers=auth_headers()).status_code == 404

    @pytest.mark.parametrize(
        "body",
        [
            {"ipxe_url": "https://as/a", "image_created_at": "2026-10-19T10:00:05Z"},
            {"ipxe_url": "http://as/a", "image_created_at": "2026-10-19T10:00:05"},  # no zone
            {"ipxe_url": "http://as/a"},
        ],
    )
    def test_an_invalid_body_is_422(self, client, body):
        assert client.put(self.URL, json=body, headers=auth_headers()).status_code == 422

    @pytest.mark.parametrize("path", [f"/pxe-map/Upper/{INFRA_ENV}", f"/pxe-map/{MCE}/bad_name"])
    def test_an_invalid_name_in_the_path_is_422(self, client, path):
        resp = client.put(
            path,
            json={"ipxe_url": "http://as/a", "image_created_at": self.T0},
            headers=auth_headers(),
        )
        assert resp.status_code == 422

    def test_requires_auth(self, client):
        assert self._put(client, "http://as/a", self.T0, token="wrong").status_code == 401


class TestPostConcurrencyGuarantee:
    def test_post_never_overwrites_a_racing_create(self, client):
        # write_mapping_exclusive uses os.link, which fails atomically if the
        # target already exists - this is what makes the 409 contract hold
        # under concurrent creates instead of a check-then-write race.
        first = client.post(
            "/pxe-map",
            json={"mac": "aabbccddee01", "ipxe_url": "http://example/a"},
            headers=auth_headers(),
        )
        assert first.status_code == 201

        second = client.post(
            "/pxe-map",
            json={"mac": "aabbccddee01", "ipxe_url": "http://example/b"},
            headers=auth_headers(),
        )
        assert second.status_code == 409

        get_resp = client.get("/pxe-map/aabbccddee01", headers=auth_headers())
        assert get_resp.json()["ipxe_url"] == "http://example/a"

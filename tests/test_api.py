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

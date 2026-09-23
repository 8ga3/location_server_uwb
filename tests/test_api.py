"""HTTP API のテスト。応答形、ETag、認証を確認する。"""

from __future__ import annotations

from fastapi.testclient import TestClient

ANCHOR_BODY = {"label": "北西の柱", "x": 0.0, "y": 0.0, "z": 1.8}


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_config_initial_shape(client: TestClient) -> None:
    response = client.get("/api/v1/config", params={"tag_id": 1})
    assert response.status_code == 200
    payload = response.json()
    assert payload["rev"] == 1
    assert payload["pan_id"] == "0xDECA"
    assert payload["bias_mm"] == 0
    assert payload["anchors"] == []
    assert payload["telemetry"] == {"host": "", "port": 0, "batch_cycles": 4}
    assert response.headers["etag"] == '"rev-1"'
    # ESP32 側のパーサを単純に保つため Content-Length を必ず付ける
    assert "content-length" in response.headers
    assert "transfer-encoding" not in response.headers


def test_config_rejects_out_of_range_tag_id(client: TestClient) -> None:
    assert client.get("/api/v1/config", params={"tag_id": 0}).status_code == 422
    assert client.get("/api/v1/config", params={"tag_id": 256}).status_code == 422


def test_config_returns_304_for_matching_etag(client: TestClient) -> None:
    etag = client.get("/api/v1/config").headers["etag"]
    response = client.get("/api/v1/config", headers={"If-None-Match": etag})
    assert response.status_code == 304
    assert response.headers["etag"] == etag
    assert response.content == b""


def test_config_returns_200_after_revision_changes(client: TestClient) -> None:
    etag = client.get("/api/v1/config").headers["etag"]
    client.put("/api/v1/anchors/0x0100", json=ANCHOR_BODY)
    response = client.get("/api/v1/config", headers={"If-None-Match": etag})
    assert response.status_code == 200
    assert response.headers["etag"] == '"rev-2"'


def test_config_handles_weak_and_multiple_etags(client: TestClient) -> None:
    response = client.get("/api/v1/config", headers={"If-None-Match": 'W/"rev-1", "rev-0"'})
    assert response.status_code == 304


def test_put_anchor_creates_then_updates(client: TestClient) -> None:
    created = client.put("/api/v1/anchors/0x0100", json=ANCHOR_BODY)
    assert created.status_code == 201
    assert created.json() == {
        "rev": 2,
        "anchor": {
            "id": "0x0100",
            "label": "北西の柱",
            "x": 0.0,
            "y": 0.0,
            "z": 1.8,
            "enabled": True,
            "source": "manual",
            "updated_at": created.json()["anchor"]["updated_at"],
        },
    }

    updated = client.put("/api/v1/anchors/0x0100", json={**ANCHOR_BODY, "x": 5.12})
    assert updated.status_code == 200
    assert updated.json()["rev"] == 3
    assert updated.json()["anchor"]["x"] == 5.12


def test_put_anchor_accepts_decimal_id(client: TestClient) -> None:
    response = client.put("/api/v1/anchors/256", json=ANCHOR_BODY)
    assert response.status_code == 201
    assert response.json()["anchor"]["id"] == "0x0100"


def test_put_anchor_rejects_out_of_range_id(client: TestClient) -> None:
    assert client.put("/api/v1/anchors/0x00FF", json=ANCHOR_BODY).status_code == 400
    assert client.put("/api/v1/anchors/0xFFFF", json=ANCHOR_BODY).status_code == 400
    assert client.put("/api/v1/anchors/beef", json=ANCHOR_BODY).status_code == 400


def test_put_anchor_rejects_unknown_field(client: TestClient) -> None:
    response = client.put("/api/v1/anchors/0x0100", json={**ANCHOR_BODY, "x_mm": 1})
    assert response.status_code == 422


def test_put_anchor_rejects_out_of_range_coordinate(client: TestClient) -> None:
    response = client.put("/api/v1/anchors/0x0100", json={**ANCHOR_BODY, "x": 1e9})
    assert response.status_code == 422


def test_anchor_coordinates_are_stored_as_millimeters(client: TestClient) -> None:
    client.put("/api/v1/anchors/0x0102", json={"x": 5.0805, "y": 4.3101, "z": 1.8})
    anchor = client.get("/api/v1/anchors").json()["anchors"][0]
    # 0.5 mm 相当は切り上げ、それ未満は切り捨てになる
    assert anchor["x"] == 5.081
    assert anchor["y"] == 4.31


def test_list_anchors_includes_disabled(client: TestClient) -> None:
    client.put("/api/v1/anchors/0x0100", json=ANCHOR_BODY)
    client.put("/api/v1/anchors/0x0101", json={**ANCHOR_BODY, "x": 5.12, "enabled": False})

    listed = client.get("/api/v1/anchors").json()
    assert [a["id"] for a in listed["anchors"]] == ["0x0100", "0x0101"]
    assert listed["rev"] == 3

    config = client.get("/api/v1/config").json()
    assert [a["id"] for a in config["anchors"]] == ["0x0100"]


def test_put_telemetry(client: TestClient) -> None:
    response = client.put(
        "/api/v1/config/telemetry",
        json={"host": "192.168.1.10", "port": 47100, "batch_cycles": 4},
    )
    assert response.status_code == 200
    assert response.json()["telemetry"] == {"host": "192.168.1.10", "port": 47100, "batch_cycles": 4}
    assert response.json()["rev"] == 2


def test_put_telemetry_rejects_hostname(client: TestClient) -> None:
    body = {"host": "uwb-server.local", "port": 47100, "batch_cycles": 4}
    assert client.put("/api/v1/config/telemetry", json=body).status_code == 422


def test_put_telemetry_requires_host_when_port_is_set(client: TestClient) -> None:
    body = {"host": "", "port": 47100, "batch_cycles": 4}
    assert client.put("/api/v1/config/telemetry", json=body).status_code == 422


def test_put_telemetry_allows_stopping_transmission(client: TestClient) -> None:
    body = {"host": "", "port": 0, "batch_cycles": 4}
    assert client.put("/api/v1/config/telemetry", json=body).status_code == 200


def test_config_revision_is_reproducible_over_http(client: TestClient) -> None:
    client.put("/api/v1/anchors/0x0100", json=ANCHOR_BODY)
    rev = client.put("/api/v1/anchors/0x0101", json={**ANCHOR_BODY, "x": 5.12}).json()["rev"]
    client.put("/api/v1/anchors/0x0100", json={**ANCHOR_BODY, "x": 0.5})

    past = client.get(f"/api/v1/config/revisions/{rev}")
    assert past.status_code == 200
    assert [(a["id"], a["x"]) for a in past.json()["anchors"]] == [("0x0100", 0.0), ("0x0101", 5.12)]

    assert client.get("/api/v1/config/revisions/999").status_code == 404


def test_write_requires_token_when_configured(token_client: TestClient) -> None:
    assert token_client.get("/api/v1/config").status_code == 200
    assert token_client.get("/api/v1/anchors").status_code == 200

    assert token_client.put("/api/v1/anchors/0x0100", json=ANCHOR_BODY).status_code == 401
    wrong = token_client.put("/api/v1/anchors/0x0100", json=ANCHOR_BODY, headers={"X-Auth-Token": "nope"})
    assert wrong.status_code == 401

    ok = token_client.put("/api/v1/anchors/0x0100", json=ANCHOR_BODY, headers={"X-Auth-Token": "s3cret"})
    assert ok.status_code == 201

    telemetry = token_client.put(
        "/api/v1/config/telemetry",
        json={"host": "192.168.1.10", "port": 47100, "batch_cycles": 4},
    )
    assert telemetry.status_code == 401

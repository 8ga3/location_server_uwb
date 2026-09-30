"""参照 API とセッション一覧のテスト。応答形、間引き、集計、エラーを確認する。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from location_server.ingest.packet import CycleRecord, RangeRecord, TelemetryPacket
from location_server.store import QueryStore, ReceivedPacket, TelemetryStore
from location_server.store.query import stride_for
from telemetry_helpers import ANCHORS, make_packet

RECV_AT = "2026-09-30T00:00:00+00:00"


def _failed_cycle(seq: int, t_tag_ms: int) -> CycleRecord:
    """測位に失敗し、0x0101 の測距も失敗したサイクル。"""
    return CycleRecord(
        seq=seq,
        t_tag_ms=t_tag_ms,
        fix_flags=0,
        used_count=2,
        x_mm=0,
        y_mm=0,
        z_mm=0,
        residual_mm=0,
        ranges=tuple(RangeRecord(a, 3 if a == 0x0101 else 0, 9, 0 if a == 0x0101 else 2000) for a in ANCHORS),
    )


def _populate(store: TelemetryStore) -> int:
    """seq 0..7 は成功、8..11 は欠番、12..15 のうち 12 と 13 は測位失敗のセッションを書く。"""
    good = make_packet(seq=0, t_tag_ms=1000, count=8, period_ms=100)
    tail = make_packet(seq=12, t_tag_ms=2200, count=4, period_ms=100)
    tail = TelemetryPacket(
        flags=0,
        tag_id=tail.tag_id,
        boot_id=tail.boot_id,
        seq=12,
        t_tag_ms=2200,
        anchor_n=tail.anchor_n,
        cycles=(_failed_cycle(12, 2200), _failed_cycle(13, 2300), *tail.cycles[2:]),
    )
    result = store.write_packets([ReceivedPacket(good, RECV_AT), ReceivedPacket(tail, RECV_AT)])
    return result.sessions[(1, 0xAAAAAAAA)]


@pytest.fixture
def populated(client: TestClient) -> int:
    telemetry: TelemetryStore = client.app.state.telemetry_store  # type: ignore[attr-defined]
    return _populate(telemetry)


def test_stride_for() -> None:
    assert stride_for(0, 10) == 1
    assert stride_for(10, 10) == 1
    # 末尾の行を足しても max_points に収まる間隔にする
    assert stride_for(11, 10) == 2
    assert stride_for(100, 7) == 17
    assert stride_for(100, 2) == 99
    with pytest.raises(ValueError):
        stride_for(10, 1)


def test_session_list_reports_rates(client: TestClient, populated: int) -> None:
    response = client.get("/api/v1/sessions")
    assert response.status_code == 200
    [session] = response.json()["sessions"]
    assert session["id"] == populated
    assert session["tag_id"] == 1
    assert session["boot_id"] == 0xAAAAAAAA
    assert session["cycles"] == 12
    assert session["fix_ok_cycles"] == 10
    assert session["missing_cycles"] == 4
    assert session["fix_rate"] == pytest.approx(10 / 12)
    assert session["loss_rate"] == pytest.approx(4 / 16)
    assert (session["first_t_ms"], session["last_t_ms"]) == (1000, 2500)


def test_session_list_includes_sessions_without_data(client: TestClient) -> None:
    client.post("/api/v1/hello", json={"tag_id": 3, "boot_id": 1})
    [session] = client.get("/api/v1/sessions").json()["sessions"]
    assert session["cycles"] == 0
    assert session["fix_rate"] is None
    assert session["loss_rate"] is None


def test_session_list_is_newest_first_and_limited(client: TestClient) -> None:
    for boot in range(3):
        client.post("/api/v1/hello", json={"tag_id": 1, "boot_id": boot})
    ids = [s["id"] for s in client.get("/api/v1/sessions", params={"limit": 2}).json()["sessions"]]
    assert ids == [3, 2]
    assert client.get("/api/v1/sessions", params={"limit": 0}).status_code == 422


def test_track_returns_columns(client: TestClient, populated: int) -> None:
    response = client.get(f"/api/v1/sessions/{populated}/track")
    assert response.status_code == 200
    payload = response.json()
    assert payload["decimation"] == {"total": 12, "stride": 1}
    fix = payload["fix"]
    assert fix["seq"] == [0, 1, 2, 3, 4, 5, 6, 7, 12, 13, 14, 15]
    assert fix["t"][:2] == [1000, 1100]
    # 欠番をまたぐ seq 12 と先頭の周期は null
    assert fix["dt"] == [None, 100, 100, 100, 100, 100, 100, 100, None, 100, 100, 100]
    assert fix["ok"][7:10] == [True, False, False]
    assert fix["x"][7:10] == [1.234, None, None]
    assert fix["used"][8] == 2
    assert fix["resid"][0] == 0.042
    assert fix["z"][0] == 1.0


def test_track_time_range_and_decimation(client: TestClient, populated: int) -> None:
    payload = client.get(
        f"/api/v1/sessions/{populated}/track", params={"from_ms": 1100, "to_ms": 2300, "max_points": 3}
    ).json()
    # 1100..1700 の 7 件と 2200, 2300 の 2 件を 3 件以下へ間引く。末尾の行は必ず含める
    assert payload["decimation"] == {"total": 9, "stride": 4}
    assert payload["fix"]["t"] == [1100, 1500, 2300]
    # 周期は間引く前の隣のサイクルから求めるので、間引いても 400 にはならない
    assert payload["fix"]["dt"] == [100, 100, 100]


def test_track_empty_range(client: TestClient, populated: int) -> None:
    payload = client.get(f"/api/v1/sessions/{populated}/track", params={"from_ms": 5000}).json()
    assert payload["decimation"] == {"total": 0, "stride": 1}
    assert payload["fix"]["t"] == []


def test_track_rejects_bad_parameters(client: TestClient, populated: int) -> None:
    url = f"/api/v1/sessions/{populated}/track"
    assert client.get(url, params={"from_ms": 10, "to_ms": 5}).status_code == 400
    assert client.get(url, params={"max_points": 1}).status_code == 422
    assert client.get(url, params={"from_ms": -1}).status_code == 422
    assert client.get("/api/v1/sessions/999/track").status_code == 404


def test_ranges_all_anchors(client: TestClient, populated: int) -> None:
    payload = client.get(f"/api/v1/sessions/{populated}/ranges").json()
    assert list(payload["ranges"]) == ["0x0100", "0x0101", "0x0102", "0x0103"]
    a1 = payload["ranges"]["0x0101"]
    assert len(a1["t"]) == 12
    assert a1["d"][8:10] == [None, None]
    assert a1["st"][8:10] == [3, 3]
    assert a1["d"][0] == 1.1
    assert payload["decimation"]["0x0101"] == {"total": 12, "stride": 1}


def test_ranges_single_anchor_with_decimation(client: TestClient, populated: int) -> None:
    payload = client.get(
        f"/api/v1/sessions/{populated}/ranges", params={"anchor_id": "0x0102", "max_points": 5}
    ).json()
    assert list(payload["ranges"]) == ["0x0102"]
    assert payload["decimation"]["0x0102"] == {"total": 12, "stride": 3}
    assert payload["ranges"]["0x0102"]["seq"] == [0, 3, 6, 13, 15]
    # 10 進表記の ID も受け付ける
    same = client.get(f"/api/v1/sessions/{populated}/ranges", params={"anchor_id": str(0x0102)}).json()
    assert list(same["ranges"]) == ["0x0102"]


def test_ranges_rejects_bad_anchor(client: TestClient, populated: int) -> None:
    url = f"/api/v1/sessions/{populated}/ranges"
    assert client.get(url, params={"anchor_id": "0x0001"}).status_code == 400
    assert client.get(url, params={"anchor_id": "abc"}).status_code == 400


def test_summary(client: TestClient, populated: int) -> None:
    client.put("/api/v1/anchors/0x0100", json={"label": "北西", "x": 0, "y": 0, "z": 1.8})
    response = client.get(f"/api/v1/sessions/{populated}/summary")
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    assert payload["session"]["missing_cycles"] == 4
    # 残差は成功したサイクルだけで RMS を取る
    assert payload["residual_rms"] == pytest.approx(0.042)
    # 欠番をまたぐ差 (seq 7 -> 12) は周期に含めない
    assert payload["period_mean_ms"] == pytest.approx(100)
    assert payload["period_max_ms"] == 100
    assert (payload["used_min"], payload["used_max"]) == (2, 4)
    by_id = {entry["id"]: entry for entry in payload["ranges"]}
    a1 = by_id["0x0101"]
    assert (a1["samples"], a1["ok_samples"]) == (12, 10)
    assert a1["success_rate"] == pytest.approx(10 / 12)
    assert a1["status_counts"] == {"0": 10, "3": 2}
    assert a1["elapsed_max_ms"] == 9
    assert a1["elapsed_mean_ms"] == pytest.approx((10 * 6 + 2 * 9) / 12)
    assert a1["distance_mean"] == pytest.approx(1.1)
    assert by_id["0x0100"]["distance_mean"] == pytest.approx((10 * 1.0 + 2 * 2.0) / 12)
    # hello が無いセッションは現在の構成の座標表を返す
    assert payload["anchors_rev"] == 2
    assert payload["anchors"] == [{"id": "0x0100", "label": "北西", "x": 0.0, "y": 0.0, "z": 1.8}]


def test_summary_uses_session_config_revision(client: TestClient, populated: int) -> None:
    client.put("/api/v1/anchors/0x0100", json={"x": 0, "y": 0, "z": 1.8})
    client.post("/api/v1/hello", json={"tag_id": 1, "boot_id": 0xAAAAAAAA, "config_rev": 2})
    client.put("/api/v1/anchors/0x0100", json={"x": 1, "y": 0, "z": 1.8})
    payload = client.get(f"/api/v1/sessions/{populated}/summary").json()
    assert payload["anchors_rev"] == 2
    assert payload["anchors"][0]["x"] == 0.0


def test_summary_unknown_session(client: TestClient) -> None:
    assert client.get("/api/v1/sessions/1/summary").status_code == 404


def test_query_store_summary_without_data(client: TestClient) -> None:
    session_id = client.post("/api/v1/hello", json={"tag_id": 1, "boot_id": 5}).json()["session_id"]
    query: QueryStore = client.app.state.query_store  # type: ignore[attr-defined]
    summary = query.summary(session_id)
    assert summary is not None
    assert summary.residual_rms_mm is None
    assert summary.period_mean_ms is None
    assert summary.anchors == ()
    assert summary.session.cycles == 0


def _write(store: TelemetryStore, *packets: TelemetryPacket) -> int:
    result = store.write_packets([ReceivedPacket(p, RECV_AT) for p in packets])
    return result.sessions[(packets[0].tag_id, packets[0].boot_id)]


def test_millis_wrap_is_unrolled(client: TestClient) -> None:
    """millis() が 32 ビットで折り返しても、時刻順・周期・範囲指定が崩れない。"""
    telemetry: TelemetryStore = client.app.state.telemetry_store  # type: ignore[attr-defined]
    start = 0xFFFFFFFF - 150  # 4 サイクル目で折り返す
    session_id = _write(telemetry, make_packet(seq=0, t_tag_ms=start, count=4, period_ms=100))
    fix = client.get(f"/api/v1/sessions/{session_id}/track").json()["fix"]
    assert fix["seq"] == [0, 1, 2, 3]
    assert fix["t"] == [start, start + 100, start + 200, start + 300]
    assert fix["dt"] == [None, 100, 100, 100]
    # 展開した時刻で範囲を指定する
    part = client.get(f"/api/v1/sessions/{session_id}/track", params={"from_ms": 2**32}).json()["fix"]
    assert part["seq"] == [2, 3]
    ranges = client.get(f"/api/v1/sessions/{session_id}/ranges").json()["ranges"]
    assert ranges["0x0100"]["t"] == fix["t"]
    summary = client.get(f"/api/v1/sessions/{session_id}/summary").json()
    assert summary["period_mean_ms"] == pytest.approx(100)
    assert (summary["session"]["first_t_ms"], summary["session"]["last_t_ms"]) == (start, start + 300)


def test_seq_wrap_is_unrolled(client: TestClient) -> None:
    telemetry: TelemetryStore = client.app.state.telemetry_store  # type: ignore[attr-defined]
    session_id = _write(telemetry, make_packet(seq=0xFFFFFFFE, t_tag_ms=1000, count=4, period_ms=100))
    fix = client.get(f"/api/v1/sessions/{session_id}/track").json()["fix"]
    assert fix["seq"] == [0xFFFFFFFE, 0xFFFFFFFF, 2**32, 2**32 + 1]
    assert fix["t"] == [1000, 1100, 1200, 1300]
    assert fix["dt"] == [None, 100, 100, 100]
    [session] = client.get("/api/v1/sessions").json()["sessions"]
    assert (session["cycles"], session["missing_cycles"]) == (4, 0)
    assert client.get(f"/api/v1/sessions/{session_id}/summary").json()["period_max_ms"] == 100


def test_seq_wrap_boundary_is_half_range(client: TestClient) -> None:
    """seq の最小値と最大値の差がちょうど 2^31 なら折り返したとみなす (設計文書 5.5)。"""
    telemetry: TelemetryStore = client.app.state.telemetry_store  # type: ignore[attr-defined]
    session_id = _write(
        telemetry,
        make_packet(seq=2**31, t_tag_ms=1000, count=1),
        make_packet(seq=0, t_tag_ms=2000, count=1),
    )
    fix = client.get(f"/api/v1/sessions/{session_id}/track").json()["fix"]
    assert fix["seq"] == [2**31, 2**32]


def test_anchor_first_seen_after_wrap_uses_session_unwrap(client: TestClient) -> None:
    """折り返した後に初めて現れたアンカーの測距も、同じサイクルの測位と同じ展開値になる。"""
    telemetry: TelemetryStore = client.app.state.telemetry_store  # type: ignore[attr-defined]
    start = 0xFFFFFFFF - 50
    session_id = _write(
        telemetry,
        make_packet(seq=0xFFFFFFFE, t_tag_ms=start, count=2, period_ms=50),
        make_packet(seq=0, t_tag_ms=start + 100 - 2**32, count=2, period_ms=50, anchors=(*ANCHORS, 0x0104)),
    )
    fix = client.get(f"/api/v1/sessions/{session_id}/track").json()["fix"]
    ranges = client.get(f"/api/v1/sessions/{session_id}/ranges").json()["ranges"]
    assert ranges["0x0104"]["seq"] == fix["seq"][2:] == [2**32, 2**32 + 1]
    assert ranges["0x0104"]["t"] == fix["t"][2:] == [start + 100, start + 150]
    assert ranges["0x0100"]["seq"] == fix["seq"]

"""ライブ配信の WebSocket のテスト。購読、切り替え、誤った要求、UDP からブラウザまでの結合を確認する。"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from location_server.api import create_app
from location_server.api.routes_live import _send_frames
from location_server.ingest.packet import encode_packet
from location_server.live import LiveHub
from location_server.live.hub import END_TIMEOUT
from location_server.settings import Settings
from telemetry_helpers import make_packet

ANCHOR_BODY = {"label": "北西の柱", "x": 0.0, "y": 0.0, "z": 1.8}


def _hub(client: TestClient) -> LiveHub:
    hub: LiveHub = client.app.state.live  # type: ignore[attr-defined]
    return hub


def _publish(client: TestClient, **kwargs: Any) -> None:
    """ハブはイベントループのスレッドから呼ぶ前提なので、テストからもループ上で呼ぶ。"""
    assert client.portal is not None
    client.portal.call(_hub(client).publish, make_packet(**kwargs))


def _receive_until(ws: WebSocketTestSession, kind: str) -> dict[str, Any]:
    while True:
        frame: dict[str, Any] = ws.receive_json()
        if frame["type"] == kind:
            return frame


def test_subscribe_receives_snapshot_with_anchors(client: TestClient) -> None:
    client.put("/api/v1/anchors/0x0100", json=ANCHOR_BODY)
    with client.websocket_connect("/api/v1/ws/live") as ws:
        ws.send_json({"op": "subscribe", "tag_id": 1})
        snapshot = ws.receive_json()
    assert snapshot["type"] == "snapshot"
    assert snapshot["tag_id"] == 1
    assert snapshot["session_id"] is None
    assert snapshot["history_ms"] == 30000
    assert snapshot["anchors_rev"] == 2
    assert snapshot["anchors"] == [{"id": "0x0100", "label": "北西の柱", "x": 0.0, "y": 0.0, "z": 1.8}]


def test_live_session_flow(client: TestClient) -> None:
    client.put("/api/v1/anchors/0x0100", json=ANCHOR_BODY)
    client.put("/api/v1/anchors/0x0100", json={**ANCHOR_BODY, "x": 1.0})
    session_id = client.post(
        "/api/v1/hello", json={"tag_id": 1, "boot_id": 0xAAAAAAAA, "config_rev": 2}
    ).json()["session_id"]
    with client.websocket_connect("/api/v1/ws/live") as ws:
        ws.send_json({"op": "subscribe", "tag_id": 1, "history_ms": 1000})
        assert ws.receive_json()["type"] == "snapshot"
        _publish(client, seq=0)
        start = ws.receive_json()
        assert start["type"] == "session_start"
        assert (start["session_id"], start["config_rev"]) == (session_id, 2)
        # セッションが使った構成リビジョン (x = 0.0) の座標を返す。現在の rev 3 ではない
        assert start["anchors_rev"] == 2
        assert start["anchors"][0]["x"] == 0.0
        append = _receive_until(ws, "append")
        assert append["session_id"] == session_id
        assert append["fix"]["seq"] == [0, 1, 2, 3]
        assert append["lost"] == 0


def test_resubscribe_switches_tag(client: TestClient) -> None:
    with client.websocket_connect("/api/v1/ws/live") as ws:
        ws.send_json({"op": "subscribe", "tag_id": 1})
        ws.receive_json()
        ws.send_json({"op": "subscribe", "tag_id": 2})
        assert ws.receive_json()["tag_id"] == 2
        _publish(client, tag_id=1, seq=0)
        _publish(client, tag_id=2, seq=100)
        start = ws.receive_json()
        assert (start["type"], start["tag_id"]) == ("session_start", 2)
        assert _receive_until(ws, "append")["fix"]["seq"][0] == 100


def test_unsubscribe_stops_frames(client: TestClient) -> None:
    with client.websocket_connect("/api/v1/ws/live") as ws:
        ws.send_json({"op": "subscribe", "tag_id": 1})
        ws.receive_json()
        ws.send_json({"op": "unsubscribe"})
        # 購読解除の処理が終わってから送る。誤った要求への応答で順序を確かめる
        ws.send_json({"op": "nope"})
        assert ws.receive_json()["type"] == "error"
        _publish(client, seq=0)
        ws.send_json({"op": "nope"})
        assert ws.receive_json()["type"] == "error"
    assert _hub(client).subscriber_count == 0


def test_invalid_requests_return_errors_and_keep_connection(client: TestClient) -> None:
    with client.websocket_connect("/api/v1/ws/live") as ws:
        for bad in (
            "not json",
            '{"op": "subscribe"}',
            '{"op": "subscribe", "tag_id": 0}',
            '{"op": "subscribe", "tag_id": 1, "history_ms": 60001}',
            '{"op": "x"}',
        ):
            ws.send_text(bad)
            frame = ws.receive_json()
            assert frame["type"] == "error", bad
            assert frame["detail"]
        ws.send_bytes(b"\x00")
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"op": "subscribe", "tag_id": 1})
        assert ws.receive_json()["type"] == "snapshot"


def test_udp_to_websocket(db_path: Path) -> None:
    settings = Settings(db_path=db_path, host="127.0.0.1", port=0, auth_token=None, udp_port=0)
    with TestClient(create_app(settings)) as client:
        port = client.app.state.ingest.local_port  # type: ignore[attr-defined]
        with client.websocket_connect("/api/v1/ws/live") as ws:
            ws.send_json({"op": "subscribe", "tag_id": 1})
            ws.receive_json()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(encode_packet(make_packet(seq=0)), ("127.0.0.1", port))
                assert ws.receive_json()["type"] == "session_start"
                first = _receive_until(ws, "append")
                assert first["fix"]["seq"] == [0, 1, 2, 3]
                # 保存系のコミット後に届くパケットの append には DB のセッション ID が載る
                for seq in range(4, 400, 4):
                    sock.sendto(encode_packet(make_packet(seq=seq)), ("127.0.0.1", port))
                    append = _receive_until(ws, "append")
                    if append["session_id"] is not None:
                        break
                assert append["session_id"] == 1


def test_control_frame_flood_closes_connection(client: TestClient) -> None:
    """捨てられない制御フレームだけでキューが溢れた接続は 1013 で閉じる。ページは再接続して取り直す。"""
    hub = _hub(client)
    assert client.portal is not None
    with client.websocket_connect("/api/v1/ws/live") as ws:
        ws.send_json({"op": "subscribe", "tag_id": 1})
        assert ws.receive_json()["type"] == "snapshot"

        def flood() -> None:
            for boot in range(1, 100):
                hub.publish(make_packet(boot_id=boot, seq=0, count=1))

        client.portal.call(flood)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            while True:
                ws.receive_json()
    assert excinfo.value.code == 1013
    assert hub.stats.overflow_disconnects == 1


def test_lost_is_sent_on_control_frames_when_all_appends_are_dropped() -> None:
    """キューの append がすべて捨てられても、残った制御フレームで lost が届く。"""

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send_text(self, text: str) -> None:
            self.sent.append(json.loads(text))

    async def scenario() -> list[dict[str, Any]]:
        hub = LiveHub(subscriber_max_frames=2)
        sub = hub.connect()
        hub.subscribe(sub, 1)
        sub.pop_nowait()  # snapshot は送ったものとする
        hub.publish(make_packet(seq=0))
        for i in range(1, 5):
            hub.publish(make_packet(seq=i * 4))
            hub.tick()
        sub.push(
            {"type": "session_end", "session_id": None, "tag_id": 1, "boot_id": 1, "reason": END_TIMEOUT}
        )
        ws = FakeWebSocket()
        task = asyncio.create_task(_send_frames(ws, sub, store=None))  # type: ignore[arg-type]
        await asyncio.sleep(0.01)
        task.cancel()
        return ws.sent

    sent = asyncio.run(scenario())
    assert [f["type"] for f in sent] == ["session_start", "session_end"]
    assert sent[-1]["lost"] == 4

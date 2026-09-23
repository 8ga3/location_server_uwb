"""セキュリティレビューで見つかった不具合の回帰テスト。

いずれも未認証のクライアントから到達できる未処理例外で、意図した 401 / 400 ではなく
500 を返していた。HTTP テストクライアントは不正なヘッダの送信を拒否するため、
ヘッダ側は ASGI アプリを直接駆動して検証する。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from location_server.units import ValueRangeError, parse_hex_id

ANCHOR_BODY = {"x": 0.0, "y": 0.0, "z": 1.8}


def _call_asgi(app: Any, headers: list[tuple[bytes, bytes]], body: bytes) -> dict[str, Any]:
    """生のヘッダバイト列を指定して ASGI アプリを 1 回だけ呼ぶ。"""
    messages: list[dict[str, Any]] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": "/api/v1/anchors/0x0100",
        "raw_path": b"/api/v1/anchors/0x0100",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    return next(m for m in messages if m["type"] == "http.response.start")


def test_non_ascii_token_header_returns_401(token_client: TestClient) -> None:
    """非 ASCII を含むトークンヘッダで 500 にならず 401 を返す。

    Starlette はヘッダを latin-1 で復号するため、0x80..0xFF のバイトを 1 つ送るだけで
    `secrets.compare_digest()` が `TypeError` を送出していた。
    """
    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"content-length", b"31"),
        (b"x-auth-token", b"n\xf6pe"),
    ]
    start = _call_asgi(token_client.app, headers, b'{"x": 0.0, "y": 0.0, "z": 1.8}')
    assert start["status"] == 401


def test_ascii_wrong_token_still_returns_401(token_client: TestClient) -> None:
    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"content-length", b"31"),
        (b"x-auth-token", b"nope"),
    ]
    start = _call_asgi(token_client.app, headers, b'{"x": 0.0, "y": 0.0, "z": 1.8}')
    assert start["status"] == 401


def test_correct_token_still_accepted(token_client: TestClient) -> None:
    response = token_client.put(
        "/api/v1/anchors/0x0100", json=ANCHOR_BODY, headers={"X-Auth-Token": "s3cret"}
    )
    assert response.status_code == 201


@pytest.mark.parametrize("digits", [17, 100, 4400])
def test_overlong_decimal_id_returns_400(client: TestClient, digits: int) -> None:
    """桁数の多い 10 進 ID で 500 にならず 400 を返す。

    CPython の `int_max_str_digits` (既定 4300) を超える数字列は `int()` が
    `ValueError` を送出する。これが捕捉されず 500 になっていた。
    """
    response = client.put(f"/api/v1/anchors/{'1' * digits}", json=ANCHOR_BODY)
    assert response.status_code == 400


@pytest.mark.parametrize("digits", [17, 100, 4400])
def test_overlong_hex_id_returns_400(client: TestClient, digits: int) -> None:
    response = client.put(f"/api/v1/anchors/0x{'1' * digits}", json=ANCHOR_BODY)
    assert response.status_code == 400


@pytest.mark.parametrize("digits", [17, 4400])
def test_parse_hex_id_raises_value_range_error(digits: int) -> None:
    """`parse_hex_id` が送出する例外は `ValueRangeError` に統一されている。"""
    with pytest.raises(ValueRangeError):
        parse_hex_id("1" * digits)
    with pytest.raises(ValueRangeError):
        parse_hex_id("0x" + "1" * digits)


def test_valid_ids_still_parse() -> None:
    assert parse_hex_id("0x0100") == 0x0100
    assert parse_hex_id("256") == 256

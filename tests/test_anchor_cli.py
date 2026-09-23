"""座標入力 CLI のテスト。

ネットワークへは出ず、CLI が組み立てるリクエストと表示内容だけを確認する。
"""

from __future__ import annotations

from typing import Any

import pytest

import anchor_cli


class _Recorder:
    """`anchor_cli._request` を差し替えて呼び出し内容を記録する。"""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, str, str, str | None, Any]] = []

    def __call__(self, server: str, method: str, path: str, token: str | None, body: Any = None) -> Any:
        self.calls.append((server, method, path, token, body))
        return self.response


@pytest.fixture
def anchor_payload() -> dict[str, Any]:
    return {
        "rev": 3,
        "anchors": [
            {
                "id": "0x0100",
                "label": "北西の柱",
                "x": 0.0,
                "y": 0.0,
                "z": 1.8,
                "enabled": True,
                "source": "manual",
                "updated_at": "2026-09-23T00:00:00+00:00",
            },
            {
                "id": "0x0101",
                "label": None,
                "x": 5.12,
                "y": 0.0,
                "z": 1.8,
                "enabled": False,
                "source": "survey",
                "updated_at": "2026-09-23T00:00:01+00:00",
            },
        ],
    }


def test_list_prints_all_anchors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], anchor_payload: dict[str, Any]
) -> None:
    recorder = _Recorder(anchor_payload)
    monkeypatch.setattr(anchor_cli, "_request", recorder)

    assert anchor_cli.main(["--server", "http://example:1", "list"]) == 0

    assert recorder.calls == [("http://example:1", "GET", "/api/v1/anchors", None, None)]
    out = capsys.readouterr().out
    assert "rev = 3" in out
    assert "0x0100" in out and "北西の柱" in out
    assert "有効" in out and "無効" in out


def test_list_reports_empty_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(anchor_cli, "_request", _Recorder({"rev": 1, "anchors": []}))
    assert anchor_cli.main(["list"]) == 0
    assert "登録されていません" in capsys.readouterr().out


def test_set_sends_meters_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {"rev": 2, "anchor": {"id": "0x0100", "x": 0.0, "y": 0.0, "z": 1.8}}
    recorder = _Recorder(response)
    monkeypatch.setattr(anchor_cli, "_request", recorder)

    args = ["--token", "s3cret", "set", "0x0100", "--x", "0", "--y", "0", "--z", "1.8", "--label", "柱"]
    assert anchor_cli.main(args) == 0

    _, method, path, token, body = recorder.calls[0]
    assert (method, path, token) == ("PUT", "/api/v1/anchors/0x0100", "s3cret")
    assert body == {"x": 0.0, "y": 0.0, "z": 1.8, "enabled": True, "label": "柱"}


def test_set_disabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder({"rev": 2, "anchor": {"id": "0x0101", "x": 5.12, "y": 0.0, "z": 1.8}})
    monkeypatch.setattr(anchor_cli, "_request", recorder)

    args = ["set", "0x0101", "--x", "5.12", "--y", "0", "--z", "1.8", "--disabled"]
    assert anchor_cli.main(args) == 0

    body = recorder.calls[0][4]
    assert body["enabled"] is False
    # --label を指定しない場合はフィールドごと送らず、サーバー側の既定に任せる
    assert "label" not in body


def test_telemetry_sends_body(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {"rev": 4, "telemetry": {"host": "192.168.1.10", "port": 47100, "batch_cycles": 8}}
    recorder = _Recorder(response)
    monkeypatch.setattr(anchor_cli, "_request", recorder)

    args = ["telemetry", "--host", "192.168.1.10", "--port", "47100", "--batch-cycles", "8"]
    assert anchor_cli.main(args) == 0

    _, method, path, _, body = recorder.calls[0]
    assert (method, path) == ("PUT", "/api/v1/config/telemetry")
    assert body == {"host": "192.168.1.10", "port": 47100, "batch_cycles": 8}


def test_config_uses_revision_path(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder({"rev": 6})
    monkeypatch.setattr(anchor_cli, "_request", recorder)

    assert anchor_cli.main(["config"]) == 0
    assert recorder.calls[0][2] == "/api/v1/config"

    assert anchor_cli.main(["config", "--rev", "6"]) == 0
    assert recorder.calls[1][2] == "/api/v1/config/revisions/6"


def test_api_error_is_reported(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _fail(*_args: Any, **_kwargs: Any) -> Any:
        raise anchor_cli.ApiError("接続できません")

    monkeypatch.setattr(anchor_cli, "_request", _fail)
    assert anchor_cli.main(["list"]) == 1
    assert "接続できません" in capsys.readouterr().err

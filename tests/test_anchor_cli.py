"""座標入力 CLI のテスト。

ネットワークへは出ず、CLI が組み立てるリクエストと表示内容だけを確認する。
"""

from __future__ import annotations

import json
from pathlib import Path
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


class _Server:
    """`GET /api/v1/anchors` と `POST /api/v1/anchors:bulk` だけを真似る。"""

    def __init__(self, anchors: list[dict[str, Any]]) -> None:
        self.anchors = anchors
        self.calls: list[tuple[str, str, str | None, Any]] = []

    def __call__(self, server: str, method: str, path: str, token: str | None, body: Any = None) -> Any:
        self.calls.append((method, path, token, body))
        if (method, path) == ("GET", "/api/v1/anchors"):
            return {"rev": 5, "anchors": self.anchors}
        if (method, path) == ("POST", "/api/v1/anchors:bulk"):
            return {"rev": 6, "anchors": body["anchors"]}
        raise AssertionError(f"想定していない呼び出し: {method} {path}")


def _current(anchor_id: str, x: float, label: str | None = None, source: str = "manual") -> dict[str, Any]:
    return {
        "id": anchor_id,
        "label": label,
        "x": x,
        "y": 0.0,
        "z": 0.26,
        "enabled": True,
        "source": source,
        "updated_at": "2026-10-04T00:00:00+00:00",
    }


@pytest.fixture
def room_file(tmp_path: Path) -> Path:
    path = tmp_path / "room.json"
    document = {
        "note": "room-1",
        "anchors": [
            {"id": "0x0100", "x": 0.0, "y": 0.0, "z": 0.26, "label": "左前"},
            {"id": "0x0101", "x": 2.3, "y": 0.0, "z": 0.26, "label": "右前"},
        ],
    }
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path


def test_apply_posts_file_contents(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], room_file: Path
) -> None:
    server = _Server([_current("0x0100", 0.0, "左前"), _current("0x0105", 9.0)])
    monkeypatch.setattr(anchor_cli, "_request", server)

    assert anchor_cli.main(["--token", "s3cret", "apply", str(room_file)]) == 0

    assert [call[:3] for call in server.calls] == [
        ("GET", "/api/v1/anchors", "s3cret"),
        ("POST", "/api/v1/anchors:bulk", "s3cret"),
    ]
    assert server.calls[1][3] == json.loads(room_file.read_text(encoding="utf-8"))
    out = capsys.readouterr().out
    assert "  0x0100" in out
    assert "+ 0x0101" in out
    assert "- 0x0105" in out
    assert "rev 6 へ更新しました: 2 台" in out


def test_apply_dry_run_does_not_post(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], room_file: Path
) -> None:
    server = _Server([_current("0x0100", 0.5, "左前")])
    monkeypatch.setattr(anchor_cli, "_request", server)

    assert anchor_cli.main(["apply", str(room_file), "--dry-run"]) == 0

    assert [call[:2] for call in server.calls] == [("GET", "/api/v1/anchors")]
    out = capsys.readouterr().out
    assert "~ 0x0100" in out
    assert "送信しません" in out


def test_apply_skips_when_nothing_changes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], room_file: Path
) -> None:
    # 2.3 と 2.3000001 はミリメートルにすると同じ値なので変更とみなさない
    server = _Server([_current("0x0100", 0.0, "左前"), _current("0x0101", 2.3000001, "右前")])
    monkeypatch.setattr(anchor_cli, "_request", server)

    assert anchor_cli.main(["apply", str(room_file)]) == 0

    assert len(server.calls) == 1
    assert "変更はありません" in capsys.readouterr().out


def test_apply_treats_source_change_as_difference(monkeypatch: pytest.MonkeyPatch, room_file: Path) -> None:
    server = _Server([_current("0x0100", 0.0, "左前", "survey"), _current("0x0101", 2.3, "右前")])
    monkeypatch.setattr(anchor_cli, "_request", server)

    assert anchor_cli.main(["apply", str(room_file)]) == 0
    assert server.calls[-1][:2] == ("POST", "/api/v1/anchors:bulk")


def test_apply_note_option_overrides_file(monkeypatch: pytest.MonkeyPatch, room_file: Path) -> None:
    server = _Server([])
    monkeypatch.setattr(anchor_cli, "_request", server)

    assert anchor_cli.main(["apply", str(room_file), "--note", "部屋 1 に移設"]) == 0
    assert server.calls[-1][3]["note"] == "部屋 1 に移設"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{", "JSON として解釈できません"),
        ("[]", "anchors の配列"),
        ('{"anchors": [1]}', "オブジェクトで書いてください"),
        ('{"anchors": [{"id": "0x0100", "x": 0, "y": 0}]}', "z がありません"),
        ('{"anchors": [{"id": 256, "x": 0, "y": 0, "z": 0}]}', "id は文字列"),
        ('{"anchors": [{"id": "0xZZ", "x": 0, "y": 0, "z": 0}]}', "id として解釈できません"),
        ('{"anchors": [{"id": "0x0100", "x": "0", "y": 0, "z": 0}]}', "座標は数値"),
        ('{"anchors": [{"id": "0x0100", "x": Infinity, "y": 0, "z": 0}]}', "有限でない値"),
        ('{"anchors": [{"id": "0x0100", "x": 0, "y": NaN, "z": 0}]}', "有限でない値"),
        (
            '{"anchors": [{"id": "0x0100", "x": 0, "y": 0, "z": 0}, {"id": "256", "x": 1, "y": 0, "z": 0}]}',
            "重複しています: 0x0100",
        ),
    ],
)
def test_apply_reports_invalid_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    server = _Server([])
    monkeypatch.setattr(anchor_cli, "_request", server)
    path = tmp_path / "broken.json"
    path.write_text(content, encoding="utf-8")

    assert anchor_cli.main(["apply", str(path)]) == 1

    assert server.calls == []
    assert message in capsys.readouterr().err


def test_apply_reports_missing_file(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert anchor_cli.main(["apply", str(tmp_path / "missing.json")]) == 1
    assert "を読めません" in capsys.readouterr().err

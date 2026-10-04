"""座標入力 CLI のテスト。

ネットワークへは出ず、CLI が組み立てるリクエストと表示内容だけを確認する。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

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
        # float へ直せない桁数の整数でも、未処理の例外にせず範囲外として報告する
        pytest.param(
            '{"anchors": [{"id": "0x0100", "x": 1' + "0" * 400 + ', "y": 0, "z": 0}]}',
            "扱える範囲を超えて",
            id="int-too-large-for-float",
        ),
        # int_max_str_digits (既定 4300 桁) を超える整数は json.loads が ValueError を送出する
        pytest.param(
            '{"anchors": [{"id": "0x0100", "x": 1' + "0" * 5000 + ', "y": 0, "z": 0}]}',
            "JSON として解釈できません",
            id="int-exceeds-max-str-digits",
        ),
        # 入れ子が深すぎると json.loads が RecursionError を送出する
        pytest.param("[" * 1_000_000 + "]" * 1_000_000, "JSON として解釈できません", id="too-deep"),
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


# サーバーの bulk API が 422 で拒否する本文。CLI も送る前に同じものを拒否する
_INVALID_DOCUMENTS: list[Any] = [
    {"anchors": []},
    {"anchors": [{"id": "0x0100", "x": 0, "y": 0, "z": 0}], "rev": 3},
    {"anchors": [{"id": "0x0100", "x": 0, "y": 0, "z": 0, "x_mm": 0}]},
    {"anchors": [{"id": f"{0x0100 + i}", "x": 0, "y": 0, "z": 0} for i in range(256)]},
    {"anchors": [{"id": "0x00FF", "x": 0, "y": 0, "z": 0}]},
    {"anchors": [{"id": "0xFFFF", "x": 0, "y": 0, "z": 0}]},
    {"anchors": [{"id": "+256", "x": 0, "y": 0, "z": 0}]},
    {"anchors": [{"id": "1_0_0_0", "x": 0, "y": 0, "z": 0}]},
    {"anchors": [{"id": "0x0100", "x": 1e9, "y": 0, "z": 0}]},
    # ミリメートルへ丸めると範囲内に入るが、メートル値では範囲外
    {"anchors": [{"id": "0x0100", "x": 2147483.6474, "y": 0, "z": 0}]},
    {"anchors": [{"id": "0x0100", "x": 0, "y": -2147483.6474, "z": 0}]},
    {"anchors": [{"id": "0x0100", "x": 0, "y": 0, "z": 2147484}]},
    {"anchors": [{"id": "0x0100", "x": 0, "y": 0, "z": 0}], "source": "guess"},
    {"anchors": [{"id": "0x0100", "x": 0, "y": 0, "z": 0}], "source": None},
    {"anchors": [{"id": "0x0100", "x": 0, "y": 0, "z": 0}], "note": "x" * 201},
    {"anchors": [{"id": "0x0100", "x": 0, "y": 0, "z": 0}], "note": 1},
    {"anchors": [{"id": "0x0100", "x": 0, "y": 0, "z": 0, "label": 1}]},
    {"anchors": [{"id": "0x0100", "x": 0, "y": 0, "z": 0, "enabled": [True]}]},
]


@pytest.mark.parametrize("document", _INVALID_DOCUMENTS)
def test_apply_rejects_what_server_rejects(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    client: TestClient,
    document: Any,
) -> None:
    assert client.post("/api/v1/anchors:bulk", json=document).status_code == 422

    # 現在の構成も空なので、検証を通ってしまうと「変更はありません」で成功扱いになる
    server = _Server([])
    monkeypatch.setattr(anchor_cli, "_request", server)
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert anchor_cli.main(["apply", str(path)]) == 1
    assert server.calls == []
    assert capsys.readouterr().err


def test_apply_checks_note_option(monkeypatch: pytest.MonkeyPatch, room_file: Path) -> None:
    server = _Server([])
    monkeypatch.setattr(anchor_cli, "_request", server)

    assert anchor_cli.main(["apply", str(room_file), "--note", "x" * 201]) == 1
    assert server.calls == []


def test_apply_accepts_what_server_accepts(tmp_path: Path, client: TestClient) -> None:
    document = {
        "source": "survey",
        "note": "x" * 200,
        "anchors": [
            {"id": "0x0100", "x": 0, "y": 0, "z": 0, "label": None, "enabled": False},
            {"id": " 0x0101 ", "x": -2147483.647, "y": 2147483.647, "z": 0.0005},
            {"id": "65534", "x": 1, "y": 2, "z": 3, "label": "柱"},
        ],
    }
    path = tmp_path / "valid.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert anchor_cli._load_anchor_file(path) == document
    assert client.post("/api/v1/anchors:bulk", json=document).status_code == 200


def test_apply_reports_non_utf8_file(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    path = tmp_path / "sjis.json"
    path.write_bytes(
        '{"anchors": [{"id": "0x0100", "x": 0, "y": 0, "z": 0, "label": "左前"}]}'.encode("cp932")
    )
    assert anchor_cli.main(["apply", str(path)]) == 1
    assert "UTF-8 として読めません" in capsys.readouterr().err


def test_apply_reports_missing_file(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert anchor_cli.main(["apply", str(tmp_path / "missing.json")]) == 1
    assert "を読めません" in capsys.readouterr().err

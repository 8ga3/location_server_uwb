#!/usr/bin/env python3
"""アンカー座標を手入力するための CLI。

サーバーの管理 API を叩くので、サーバーが動いていれば追加の依存なしで使える。
座標はメートルで入力する (API と同じ表記)。DB には整数ミリメートルで保存される。

使用例:

    python tools/anchor_cli.py list
    python tools/anchor_cli.py set 0x0100 --x 0 --y 0 --z 1.8 --label 北西の柱
    python tools/anchor_cli.py apply anchors.json --dry-run
    python tools/anchor_cli.py telemetry --host 192.168.1.10 --port 47100 --batch-cycles 4
    python tools/anchor_cli.py config
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

DEFAULT_SERVER = "http://127.0.0.1:8000"
TIMEOUT_SEC = 10.0


class ApiError(RuntimeError):
    """サーバーがエラー応答を返した場合に送出する。"""


class InputError(ValueError):
    """座標表ファイルを読めない、または形式が正しくない場合に送出する。"""


def _request(server: str, method: str, path: str, token: str | None, body: Any = None) -> Any:
    url = urllib.parse.urljoin(server.rstrip("/") + "/", path.lstrip("/"))
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Auth-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"{method} {url} が {exc.code} を返しました: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"{method} {url} に接続できません: {exc.reason}") from exc
    if not payload:
        return None
    return json.loads(payload)


def _print_anchors(payload: dict[str, Any]) -> None:
    print(f"rev = {payload['rev']}")
    anchors = payload["anchors"]
    if not anchors:
        print("アンカーは 1 台も登録されていません")
        return
    print(f"{'id':<8} {'x [m]':>10} {'y [m]':>10} {'z [m]':>10}  状態   {'source':<8} label")
    for anchor in anchors:
        # 全角 2 文字で揃えることで、有効・無効が混ざっても列がずれない
        enabled = "有効" if anchor["enabled"] else "無効"
        label = anchor["label"] or ""
        print(
            f"{anchor['id']:<8} {anchor['x']:>10.3f} {anchor['y']:>10.3f} {anchor['z']:>10.3f}"
            f"  {enabled}   {anchor['source']:<8} {label}"
        )


def cmd_list(args: argparse.Namespace) -> int:
    payload = _request(args.server, "GET", "/api/v1/anchors", args.token)
    _print_anchors(payload)
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    body: dict[str, Any] = {
        "x": args.x,
        "y": args.y,
        "z": args.z,
        "enabled": not args.disabled,
    }
    if args.label is not None:
        body["label"] = args.label
    payload = _request(args.server, "PUT", f"/api/v1/anchors/{args.anchor_id}", args.token, body)
    anchor = payload["anchor"]
    print(
        f"rev {payload['rev']} へ更新しました: {anchor['id']} "
        f"({anchor['x']:.3f}, {anchor['y']:.3f}, {anchor['z']:.3f}) [m]"
    )
    return 0


def cmd_telemetry(args: argparse.Namespace) -> int:
    body = {"host": args.host, "port": args.port, "batch_cycles": args.batch_cycles}
    payload = _request(args.server, "PUT", "/api/v1/config/telemetry", args.token, body)
    telemetry = payload["telemetry"]
    print(
        f"rev {payload['rev']} へ更新しました: host={telemetry['host'] or '(未設定)'} "
        f"port={telemetry['port']} batch_cycles={telemetry['batch_cycles']}"
    )
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    path = "/api/v1/config" if args.rev is None else f"/api/v1/config/revisions/{args.rev}"
    payload = _request(args.server, "GET", path, args.token)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _anchor_id(value: Any) -> int:
    """`0x0100` 形式または 10 進数表記の ID を整数にする。

    差分を取るためだけに使い、範囲はサーバーが検証する。
    """
    if not isinstance(value, str):
        raise InputError(f"id は文字列で書いてください: {value!r}")
    text = value.strip()
    try:
        return int(text[2:], 16) if text[:2].lower() == "0x" else int(text, 10)
    except ValueError as exc:
        raise InputError(f"id として解釈できません: {value!r}") from exc


def _mm(value: Any) -> int:
    """メートル表記の座標を、サーバーと同じ四捨五入で整数ミリメートルにする。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InputError(f"座標は数値で書いてください: {value!r}")
    # json.loads は Infinity / NaN も数値として読むので、ここで弾く
    if not math.isfinite(value):
        raise InputError(f"座標に有限でない値は使えません: {value!r}")
    return int(Decimal(str(value)).scaleb(3).to_integral_value(rounding=ROUND_HALF_UP))


def _anchor_key(anchor: dict[str, Any], source: str) -> tuple[Any, ...]:
    """差分の比較に使う値。座標は丸め差を避けるためミリメートルで比べる。"""
    return (
        _mm(anchor["x"]),
        _mm(anchor["y"]),
        _mm(anchor["z"]),
        anchor.get("label"),
        anchor.get("enabled", True),
        source,
    )


def _load_anchor_file(path: Path) -> dict[str, Any]:
    """座標表ファイル (JSON) を読み、bulk API へ渡す本文として返す。"""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"{path} を読めません: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} は JSON として解釈できません: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("anchors"), list):
        raise InputError(f"{path} には anchors の配列を持つオブジェクトを書いてください")
    seen: set[int] = set()
    for anchor in document["anchors"]:
        if not isinstance(anchor, dict):
            raise InputError(f"anchors の要素はオブジェクトで書いてください: {anchor!r}")
        missing = [key for key in ("id", "x", "y", "z") if key not in anchor]
        if missing:
            raise InputError(f"{anchor.get('id', '(id なし)')} に {', '.join(missing)} がありません")
        # 差分の表示で使う値だけここで確かめる。範囲はサーバーが検証する。
        # ID の重複は差分の表示が潰れてしまうので、送る前にここでも弾く
        anchor_id = _anchor_id(anchor["id"])
        if anchor_id in seen:
            raise InputError(f"アンカー ID が重複しています: 0x{anchor_id:04X}")
        seen.add(anchor_id)
        for key in ("x", "y", "z"):
            _mm(anchor[key])
    return document


def _print_diff(current: dict[str, Any], document: dict[str, Any]) -> bool:
    """現在のアンカー表と座標表ファイルの差分を表示する。差分があれば真を返す。"""
    source = document.get("source", "manual")
    before = {_anchor_id(a["id"]): a for a in current["anchors"]}
    after = {_anchor_id(a["id"]): a for a in document["anchors"]}
    changed = False
    for anchor_id in sorted(before.keys() | after.keys()):
        old = before.get(anchor_id)
        new = after.get(anchor_id)
        if new is None:
            mark = "-"
        elif old is None:
            mark = "+"
        elif _anchor_key(old, old["source"]) != _anchor_key(new, source):
            mark = "~"
        else:
            mark = " "
        changed = changed or mark != " "
        shown = new if new is not None else before[anchor_id]
        enabled = "有効" if shown.get("enabled", True) else "無効"
        print(
            f"{mark} 0x{anchor_id:04X} {shown['x']:>10.3f} {shown['y']:>10.3f} {shown['z']:>10.3f}"
            f"  {enabled}   {shown.get('label') or ''}"
        )
    return changed


def cmd_apply(args: argparse.Namespace) -> int:
    document = _load_anchor_file(Path(args.file))
    if args.note is not None:
        document["note"] = args.note

    current = _request(args.server, "GET", "/api/v1/anchors", args.token)
    print(f"rev {current['rev']} からの差分 (+ 追加、- 削除、~ 変更):")
    if not _print_diff(current, document):
        print("変更はありません。送信しません")
        return 0
    if args.dry_run:
        print("--dry-run を指定したので送信しません")
        return 0

    payload = _request(args.server, "POST", "/api/v1/anchors:bulk", args.token, document)
    print(f"rev {payload['rev']} へ更新しました: {len(payload['anchors'])} 台")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="anchor_cli", description=__doc__.splitlines()[0])
    parser.add_argument(
        "--server",
        default=os.environ.get("UWB_SERVER_URL", DEFAULT_SERVER),
        help=f"サーバーの URL (既定: {DEFAULT_SERVER}、環境変数 UWB_SERVER_URL でも指定できる)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("UWB_AUTH_TOKEN"),
        help="書き込み系 API の共有トークン (環境変数 UWB_AUTH_TOKEN でも指定できる)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="アンカー一覧を表示する")
    p_list.set_defaults(func=cmd_list)

    p_set = sub.add_parser("set", help="アンカー 1 台の座標を登録または更新する")
    p_set.add_argument("anchor_id", help="アンカー ID (0x0100 形式または 10 進数)")
    p_set.add_argument("--x", type=float, required=True, help="X 座標 [m]")
    p_set.add_argument("--y", type=float, required=True, help="Y 座標 [m]")
    p_set.add_argument("--z", type=float, required=True, help="Z 座標 [m]")
    p_set.add_argument("--label", default=None, help="現場での呼び名")
    p_set.add_argument("--disabled", action="store_true", help="このアンカーを構成配信から外す")
    p_set.set_defaults(func=cmd_set)

    p_apply = sub.add_parser(
        "apply", help="座標表ファイル (JSON) でアンカー表を丸ごと置き換える。ファイルに無いアンカーは削除する"
    )
    p_apply.add_argument("file", help="座標表ファイル (JSON)")
    p_apply.add_argument("--dry-run", action="store_true", help="差分だけ表示し、サーバーへは送らない")
    p_apply.add_argument(
        "--note", default=None, help="構成リビジョンに残すメモ (ファイルの note より優先する)"
    )
    p_apply.set_defaults(func=cmd_apply)

    p_tel = sub.add_parser("telemetry", help="テレメトリ送信先を更新する")
    p_tel.add_argument("--host", default="", help="テレメトリ送信先の IP アドレス")
    p_tel.add_argument("--port", type=int, required=True, help="送信先ポート (0 で送信停止)")
    p_tel.add_argument("--batch-cycles", type=int, default=4, help="1 パケットに詰めるサイクル数")
    p_tel.set_defaults(func=cmd_telemetry)

    p_config = sub.add_parser("config", help="タグへ配る構成を表示する")
    p_config.add_argument("--rev", type=int, default=None, help="過去のリビジョンを指定する")
    p_config.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result: int = args.func(args)
    except (ApiError, InputError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())

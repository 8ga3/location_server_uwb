#!/usr/bin/env python3
"""タグ側フィルタの効果を、最小二乗の解と比べて数値で出す CLI (設計文書 8.4)。

参照 API (`GET /api/v1/sessions/{id}/track`) から測位結果を間引かずに取り、最小二乗の解とフィルタ後の位置が
どちらも有効なサイクルどうしで次の指標を比べる。可視化ページのサマリ (`static/js/filter_metrics.js`) と
同じ定義で、定義を変えるときは両方を揃える。

- 散らばり RMS: 範囲内の平均位置からの水平距離の RMS。静止している区間で見る
- 跳び: seq が 1 つ違う隣り合うサイクルの間の水平の移動量。RMS、最大、50 mm を超えた回数

時刻の範囲 (`--from` / `--to`) は、セッションの最初のサイクルからの秒数で指定する。`--window` を付けると
その長さの区間ごとの表も出すので、静止していた区間を探すのに使う。

使用例:

    python tools/filter_compare.py --server http://192.168.1.10:8000
    python tools/filter_compare.py 2738 --window 10
    python tools/filter_compare.py 2738 --from 260 --to 310
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_SERVER = "http://127.0.0.1:8000"
TIMEOUT_SEC = 30.0
# 参照 API の max_points の上限。これを超える範囲は間引かれるので、分けて取り直す
MAX_POINTS = 20_000
JUMP_THRESHOLD_M = 0.05
DEFAULT_LIST_JUMP_MM = 200.0

Fix = dict[str, list[Any]]
Fetch = Callable[[str], Any]


class ApiError(RuntimeError):
    """サーバーがエラー応答を返したか、接続できなかった場合に送出する。"""


def _get(server: str, path: str) -> Any:
    url = urllib.parse.urljoin(server.rstrip("/") + "/", path.lstrip("/"))
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SEC) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ApiError(f"GET {url} が {exc.code} を返しました: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"GET {url} に接続できません: {exc.reason}") from exc


# ------------------------------------------------------------------ 指標


@dataclass(frozen=True, slots=True)
class PositionStats:
    """1 つの方式 (最小二乗かフィルタ) の指標。長さはメートル。跳びの値は組が無ければ None。"""

    mean_x: float
    mean_y: float
    scatter_rms: float
    step_rms: float | None
    step_max: float | None
    jumps: int | None


@dataclass(frozen=True, slots=True)
class FilterMetrics:
    """範囲内の比較結果。`compared` は両方が有効なサイクルの数、`pairs` は跳びを数えた組の数。"""

    cycles: int
    compared: int
    pairs: int
    rejected: int
    predicted: int
    ls: PositionStats
    kf: PositionStats


def _position_stats(xs: list[float], ys: list[float], steps: list[float]) -> PositionStats:
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    scatter = math.sqrt(sum((x - mean_x) ** 2 + (y - mean_y) ** 2 for x, y in zip(xs, ys, strict=True)) / n)
    if not steps:
        return PositionStats(mean_x, mean_y, scatter, None, None, None)
    return PositionStats(
        mean_x,
        mean_y,
        scatter,
        math.sqrt(sum(s * s for s in steps) / len(steps)),
        max(steps),
        sum(1 for s in steps if s > JUMP_THRESHOLD_M),
    )


def filter_metrics(fix: Fix, lo: int, hi: int) -> FilterMetrics | None:
    """`fix` の lo..hi (両端を含む) の範囲で指標を求める。対象のサイクルが無ければ None。"""
    lo = max(0, lo)
    hi = min(len(fix["t"]) - 1, hi)
    ls_x: list[float] = []
    ls_y: list[float] = []
    kf_x: list[float] = []
    kf_y: list[float] = []
    ls_steps: list[float] = []
    kf_steps: list[float] = []
    rejected = 0
    predicted = 0
    prev = -1
    for i in range(lo, hi + 1):
        rejected += fix["krej"][i] or 0
        if fix["kok"][i] and not fix["kupd"][i]:
            predicted += 1
        if not (fix["ok"][i] and fix["kok"][i]):
            continue
        if prev >= 0 and fix["seq"][i] - fix["seq"][prev] == 1:
            ls_steps.append(math.hypot(fix["x"][i] - fix["x"][prev], fix["y"][i] - fix["y"][prev]))
            kf_steps.append(math.hypot(fix["kx"][i] - fix["kx"][prev], fix["ky"][i] - fix["ky"][prev]))
        ls_x.append(fix["x"][i])
        ls_y.append(fix["y"][i])
        kf_x.append(fix["kx"][i])
        kf_y.append(fix["ky"][i])
        prev = i
    if not ls_x:
        return None
    return FilterMetrics(
        cycles=hi - lo + 1,
        compared=len(ls_x),
        pairs=len(ls_steps),
        rejected=rejected,
        predicted=predicted,
        ls=_position_stats(ls_x, ls_y, ls_steps),
        kf=_position_stats(kf_x, kf_y, kf_steps),
    )


@dataclass(frozen=True, slots=True)
class Jump:
    """最小二乗の解が大きく跳んだサイクル。`t_s` はセッションの最初のサイクルからの秒数。"""

    t_s: float
    seq: int
    ls_step: float
    kf_step: float
    resid: float | None
    rejected: int | None


def big_jumps(fix: Fix, threshold_m: float, origin_ms: int) -> list[Jump]:
    """隣り合うサイクルで最小二乗の解が `threshold_m` を超えて跳んだ箇所と、そのときのフィルタの動き。

    時刻は `origin_ms` (セッションの最初のサイクルの時刻) からの秒数にする。`--from` で範囲を絞っても、
    表示した時刻をそのまま `--from` / `--to` に渡せるようにするためである。
    """
    jumps: list[Jump] = []
    for i in range(1, len(fix["t"])):
        p = i - 1
        if fix["seq"][i] - fix["seq"][p] != 1:
            continue
        if not (fix["ok"][i] and fix["kok"][i] and fix["ok"][p] and fix["kok"][p]):
            continue
        step = math.hypot(fix["x"][i] - fix["x"][p], fix["y"][i] - fix["y"][p])
        if step <= threshold_m:
            continue
        kf_step = math.hypot(fix["kx"][i] - fix["kx"][p], fix["ky"][i] - fix["ky"][p])
        jumps.append(
            Jump(
                (fix["t"][i] - origin_ms) / 1000,
                fix["seq"][i],
                step,
                kf_step,
                fix["resid"][i],
                fix["krej"][i],
            )
        )
    return jumps


# ------------------------------------------------------------------ 取得


def fetch_track(fetch: Fetch, session_id: int, from_ms: int, to_ms: int) -> Fix:
    """`from_ms`..`to_ms` (両端を含む) の測位結果を間引かずに取る。

    間引かれて返ってきた範囲は半分に分けて取り直す。`fetch` は API のパスを受けて JSON を返す関数。
    """
    query = urllib.parse.urlencode({"from_ms": from_ms, "to_ms": to_ms, "max_points": MAX_POINTS})
    payload = fetch(f"/api/v1/sessions/{session_id}/track?{query}")
    fix: Fix = payload["fix"]
    if payload["decimation"]["stride"] <= 1 or to_ms <= from_ms:
        return fix
    middle = (from_ms + to_ms) // 2
    first = fetch_track(fetch, session_id, from_ms, middle)
    second = fetch_track(fetch, session_id, middle + 1, to_ms)
    return {key: first[key] + second[key] for key in first}


def latest_session_id(fetch: Fetch) -> int:
    sessions = fetch("/api/v1/sessions?limit=1")["sessions"]
    if not sessions:
        raise ApiError("セッションが 1 件もありません")
    session_id: int = sessions[0]["id"]
    return session_id


# ------------------------------------------------------------------ 表示


def _width(text: str) -> int:
    """端末での表示幅。全角の文字は 2 桁として数える。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _left(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _right(text: str, width: int) -> str:
    return " " * max(0, width - _width(text)) + text


def _mm(value: float | None) -> str:
    return "--" if value is None else f"{value * 1000:.1f} mm"


def _ratio(kf: float | None, ls: float | None) -> str:
    if kf is None or ls is None or ls <= 0:
        return "--"
    return f"{kf / ls:.2f}"


def format_metrics(metrics: FilterMetrics) -> list[str]:
    ls, kf = metrics.ls, metrics.kf
    rows: list[tuple[str, str, str, str]] = [
        ("散らばり RMS", _mm(ls.scatter_rms), _mm(kf.scatter_rms), _ratio(kf.scatter_rms, ls.scatter_rms)),
        ("跳び RMS", _mm(ls.step_rms), _mm(kf.step_rms), _ratio(kf.step_rms, ls.step_rms)),
        ("跳びの最大", _mm(ls.step_max), _mm(kf.step_max), _ratio(kf.step_max, ls.step_max)),
        (
            f"{JUMP_THRESHOLD_M * 1000:.0f} mm を超える跳び",
            "--" if ls.jumps is None else str(ls.jumps),
            "--" if kf.jumps is None else str(kf.jumps),
            "--" if ls.jumps is None or kf.jumps is None else _ratio(float(kf.jumps), float(ls.jumps)),
        ),
    ]
    lines = [_left("指標", 22) + _right("最小二乗", 12) + _right("フィルタ", 12) + _right("比", 7)]
    lines += [_left(label, 22) + _right(a, 12) + _right(b, 12) + _right(r, 7) for label, a, b, r in rows]
    lines.append(
        f"比較 {metrics.compared} / {metrics.cycles} サイクル、跳びの組 {metrics.pairs}、"
        f"棄却した測距 {metrics.rejected}、予測のみ {metrics.predicted}"
    )
    lines.append(
        f"平均位置: 最小二乗 ({ls.mean_x:.3f}, {ls.mean_y:.3f}) m / "
        f"フィルタ ({kf.mean_x:.3f}, {kf.mean_y:.3f}) m"
    )
    return lines


def _seconds(value: float) -> str:
    """秒をミリ秒の精度で表す。末尾の 0 は落とす (10.000 は 10、0.500 は 0.5)。"""
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def format_windows(fix: Fix, window_s: float, origin_ms: int) -> list[str]:
    """`window_s` 秒ごとの区間に区切った表。散らばりが小さい区間が静止していた区間の候補になる。

    区間の境界と開始時刻は `origin_ms` (セッションの最初のサイクルの時刻) を基準にする。`--from` で範囲を
    絞っても同じ境界になり、開始時刻をそのまま `--from` に渡せる。
    """
    window_ms = round(window_s * 1000)
    if window_ms < 1:
        raise ValueError(f"区間の長さは 1 ms 以上です: {window_s} s")
    headers = [
        "開始 [s]",
        "LS 平均 x",
        "LS 平均 y",
        "LS 散らばり",
        "KF 散らばり",
        "LS 跳び",
        "KF 跳び",
        "棄却",
    ]
    widths = [9, 10, 10, 12, 12, 9, 9, 5]
    lines = [" ".join(_right(h, w) for h, w in zip(headers, widths, strict=True))]
    if not fix["t"]:
        return lines
    lo = 0
    n = len(fix["t"])
    while lo < n:
        start = fix["t"][lo] - (fix["t"][lo] - origin_ms) % window_ms
        hi = lo
        while hi + 1 < n and fix["t"][hi + 1] < start + window_ms:
            hi += 1
        m = filter_metrics(fix, lo, hi)
        begin = _seconds((start - origin_ms) / 1000)
        if m is None:
            cells = [begin, "(比較できるサイクルなし)"]
        else:
            cells = [
                begin,
                f"{m.ls.mean_x:.3f}",
                f"{m.ls.mean_y:.3f}",
                f"{m.ls.scatter_rms * 1000:.1f}",
                f"{m.kf.scatter_rms * 1000:.1f}",
                _num_mm(m.ls.step_rms),
                _num_mm(m.kf.step_rms),
                str(m.rejected),
            ]
        lines.append(" ".join(_right(c, w) for c, w in zip(cells, widths, strict=False)))
        lo = hi + 1
    lines.append("開始は秒、平均 x / y はメートル、散らばりと跳び (RMS) はミリメートル")
    return lines


def _num_mm(value: float | None) -> str:
    return "--" if value is None else f"{value * 1000:.1f}"


def format_jumps(jumps: list[Jump], threshold_m: float) -> list[str]:
    lines = [f"最小二乗の解が {threshold_m * 1000:.0f} mm を超えて跳んだサイクル: {len(jumps)} 件"]
    for j in jumps:
        resid = "--" if j.resid is None else f"{j.resid * 1000:.0f}"
        rejected = "--" if j.rejected is None else str(j.rejected)
        lines.append(
            f"  t={_seconds(j.t_s):>9} s seq={j.seq}  最小二乗 {j.ls_step * 1000:6.0f} mm"
            f"  フィルタ {j.kf_step * 1000:6.0f} mm  残差 {resid} mm  棄却 {rejected}"
        )
    return lines


# ------------------------------------------------------------------ 本体


def run(args: argparse.Namespace, fetch: Fetch) -> list[str]:
    session_id = args.session if args.session is not None else latest_session_id(fetch)
    summary = fetch(f"/api/v1/sessions/{session_id}/summary")
    session = summary["session"]
    first_t, last_t = session["first_t_ms"], session["last_t_ms"]
    if first_t is None or last_t is None:
        return [f"セッション #{session_id} には測位結果がありません"]
    from_ms = first_t if args.from_s is None else first_t + round(args.from_s * 1000)
    to_ms = last_t if args.to_s is None else first_t + round(args.to_s * 1000)
    fix = fetch_track(fetch, session_id, from_ms, to_ms)

    lines = [
        f"セッション #{session_id} (タグ {session['tag_id']})、"
        f"範囲 {(from_ms - first_t) / 1000:.1f}〜{(to_ms - first_t) / 1000:.1f} s (最初のサイクルから)",
        "",
    ]
    metrics = filter_metrics(fix, 0, len(fix["t"]) - 1)
    if metrics is None:
        lines.append("最小二乗とフィルタがどちらも有効なサイクルがありません")
        return lines
    lines += format_metrics(metrics)
    lines.append("散らばりは静止している区間で見る。動いている区間では移動量が入る")
    if args.window is not None:
        lines += ["", *format_windows(fix, args.window, first_t)]
    if args.list_jump_mm > 0:
        threshold_m = args.list_jump_mm / 1000
        lines += ["", *format_jumps(big_jumps(fix, threshold_m, first_t), threshold_m)]
    return lines


# --window の下限 [s]。区間の境界はミリ秒で数えるので、これより短いと区間の長さが 0 になる
WINDOW_MIN_S = 0.001


def _window(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"数値を指定してください: {value}") from None
    if not math.isfinite(number) or number < WINDOW_MIN_S:
        raise argparse.ArgumentTypeError(f"{WINDOW_MIN_S} 以上の有限の値を指定してください: {value}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="タグ側フィルタの効果を最小二乗の解と比べる (設計文書 8.4)")
    parser.add_argument("session", nargs="?", type=int, help="セッション ID (省略すると最新のセッション)")
    parser.add_argument("--server", default=DEFAULT_SERVER, help=f"サーバーの URL (既定 {DEFAULT_SERVER})")
    parser.add_argument("--from", dest="from_s", type=float, help="範囲の始まり [s] (最初のサイクルから)")
    parser.add_argument("--to", dest="to_s", type=float, help="範囲の終わり [s] (最初のサイクルから)")
    parser.add_argument(
        "--window", type=_window, help=f"この秒数ごとの区間に区切った表も出す ({WINDOW_MIN_S} 以上)"
    )
    parser.add_argument(
        "--list-jump-mm",
        type=float,
        default=DEFAULT_LIST_JUMP_MM,
        help=(
            f"最小二乗の解がこの値 [mm] を超えて跳んだサイクルを列挙する"
            f" (既定 {DEFAULT_LIST_JUMP_MM:.0f}、0 で出さない)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        lines = run(args, lambda path: _get(args.server, path))
    except ApiError as exc:
        print(exc, file=sys.stderr)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())

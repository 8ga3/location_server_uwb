"""フィルタの効果を比べる CLI (`tools/filter_compare.py`) のテスト。

ネットワークへは出ず、参照 API の応答を組み立てて渡す。
"""

from __future__ import annotations

import math
import urllib.parse
from typing import Any

import pytest

import filter_compare


def _fix(
    xs: list[float],
    ys: list[float],
    kxs: list[float],
    kys: list[float],
    *,
    seq: list[int] | None = None,
    ok: list[bool] | None = None,
    kok: list[bool] | None = None,
    kupd: list[bool] | None = None,
    krej: list[int | None] | None = None,
) -> dict[str, list[Any]]:
    n = len(xs)
    seqs = seq if seq is not None else list(range(n))
    return {
        "t": [1000 + 100 * s for s in seqs],
        "seq": seqs,
        "x": xs,
        "y": ys,
        "kx": kxs,
        "ky": kys,
        "ok": ok if ok is not None else [True] * n,
        "kok": kok if kok is not None else [True] * n,
        "kupd": kupd if kupd is not None else [True] * n,
        "krej": krej if krej is not None else [0] * n,
        "resid": [0.03] * n,
    }


def test_metrics_compare_scatter_and_steps() -> None:
    # 最小二乗は x が ±0.02 で振れ、フィルタは ±0.01 で振れる
    fix = _fix([0.02, -0.02, 0.02, -0.02], [0.0] * 4, [0.01, -0.01, 0.01, -0.01], [0.0] * 4)
    m = filter_compare.filter_metrics(fix, 0, 3)
    assert m is not None
    assert m.compared == 4
    assert m.pairs == 3
    assert m.ls.scatter_rms == pytest.approx(0.02)
    assert m.kf.scatter_rms == pytest.approx(0.01)
    assert m.ls.step_rms == pytest.approx(0.04)
    assert m.kf.step_max == pytest.approx(0.02)
    assert m.ls.jumps == 0
    assert m.kf.jumps == 0


def test_steps_skip_gaps_and_cycles_not_valid_in_both() -> None:
    # seq 2 は欠番、seq 4 はフィルタが無効。跳びは seq 0-1 の組だけ
    fix = _fix(
        [0.0, 0.1, 0.5, 0.7],
        [0.0] * 4,
        [0.0, 0.06, 0.5, 0.7],
        [0.0] * 4,
        seq=[0, 1, 3, 4],
        kok=[True, True, True, False],
        kupd=[True, True, True, False],
    )
    m = filter_compare.filter_metrics(fix, 0, 3)
    assert m is not None
    assert m.compared == 3
    assert m.pairs == 1
    assert m.ls.step_max == pytest.approx(0.1)
    assert m.ls.jumps == 1
    assert m.kf.jumps == 1


def test_decimated_data_has_no_steps() -> None:
    fix = _fix([0.0, 0.1, 0.2], [0.0] * 3, [0.0, 0.1, 0.2], [0.0] * 3, seq=[0, 3, 6])
    m = filter_compare.filter_metrics(fix, 0, 2)
    assert m is not None
    assert m.pairs == 0
    assert m.ls.step_rms is None
    assert m.ls.jumps is None
    assert "--" in "\n".join(filter_compare.format_metrics(m))


def test_counts_rejections_and_predicted_cycles() -> None:
    fix = _fix(
        [0.0, 0.0, 0.0],
        [0.0] * 3,
        [0.0] * 3,
        [0.0] * 3,
        ok=[True, False, True],
        kupd=[True, False, True],
        krej=[1, None, 2],
    )
    m = filter_compare.filter_metrics(fix, 0, 2)
    assert m is not None
    assert m.rejected == 3
    assert m.predicted == 1
    assert m.compared == 2


def test_no_comparable_cycles() -> None:
    fix = _fix([0.0], [0.0], [0.0], [0.0], ok=[False])
    assert filter_compare.filter_metrics(fix, 0, 0) is None


def test_big_jumps_lists_least_squares_jumps() -> None:
    fix = _fix([0.0, 0.4, 0.41], [0.0] * 3, [0.0, 0.02, 0.03], [0.0] * 3, krej=[0, 1, 0])
    jumps = filter_compare.big_jumps(fix, 0.2, fix["t"][0])
    assert len(jumps) == 1
    assert jumps[0].seq == 1
    assert jumps[0].ls_step == pytest.approx(0.4)
    assert jumps[0].kf_step == pytest.approx(0.02)
    assert jumps[0].rejected == 1
    assert jumps[0].t_s == pytest.approx(0.1)


class _FakeApi:
    """参照 API の代わり。track は `max_points` を超える範囲を間引いて返す。"""

    def __init__(self, fix: dict[str, list[Any]], *, limit: int) -> None:
        self.fix = fix
        self.limit = limit
        self.paths: list[str] = []

    def __call__(self, path: str) -> Any:
        self.paths.append(path)
        parsed = urllib.parse.urlparse(path)
        if parsed.path == "/api/v1/sessions":
            return {"sessions": [{"id": 7}]}
        if parsed.path.endswith("/summary"):
            return {"session": {"tag_id": 1, "first_t_ms": self.fix["t"][0], "last_t_ms": self.fix["t"][-1]}}
        query = urllib.parse.parse_qs(parsed.query)
        lo, hi = int(query["from_ms"][0]), int(query["to_ms"][0])
        rows = [i for i, t in enumerate(self.fix["t"]) if lo <= t <= hi]
        stride = 1 if len(rows) <= self.limit else math.ceil((len(rows) - 1) / (self.limit - 1))
        picked = [r for k, r in enumerate(rows) if k % stride == 0 or k == len(rows) - 1]
        return {
            "decimation": {"total": len(rows), "stride": stride},
            "fix": {key: [values[i] for i in picked] for key, values in self.fix.items()},
        }


def test_fetch_track_splits_decimated_ranges() -> None:
    n = 50
    fix = _fix([0.001 * i for i in range(n)], [0.0] * n, [0.0] * n, [0.0] * n)
    api = _FakeApi(fix, limit=8)
    got = filter_compare.fetch_track(api, 7, fix["t"][0], fix["t"][-1])
    assert got["seq"] == list(range(n))
    assert len(api.paths) > 1


def test_run_uses_latest_session_and_relative_range() -> None:
    n = 40
    fix = _fix([0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n)
    api = _FakeApi(fix, limit=1000)
    args = filter_compare.build_parser().parse_args(["--from", "1", "--to", "2", "--window", "0.5"])
    lines = filter_compare.run(args, api)
    track = [p for p in api.paths if "/track" in p]
    assert track and "/sessions/7/track" in track[0]
    query = urllib.parse.parse_qs(urllib.parse.urlparse(track[0]).query)
    assert int(query["from_ms"][0]) == fix["t"][0] + 1000
    assert int(query["to_ms"][0]) == fix["t"][0] + 2000
    text = "\n".join(lines)
    assert "比較 11 / 11 サイクル" in text
    assert "LS 散らばり" in text


@pytest.mark.parametrize("value", ["0", "0.0005", "-1", "inf", "nan", "abc"])
def test_window_rejects_values_that_cannot_make_a_window(value: str) -> None:
    # ミリ秒にして 1 未満になる値や有限でない値は、データを取りに行く前に引数エラーにする
    with pytest.raises(SystemExit):
        filter_compare.build_parser().parse_args(["--window", value])


def test_window_accepts_one_millisecond() -> None:
    args = filter_compare.build_parser().parse_args(["--window", "0.001"])
    assert args.window == pytest.approx(0.001)


def test_times_are_relative_to_session_start_even_with_from() -> None:
    # 40 サイクル (0〜3.9 s)。2.5 s のサイクルで最小二乗だけが 0.4 m 跳ぶ
    n = 40
    xs = [0.0] * n
    xs[25] = 0.4
    fix = _fix(xs, [0.0] * n, [0.0] * n, [0.0] * n)
    api = _FakeApi(fix, limit=1000)
    args = filter_compare.build_parser().parse_args(["--from", "2", "--window", "0.5"])
    lines = filter_compare.run(args, api)
    starts = [line.split()[0] for line in lines if line[:9].strip().replace(".", "").isdigit()]
    # 区間の開始はセッションの最初のサイクルからの秒数で、0.5 s 刻みが区別できる
    assert starts[:4] == ["2", "2.5", "3", "3.5"]
    jumps = [line for line in lines if line.strip().startswith("t=")]
    assert len(jumps) == 2
    assert jumps[0].split()[:2] == ["t=", "2.5"]


def test_window_start_is_shown_in_milliseconds() -> None:
    n = 10
    fix = _fix([0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n)
    lines = filter_compare.format_windows(fix, 0.25, fix["t"][0])
    starts = [line.split()[0] for line in lines[1:-1]]
    assert starts == ["0", "0.25", "0.5", "0.75"]


def test_jump_times_keep_milliseconds() -> None:
    # 30 Hz の記録では 2.033 s のような時刻になる。0.1 s に丸めると --from / --to に渡したときにずれる
    fix = _fix([0.0, 0.4], [0.0] * 2, [0.0] * 2, [0.0] * 2)
    fix["t"] = [1000, 3033]
    lines = filter_compare.format_jumps(filter_compare.big_jumps(fix, 0.2, 1000), 0.2)
    assert "2.033 s" in lines[1]


def test_window_table_states_units() -> None:
    n = 4
    fix = _fix([0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n)
    note = filter_compare.format_windows(fix, 1.0, fix["t"][0])[-1]
    assert "平均 x / y はメートル" in note
    assert "ミリメートル" in note

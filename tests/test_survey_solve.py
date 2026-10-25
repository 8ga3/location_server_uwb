"""self-survey の座標推定 CLI (`tools/survey_solve.py`) のテスト。

既知の配置から測距のログを組み立て、推定した座標が元の配置に戻るかを確かめる。
ログの測距値はファームと同じく整数ミリメートルに丸めるので、雑音が無くても 0.5 mm までの誤差が残る。
"""

from __future__ import annotations

import itertools
import json
import math
import random
from pathlib import Path

import pytest

import anchor_cli
import survey_solve

# 1 番が 0 番から +X 方向、2 番が y > 0 にある配置 (ゲージの固定と同じ向き)
LAYOUT = {
    0x0100: (0.0, 0.0, 0.25),
    0x0101: (2.37, 0.0, 0.25),
    0x0102: (2.37, 1.75, 0.80),
    0x0103: (0.0, 1.75, 0.25),
    0x0104: (1.20, 2.60, 1.10),
}


def _log(
    layout: dict[int, tuple[float, float, float]],
    *,
    bias_m: float = 0.0,
    noise_m: float = 0.0,
    samples: int = 5,
    seed: int = 1,
    skip: set[tuple[int, int]] | None = None,
) -> list[str]:
    rng = random.Random(seed)
    ids = sorted(layout)
    lines = [
        "TEST_START,result=OK",
        "SURVEY_START,coordinator=0x0100,samples=5,anchors=" + ";".join(f"0x{i:04X}" for i in ids),
    ]
    for i in ids:
        for j in ids:
            if i == j:
                continue
            if skip and (i, j) in skip:
                lines.append(f"SURVEY_PAIR,i=0x{i:04X},j=0x{j:04X},status=NO_ACK,try=0,ok=0,error=TIMEOUT")
                continue
            dist = math.dist(layout[i], layout[j]) + bias_m
            mm = [round((dist + rng.gauss(0, noise_m)) * 1000) for _ in range(samples)]
            values = ";".join(str(v) for v in mm)
            # pio device monitor のタイムスタンプが行頭に付いていても読めること
            lines.append(
                f"12:00:00.000 > SURVEY_PAIR,i=0x{i:04X},j=0x{j:04X},status=OK,try={samples},ok={samples},"
                f"median_mm=0,mm={values}"
            )
    lines.append("SURVEY_END,pairs=20,ok_pairs=20,elapsed_ms=1000")
    return lines


def _solve(
    layout: dict[int, tuple[float, float, float]], lines: list[str], **kwargs: float | None
) -> survey_solve.Solution:
    pairs = survey_solve.parse_log(lines)
    edges = survey_solve.build_edges(pairs, set())
    z = {anchor_id: p[2] for anchor_id, p in layout.items()}
    return survey_solve.solve(edges, z, **kwargs)


def _assert_same_shape(
    layout: dict[int, tuple[float, float, float]], solution: survey_solve.Solution
) -> None:
    """推定した水平位置の全ての組の距離が、元の配置と一致することを確かめる (ゲージに依らない)。"""
    xy = dict(zip(solution.ids, solution.xy, strict=True))
    for a, b in itertools.combinations(sorted(layout), 2):
        expected = math.dist(layout[a][:2], layout[b][:2])
        assert math.dist(xy[a], xy[b]) == pytest.approx(expected, abs=2e-3), (hex(a), hex(b))


def _reference_xy(layout: dict[int, tuple[float, float, float]]) -> dict[int, tuple[float, float]]:
    return {anchor_id: (p[0], p[1]) for anchor_id, p in layout.items()}


def test_recovers_layout_and_bias_without_noise() -> None:
    solution = _solve(LAYOUT, _log(LAYOUT, bias_m=0.08))

    assert solution.bias_estimated
    assert solution.bias_m == pytest.approx(0.08, abs=1e-3)
    assert solution.redundancy == 10 - 8
    assert solution.rms_m < 1e-3
    _assert_same_shape(LAYOUT, solution)
    # ゲージ: ID 順に 0x0100 を原点、0x0101 を +X 軸上、0x0102 を y > 0 に置くので、元の配置と同じ向きになる
    assert solution.gauge_ids == (0x0100, 0x0101, 0x0102)
    for anchor_id, (x, y) in zip(solution.ids, solution.xy, strict=True):
        assert (x, y) == pytest.approx(LAYOUT[anchor_id][:2], abs=1e-3)


def test_noise_stays_small_and_alignment_matches_reference() -> None:
    solution = _solve(LAYOUT, _log(LAYOUT, bias_m=0.05, noise_m=0.02, samples=20))
    alignment = survey_solve.align(solution, _reference_xy(LAYOUT))

    assert alignment is not None
    assert not alignment.mirrored
    assert alignment.rms_m < 0.03
    assert solution.bias_m == pytest.approx(0.05, abs=0.02)


def test_alignment_detects_mirror() -> None:
    # 手測りの座標系が y を反転した向きなら、重ね合わせは鏡映を選ぶ
    mirrored = {anchor_id: (x, -y, z) for anchor_id, (x, y, z) in LAYOUT.items()}
    solution = _solve(LAYOUT, _log(LAYOUT))
    alignment = survey_solve.align(solution, _reference_xy(mirrored))

    assert alignment is not None
    assert alignment.mirrored
    assert alignment.rms_m < 1e-3


def test_four_anchors_with_bias_has_zero_redundancy() -> None:
    layout = {k: v for k, v in LAYOUT.items() if k != 0x0104}
    solution = _solve(layout, _log(layout, bias_m=0.03, noise_m=0.01, samples=20))

    assert solution.redundancy == 0
    # 冗長度 0 では測距の誤差をすべて吸収し、残差は 0 になる
    assert solution.rms_m < 1e-6
    report = survey_solve.format_report([], [], solution, None, {})
    assert any("冗長度が 0" in line for line in report)


def test_fixed_bias_leaves_redundancy_with_four_anchors() -> None:
    layout = {k: v for k, v in LAYOUT.items() if k != 0x0104}
    solution = _solve(layout, _log(layout, bias_m=0.03), fixed_bias_m=0.03)

    assert not solution.bias_estimated
    assert solution.redundancy == 1
    assert solution.rms_m < 1e-3


def test_three_anchors_with_bias_is_rejected() -> None:
    layout = {k: LAYOUT[k] for k in (0x0100, 0x0101, 0x0102)}
    with pytest.raises(survey_solve.SurveyError, match="一意に決まりません"):
        _solve(layout, _log(layout))


def test_one_way_and_missing_pairs() -> None:
    # 0x0100 -> 0x0104 だけ欠けても、逆方向が残っていれば 1 本の距離として使う。
    # 0x0101 <-> 0x0103 は両方向とも欠ける
    skip = {(0x0100, 0x0104), (0x0101, 0x0103), (0x0103, 0x0101)}
    lines = _log(LAYOUT, bias_m=0.02, skip=skip)
    pairs = survey_solve.parse_log(lines)
    edges = survey_solve.build_edges(pairs, set())

    assert len(edges) == 9
    one_way = [e for e in edges if e.directions == 1]
    assert [(e.a, e.b) for e in one_way] == [(0x0100, 0x0104)]
    solution = survey_solve.solve(edges, {k: v[2] for k, v in LAYOUT.items()})
    assert solution.rms_m < 1e-3
    assert solution.bias_m == pytest.approx(0.02, abs=1e-3)


def test_parse_uses_last_survey_only() -> None:
    old = _log(LAYOUT, bias_m=1.0)
    new = _log(LAYOUT, bias_m=0.0)
    pairs = survey_solve.parse_log(old + new)

    assert len(pairs) == len(LAYOUT) * (len(LAYOUT) - 1)
    solution = survey_solve.solve(
        survey_solve.build_edges(pairs, set()), {k: v[2] for k, v in LAYOUT.items()}
    )
    assert solution.bias_m == pytest.approx(0.0, abs=1e-3)


def test_exclude_removes_pair() -> None:
    pairs = survey_solve.parse_log(_log(LAYOUT))
    excluded = survey_solve._parse_exclude(["0x0100-0x0102", "257-0x0103"])
    edges = survey_solve.build_edges(pairs, excluded)

    keys = {(e.a, e.b) for e in edges}
    assert (0x0100, 0x0102) not in keys
    assert (0x0101, 0x0103) not in keys
    assert len(edges) == 8


def test_cli_writes_anchor_document(tmp_path: Path) -> None:
    log = tmp_path / "survey.log"
    log.write_text("\n".join(_log(LAYOUT, bias_m=0.04, noise_m=0.005, samples=10)) + "\n", encoding="utf-8")
    reference = tmp_path / "room.json"
    reference.write_text(
        json.dumps(
            {
                "note": "room",
                "anchors": [
                    {"id": f"0x{anchor_id:04X}", "x": x, "y": y, "z": z, "label": f"A{anchor_id & 0xF}"}
                    for anchor_id, (x, y, z) in LAYOUT.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "survey.json"

    args = survey_solve.build_parser().parse_args(
        [str(log), "--reference", str(reference), "--out", str(out), "--note", "survey test"]
    )
    report = survey_solve.run(args)

    document = json.loads(out.read_text(encoding="utf-8"))
    anchor_cli._check_document(document)
    assert document["source"] == "survey"
    assert document["note"] == "survey test"
    by_id = {a["id"]: a for a in document["anchors"]}
    for anchor_id, (x, y, z) in LAYOUT.items():
        entry = by_id[f"0x{anchor_id:04X}"]
        assert entry["x"] == pytest.approx(x, abs=0.02)
        assert entry["y"] == pytest.approx(y, abs=0.02)
        assert entry["z"] == z
        assert entry["label"] == f"A{anchor_id & 0xF}"
    assert any("手測りとの差の RMS" in line for line in report)


def test_cli_reports_missing_log_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "empty.log"
    log.write_text("TEST_START,result=OK\n", encoding="utf-8")

    assert survey_solve.main([str(log)]) == 1
    assert "SURVEY_PAIR 行が見つかりません" in capsys.readouterr().err


def _skip_both(pairs: list[tuple[int, int]]) -> set[tuple[int, int]]:
    return {p for a, b in pairs for p in ((a, b), (b, a))}


def test_anchor_with_single_edge_is_rejected() -> None:
    # 5 台の間の全 10 組と、6 台目への 1 組だけ。観測数の勘定は足りるが、6 台目の位置は円周上にしか決まらない
    layout = {**LAYOUT, 0x0105: (2.6, 2.4, 0.3)}
    skip = _skip_both([(anchor_id, 0x0105) for anchor_id in LAYOUT if anchor_id != 0x0100])
    with pytest.raises(survey_solve.SurveyError, match="0x0105"):
        _solve(layout, _log(layout, skip=skip))


def test_flexible_layout_is_rejected() -> None:
    # 4 台ずつの 2 つの塊を 2 本の測距だけでつなぐ。どのアンカーも測距は 3 本以上あり、観測数も足りるが、
    # 2 つの塊の相対的な向きが 1 自由度だけ決まらない
    layout = {
        0x0100: (0.0, 0.0, 0.25),
        0x0101: (1.5, 0.0, 0.25),
        0x0102: (1.6, 1.4, 0.25),
        0x0103: (0.1, 1.5, 0.25),
        0x0104: (3.5, 0.2, 0.25),
        0x0105: (5.0, 0.1, 0.25),
        0x0106: (5.1, 1.6, 0.25),
        0x0107: (3.4, 1.7, 0.25),
    }
    left, right = [0x0100, 0x0101, 0x0102, 0x0103], [0x0104, 0x0105, 0x0106, 0x0107]
    bridges = {frozenset((0x0101, 0x0104)), frozenset((0x0102, 0x0107))}
    skip = _skip_both([(a, b) for a in left for b in right if frozenset((a, b)) not in bridges])
    with pytest.raises(survey_solve.SurveyError, match="一意に決まりません"):
        _solve(layout, _log(layout, skip=skip), fixed_bias_m=0.0)


def test_anchor_with_two_edges_is_reported() -> None:
    skip = _skip_both([(0x0104, 0x0102), (0x0104, 0x0103)])
    solution = _solve(LAYOUT, _log(LAYOUT, skip=skip), fixed_bias_m=0.0)

    # 0x0104 は 0x0100 と 0x0101 だけと測っているので、その 2 台を結ぶ直線について折り返せる
    assert solution.flippable == [([0x0100, 0x0101], [0x0104])]
    report = survey_solve.format_report([], [], solution, None, {})
    assert any("0x0104 は、0x0100 と 0x0101 を結ぶ直線について折り返しても" in line for line in report)


@pytest.mark.parametrize(
    "reference_ids",
    [
        [0x0100, 0x0101],  # 共通のアンカーが 2 台
        [0x0100, 0x0101, 0x0104],  # 3 台あるが手測りの座標で一直線に並ぶ
    ],
)
def test_alignment_is_ambiguous_without_three_non_collinear_anchors(reference_ids: list[int]) -> None:
    layout = {**LAYOUT, 0x0104: (4.0, 0.0, 0.25)}
    solution = _solve(layout, _log(layout))
    reference = {anchor_id: layout[anchor_id][:2] for anchor_id in reference_ids}

    alignment = survey_solve.align(solution, reference)
    assert alignment is not None
    assert alignment.ambiguous
    report = survey_solve.format_report([], [], solution, alignment, {})
    assert any("--mirror" in line for line in report)
    with pytest.raises(survey_solve.SurveyError, match="--mirror"):
        survey_solve.build_document(solution, alignment, {}, None)

    # 向きを指定すれば書き出せる
    forced = survey_solve.align(solution, reference, mirror=False)
    assert forced is not None
    assert not forced.ambiguous
    assert not forced.mirrored
    assert forced.alternative_rms_m is None
    document = survey_solve.build_document(solution, forced, {}, None)
    by_id = {a["id"]: a for a in document["anchors"]}
    assert by_id["0x0102"]["y"] == pytest.approx(layout[0x0102][1], abs=1e-3)


def test_cli_refuses_ambiguous_mirror_and_accepts_explicit_choice(tmp_path: Path) -> None:
    log = tmp_path / "survey.log"
    log.write_text("\n".join(_log(LAYOUT)) + "\n", encoding="utf-8")
    reference = tmp_path / "room.json"
    reference.write_text(
        json.dumps(
            {
                "anchors": [
                    {
                        "id": f"0x{anchor_id:04X}",
                        "x": LAYOUT[anchor_id][0],
                        "y": LAYOUT[anchor_id][1],
                        "z": 0.25,
                    }
                    for anchor_id in (0x0100, 0x0101)
                ]
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "survey.json"

    assert survey_solve.main([str(log), "--reference", str(reference), "--out", str(out)]) == 1
    assert not out.exists()
    assert (
        survey_solve.main([str(log), "--reference", str(reference), "--out", str(out), "--mirror", "no"]) == 0
    )
    assert out.exists()


def _write_reference(path: Path, ids: list[int]) -> None:
    path.write_text(
        json.dumps(
            {
                "anchors": [
                    {"id": f"0x{i:04X}", "x": LAYOUT[i][0], "y": LAYOUT[i][1], "z": LAYOUT[i][2]} for i in ids
                ]
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("failed", [True, False])
def test_cli_refuses_anchor_without_measurements(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], failed: bool
) -> None:
    # 6 台目は SURVEY_START の一覧にあるが、全ての組が失敗した (failed) か、起床せず組が 1 つも無い
    layout = {**LAYOUT, 0x0105: (2.6, 2.4, 0.3)}
    lines = _log(layout, skip=_skip_both([(i, 0x0105) for i in LAYOUT]))
    if not failed:
        lines = [line for line in lines if "j=0x0105" not in line and "i=0x0105" not in line]
    log = tmp_path / "survey.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "survey.json"

    assert survey_solve.main([str(log)]) == 0
    assert "測距値が 1 本も無いアンカーがあります: 0x0105" in capsys.readouterr().out

    assert survey_solve.main([str(log), "--out", str(out)]) == 1
    captured = capsys.readouterr()
    assert not out.exists()
    assert "0x0105" in captured.err
    # 書き出さなくても、推定結果の表示は残す
    assert "残差 RMS" in captured.out


def test_cli_refuses_reference_without_overlap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "survey.log"
    log.write_text("\n".join(_log(LAYOUT)) + "\n", encoding="utf-8")
    reference = tmp_path / "room.json"
    _write_reference(reference, [0x0100])
    out = tmp_path / "survey.json"

    assert survey_solve.main([str(log), "--reference", str(reference)]) == 0
    assert "手測りの座標系へ重ねられません" in capsys.readouterr().out
    assert survey_solve.main([str(log), "--reference", str(reference), "--out", str(out)]) == 1
    assert not out.exists()
    assert "手測りの座標系へ重ねられません" in capsys.readouterr().err


def test_parse_survey_anchors_uses_last_start() -> None:
    lines = [
        "SURVEY_START,coordinator=0x0100,samples=20,anchors=0x0100;0x0101",
        "12:00:00 > SURVEY_START,coordinator=0x0100,samples=20,anchors=0x0100;0x0101;0x0102",
    ]
    assert survey_solve.parse_survey_anchors(lines) == {0x0100, 0x0101, 0x0102}


def test_gauge_does_not_depend_on_id_order() -> None:
    # ID 順の先頭 2 台が真上と真下 (水平位置が同じ) でも解ける
    layout = {
        0x0100: (0.0, 0.0, 0.0),
        0x0101: (0.0, 0.0, 2.4),
        0x0102: (0.44, 0.0, 0.0),
        0x0103: (0.0, 1.17, 0.0),
    }
    solution = _solve(layout, _log(layout))

    assert solution.rms_m < 1e-3
    # 0x0101 は 0x0100 の真上にあるので +X 軸上の基準には使わず、ID 順で次の 0x0102 を使う
    assert solution.gauge_ids == (0x0100, 0x0102, 0x0103)
    _assert_same_shape(layout, solution)


def test_cli_reports_output_errors_with_estimate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = tmp_path / "survey.log"
    log.write_text("\n".join(_log(LAYOUT)) + "\n", encoding="utf-8")

    # note が長すぎて座標表の検証に通らない
    assert survey_solve.main([str(log), "--out", str(tmp_path / "a.json"), "--note", "x" * 201]) == 1
    captured = capsys.readouterr()
    assert "座標表が正しくないので書き出しません" in captured.err
    assert "残差 RMS" in captured.out

    # 書き込み先のディレクトリが無い
    assert survey_solve.main([str(log), "--out", str(tmp_path / "missing" / "a.json")]) == 1
    captured = capsys.readouterr()
    assert "に書き込めません" in captured.err
    assert "残差 RMS" in captured.out


def _two_cluster_log() -> tuple[dict[int, tuple[float, float, float]], list[str]]:
    # 6 台で {0, 1, 2, 3} と {0, 1, 4, 5} の内部だけを測る。どのアンカーも測距は 3 本以上で JᵀJ も
    # 満ランクだが、4 と 5 を 0-1 の直線について折り返しても測距は変わらない
    layout = {
        0x0100: (0.0, 0.0, 0.25),
        0x0101: (2.0, 0.0, 0.25),
        0x0102: (2.2, 1.5, 0.25),
        0x0103: (0.1, 1.6, 0.25),
        0x0104: (2.3, -1.4, 0.25),
        0x0105: (-0.2, -1.5, 0.25),
    }
    upper, lower = [0x0102, 0x0103], [0x0104, 0x0105]
    return layout, _log(layout, skip=_skip_both([(a, b) for a in upper for b in lower]))


def test_partial_flip_is_detected() -> None:
    layout, lines = _two_cluster_log()
    solution = _solve(layout, lines, fixed_bias_m=0.0)

    # 切り離される 2 つの群は同じ大きさなので、ID の小さい側 (0x0102, 0x0103) を基準にする
    assert solution.flippable == [([0x0100, 0x0101], [0x0104, 0x0105])]


def test_cli_refuses_partial_flip_without_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, lines = _two_cluster_log()
    log = tmp_path / "survey.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "survey.json"

    args = [str(log), "--fixed-bias-mm", "0", "--out", str(out)]
    assert survey_solve.main(args) == 1
    assert not out.exists()
    assert "--accept-nonunique" in capsys.readouterr().err
    assert survey_solve.main([*args, "--accept-nonunique"]) == 0
    assert out.exists()


def test_gauge_is_rechosen_when_bias_collapses_baseline() -> None:
    # 真上と真下の 2 台 (0x0100 と 0x0101) は、バイアスを除く前の距離では水平に離れて見えるので、
    # 最初は 0x0101 を +X 軸上の基準に選ぶ。推定が進んで 2 台が重なったら、基準を選び直して解き直す
    layout = {
        0x0100: (0.0, 0.0, 0.0),
        0x0101: (0.0, 0.0, 2.4),
        0x0102: (0.44, 0.0, 0.0),
        0x0103: (0.0, 1.17, 0.0),
    }
    solution = _solve(layout, _log(layout, bias_m=0.08))

    assert solution.rms_m < 1e-3
    assert solution.bias_m == pytest.approx(0.08, abs=1e-3)
    assert solution.gauge_ids[1] != 0x0101
    _assert_same_shape(layout, solution)


def test_critical_edges_are_detected_for_prism_layout() -> None:
    # 三角形 {0,1,2} と {3,4,5} の内部と 0-3・1-4・2-5 だけを測る (三角柱)。ランクも落ちず 2 台で分断も
    # されないが、どの 1 本を外しても形が崩れ、同じ測距を満たす別の形がある
    layout = {
        0x0100: (0.0, 0.0, 0.25),
        0x0101: (1.0, 0.1, 0.25),
        0x0102: (0.4, 0.9, 0.25),
        0x0103: (-0.8, -0.6, 0.25),
        0x0104: (2.1, -0.4, 0.25),
        0x0105: (0.6, 2.2, 0.25),
    }
    kept = {
        frozenset(p)
        for p in [
            (0x0100, 0x0101),
            (0x0101, 0x0102),
            (0x0100, 0x0102),
            (0x0103, 0x0104),
            (0x0104, 0x0105),
            (0x0103, 0x0105),
            (0x0100, 0x0103),
            (0x0101, 0x0104),
            (0x0102, 0x0105),
        ]
    }
    skip = _skip_both([(a, b) for a, b in itertools.combinations(layout, 2) if frozenset((a, b)) not in kept])
    solution = _solve(layout, _log(layout, skip=skip), fixed_bias_m=0.0)

    assert solution.flippable == []
    assert {frozenset(e) for e in solution.critical_edges} == kept


def test_full_survey_has_no_critical_edges() -> None:
    for layout in (LAYOUT, {k: v for k, v in LAYOUT.items() if k != 0x0104}):
        solution = _solve(layout, _log(layout))
        assert solution.critical_edges == []
        assert solution.flippable == []


def test_cli_refuses_critical_edges_without_confirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 5 台の全 10 組から 3 本を外すと 7 本 (= 2n - 3) になり、形は決まるが、どの 1 本を外しても崩れる
    skip = _skip_both([(0x0100, 0x0102), (0x0101, 0x0103), (0x0100, 0x0104)])
    lines = _log(LAYOUT, skip=skip)
    log = tmp_path / "survey.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "survey.json"

    solution = _solve(LAYOUT, lines, fixed_bias_m=0.0)
    assert solution.critical_edges
    args = [str(log), "--fixed-bias-mm", "0", "--out", str(out)]
    assert survey_solve.main(args) == 1
    assert "--accept-nonunique" in capsys.readouterr().err
    assert survey_solve.main([*args, "--accept-nonunique"]) == 0


def test_alignment_needs_horizontally_separated_reference() -> None:
    # 共通の基準点が真上と真下 (水平位置が同じ) なら回転が決まらないので、--mirror を指定しても重ねない
    layout = {
        0x0100: (0.0, 0.0, 0.0),
        0x0101: (0.0, 0.0, 2.4),
        0x0102: (0.44, 0.0, 0.0),
        0x0103: (0.0, 1.17, 0.0),
    }
    solution = _solve(layout, _log(layout))
    reference = {anchor_id: layout[anchor_id][:2] for anchor_id in (0x0100, 0x0101)}

    assert survey_solve.align(solution, reference) is None
    assert survey_solve.align(solution, reference, mirror=False) is None


def test_flip_across_collinear_anchors_is_detected() -> None:
    # 0x0100〜0x0102 が x 軸上に一直線に並び、0x0103 と 0x0104 はその 3 台とだけ測る (互いは欠測)。
    # 一般の位置なら形は一意に決まるが、この配置では 0x0104 を x 軸について折り返しても測距が変わらない
    layout = {
        0x0100: (0.0, 0.0, 0.25),
        0x0101: (3.0, 0.0, 0.25),
        0x0102: (-3.0, 0.0, 0.25),
        0x0103: (0.0, 4.0, 0.25),
        0x0104: (0.0, -2.25, 0.25),
    }
    solution = _solve(layout, _log(layout, skip=_skip_both([(0x0103, 0x0104)])))

    assert solution.critical_edges == []
    assert ([0x0100, 0x0101, 0x0102], [0x0104]) in solution.flippable


def test_degenerate_initial_layout_is_retried() -> None:
    # 水平位置がほぼ重なり高さだけが違う配置に負のバイアスが乗ると、全組の測距が高さの差より短くなり、
    # バイアス 0 の MDS では全点が重なる。バイアスの初期値を下げて試し直せば解ける
    layout = {
        0x0100: (0.0, 0.0, 0.0),
        0x0101: (0.2, 0.0, 0.6),
        0x0102: (0.2, 0.2, 1.2),
        0x0103: (0.0, 0.2, 1.8),
        0x0104: (0.1, 0.1, 2.4),
    }
    solution = _solve(layout, _log(layout, bias_m=-0.08))

    assert solution.bias_m == pytest.approx(-0.08, abs=0.01)
    assert solution.rms_m < 2e-3
    xy = dict(zip(solution.ids, solution.xy, strict=True))
    for a, b in itertools.combinations(sorted(layout), 2):
        assert math.dist(xy[a], xy[b]) == pytest.approx(math.dist(layout[a][:2], layout[b][:2]), abs=0.03)


def test_alignment_is_ambiguous_when_estimate_is_collinear() -> None:
    # 推定した側の共通アンカーが一直線に並ぶと、手測りの誤差で手測り側が少し曲がっていても、
    # 両方の向きの差は同じになり、4 台目を置く側を決められない
    layout = {
        0x0100: (0.0, 0.0, 0.25),
        0x0101: (0.6, 0.0, 0.25),
        0x0102: (1.5, 0.0, 0.25),
        0x0103: (0.0, 0.8, 0.25),
    }
    solution = _solve(layout, _log(layout))
    reference = {0x0100: (0.0, 0.0), 0x0101: (0.6, 0.12), 0x0102: (1.5, 0.0)}

    alignment = survey_solve.align(solution, reference)
    assert alignment is not None
    assert alignment.ambiguous
    assert alignment.alternative_rms_m == pytest.approx(alignment.rms_m, abs=1e-3)

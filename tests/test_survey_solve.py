"""self-survey の座標推定 CLI (`tools/survey_solve.py`) のテスト。

既知の配置から測距のログを組み立て、推定した座標が元の配置に戻るかを確かめる。
ログの測距値はファームと同じく整数ミリメートルに丸めるので、雑音が無くても 0.5 mm までの誤差が残る。
"""

from __future__ import annotations

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


def _reference_xy(layout: dict[int, tuple[float, float, float]]) -> dict[int, tuple[float, float]]:
    return {anchor_id: (p[0], p[1]) for anchor_id, p in layout.items()}


def test_recovers_layout_and_bias_without_noise() -> None:
    solution = _solve(LAYOUT, _log(LAYOUT, bias_m=0.08))

    assert solution.bias_estimated
    assert solution.bias_m == pytest.approx(0.08, abs=1e-3)
    assert solution.redundancy == 10 - 8
    assert solution.rms_m < 1e-3
    # ゲージの固定 (0 番が原点、1 番が +X 軸上、2 番が y > 0) は元の配置と同じ向き
    for anchor_id, (x, y) in zip(solution.ids, solution.xy, strict=True):
        assert x == pytest.approx(LAYOUT[anchor_id][0], abs=1e-3)
        assert y == pytest.approx(LAYOUT[anchor_id][1], abs=1e-3)


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

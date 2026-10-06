#!/usr/bin/env python3
"""アンカー間の相互測距 (self-survey) のログからアンカー座標を推定する CLI。

コーディネータにしたアンカーのシリアル出力 (`SURVEY_PAIR` 行) を読み、古典的 MDS で初期解を作り、
共通バイアス `b` を含む非線形最小二乗で精密化する。手順は
`doc/multi-anchor-positioning-design.md` の 4 章を参照。

水平位置 (x, y) だけを推定し、高さ (z) は手測りの値を使う (4.4)。測距値は 3 次元の距離なので、
残差は高さの差を含めた距離で計算する。

追加の依存なしで動かすため、固有値分解と最小二乗は標準ライブラリだけで実装している。
未知数は多くても数十個なので、速度は問題にならない。

使用例:

    pio device monitor | tee survey.log      # ファーム側。コーディネータで `survey 256 257 258 259`
    python tools/survey_solve.py survey.log --reference rooms/room-1.json --out survey.json
    python tools/anchor_cli.py apply survey.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anchor_cli
from filter_compare import _left, _right

# SURVEY_PAIR 行。`pio device monitor` のタイムスタンプなど、行頭に何か付いていてもよい
_PAIR_PATTERN = re.compile(r"SURVEY_PAIR,(?P<fields>\S+)")
_START_PATTERN = re.compile(r"SURVEY_START,")
_EXCLUDE_PATTERN = re.compile(r"(?P<a>[0-9a-fA-Fx]+)-(?P<b>[0-9a-fA-Fx]+)")

# 最小二乗の反復の打ち切り
_MAX_ITERATIONS = 200
_STEP_TOLERANCE_M = 1e-9


class SurveyError(ValueError):
    """ログや入力から座標を推定できない場合に送出する。"""


@dataclass
class PairSamples:
    """1 方向 (i が要求側、j が応答側) の測距値。単位はメートル。"""

    initiator: int
    responder: int
    status: str
    samples_m: list[float]

    @property
    def median_m(self) -> float:
        return statistics.median(self.samples_m)


@dataclass
class Edge:
    """両方向をまとめた 1 組の距離。"""

    a: int
    b: int
    distance_m: float
    # 両方向の中央値の差 (片方向しか無ければ None)。アンテナ遅延の非対称や遮蔽の目安
    asymmetry_m: float | None
    directions: int


@dataclass
class Solution:
    ids: list[int]
    # 推定した水平位置。ゲージは ids[0] を原点、ids[1] を +X 軸上、ids[2] を y > 0 に固定する
    xy: list[tuple[float, float]]
    z: list[float]
    bias_m: float
    bias_estimated: bool
    residuals_m: dict[tuple[int, int], float]
    redundancy: int
    iterations: int
    mds_eigenvalues: list[float]
    rms_m: float = field(init=False)

    def __post_init__(self) -> None:
        values = list(self.residuals_m.values())
        self.rms_m = math.sqrt(sum(r * r for r in values) / len(values)) if values else 0.0


@dataclass
class Alignment:
    """推定した座標を手測りの座標系へ重ねた結果。"""

    xy: list[tuple[float, float]]
    mirrored: bool
    errors_m: list[float]
    rms_m: float


# ---------------------------------------------------------------------------
# ログの読み込み


def _parse_id(text: str) -> int:
    return int(text, 16) if text.lower().startswith("0x") else int(text, 10)


def parse_log(lines: list[str]) -> list[PairSamples]:
    """ログから最後の survey (最後の SURVEY_START 以降) の SURVEY_PAIR 行を読む。"""
    start = 0
    for index, line in enumerate(lines):
        if _START_PATTERN.search(line):
            start = index
    pairs: list[PairSamples] = []
    for line in lines[start:]:
        match = _PAIR_PATTERN.search(line)
        if match is None:
            continue
        fields: dict[str, str] = {}
        for item in match.group("fields").split(","):
            key, sep, value = item.partition("=")
            if sep:
                fields[key] = value
        try:
            initiator = _parse_id(fields["i"])
            responder = _parse_id(fields["j"])
            samples = [int(v) / 1000 for v in fields["mm"].split(";")] if fields.get("mm") else []
        except (KeyError, ValueError) as exc:
            raise SurveyError(f"SURVEY_PAIR 行を解釈できません: {line.strip()}") from exc
        pairs.append(PairSamples(initiator, responder, fields.get("status", ""), samples))
    return pairs


def build_edges(pairs: list[PairSamples], excluded: set[frozenset[int]]) -> list[Edge]:
    """両方向の中央値を平均して 1 組 1 本の距離にする (4.2 のフェーズ 1)。"""
    by_pair: dict[frozenset[int], list[PairSamples]] = {}
    for pair in pairs:
        if not pair.samples_m or pair.initiator == pair.responder:
            continue
        key = frozenset((pair.initiator, pair.responder))
        if key in excluded:
            continue
        by_pair.setdefault(key, []).append(pair)

    edges: list[Edge] = []
    for key, items in by_pair.items():
        a, b = sorted(key)
        # 同じ方向が複数あれば (ログを連結した場合など) 測距値をまとめる
        forward = [s for p in items if p.initiator == a for s in p.samples_m]
        backward = [s for p in items if p.initiator == b for s in p.samples_m]
        medians = [statistics.median(v) for v in (forward, backward) if v]
        asymmetry = abs(medians[0] - medians[1]) if len(medians) == 2 else None
        edges.append(Edge(a, b, sum(medians) / len(medians), asymmetry, len(medians)))
    edges.sort(key=lambda e: (e.a, e.b))
    return edges


# ---------------------------------------------------------------------------
# 線形代数 (小さい密行列だけを扱う)


def _jacobi_eigen(matrix: list[list[float]]) -> tuple[list[float], list[list[float]]]:
    """対称行列の固有値と固有ベクトル (列) を Jacobi 法で求め、固有値の大きい順に返す。"""
    n = len(matrix)
    a = [row[:] for row in matrix]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(100):
        off = sum(a[i][j] ** 2 for i in range(n) for j in range(n) if i != j)
        if off < 1e-22:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if abs(a[p][q]) < 1e-30:
                    continue
                theta = (a[q][q] - a[p][p]) / (2 * a[p][q])
                t = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1))
                c = 1 / math.sqrt(t * t + 1)
                s = t * c
                for k in range(n):
                    akp, akq = a[k][p], a[k][q]
                    a[k][p] = c * akp - s * akq
                    a[k][q] = s * akp + c * akq
                for k in range(n):
                    apk, aqk = a[p][k], a[q][k]
                    a[p][k] = c * apk - s * aqk
                    a[q][k] = s * apk + c * aqk
                for k in range(n):
                    vkp, vkq = v[k][p], v[k][q]
                    v[k][p] = c * vkp - s * vkq
                    v[k][q] = s * vkp + c * vkq
    order = sorted(range(n), key=lambda i: a[i][i], reverse=True)
    values = [a[i][i] for i in order]
    vectors = [[v[k][i] for i in order] for k in range(n)]
    return values, vectors


def _solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """部分ピボット付きのガウスの消去法で matrix @ x = rhs を解く。"""
    n = len(rhs)
    m = [[*row, rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-15:
            raise SurveyError("正規方程式が特異です。配置が退化しているか、測距の組が足りません")
        m[col], m[pivot] = m[pivot], m[col]
        for row in range(col + 1, n):
            factor = m[row][col] / m[col][col]
            for k in range(col, n + 1):
                m[row][k] -= factor * m[col][k]
    x = [0.0] * n
    for row in range(n - 1, -1, -1):
        x[row] = (m[row][n] - sum(m[row][k] * x[k] for k in range(row + 1, n))) / m[row][row]
    return x


# ---------------------------------------------------------------------------
# 推定


def _gauge_fix(xy: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """0 番を原点、1 番を +X 軸上、2 番を y > 0 に置く (4.2 のフェーズ 4)。"""
    x0, y0 = xy[0]
    moved = [(x - x0, y - y0) for x, y in xy]
    angle = math.atan2(moved[1][1], moved[1][0])
    c, s = math.cos(-angle), math.sin(-angle)
    rotated = [(c * x - s * y, s * x + c * y) for x, y in moved]
    if len(rotated) > 2 and rotated[2][1] < 0:
        rotated = [(x, -y) for x, y in rotated]
    return rotated


def _horizontal(distance_m: float, dz_m: float) -> float:
    return math.sqrt(max(distance_m * distance_m - dz_m * dz_m, 0.0))


def _mds(
    ids: list[int], z: list[float], edges: list[Edge], bias_m: float
) -> tuple[list[tuple[float, float]], list[float]]:
    """古典的 MDS で水平位置の初期解を作る。足りない組は既知の距離の平均で埋める。"""
    n = len(ids)
    index = {anchor_id: i for i, anchor_id in enumerate(ids)}
    known = {(index[e.a], index[e.b]): e.distance_m - bias_m for e in edges}
    fill = sum(known.values()) / len(known)
    d2 = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = known.get((i, j), fill)
            h = _horizontal(d, z[i] - z[j])
            d2[i][j] = d2[j][i] = h * h
    # 二重中心化 B = -1/2 J D² J
    row_mean = [sum(row) / n for row in d2]
    total_mean = sum(row_mean) / n
    b = [[-0.5 * (d2[i][j] - row_mean[i] - row_mean[j] + total_mean) for j in range(n)] for i in range(n)]
    values, vectors = _jacobi_eigen(b)
    scale = [math.sqrt(max(values[k], 0.0)) for k in range(2)]
    xy = [(vectors[i][0] * scale[0], vectors[i][1] * scale[1]) for i in range(n)]
    return xy, values


def solve(
    edges: list[Edge],
    z_by_id: dict[int, float],
    *,
    fixed_bias_m: float | None = None,
) -> Solution:
    """MDS の初期解から、共通バイアスを含む非線形最小二乗 (Levenberg-Marquardt) で精密化する。

    残差は `r_ij = sqrt(|p_i - p_j|² + (z_i - z_j)²) + b - d_ij`。`fixed_bias_m` を渡すと `b` を
    その値に固定し、未知数から外す。
    """
    ids = sorted({e.a for e in edges} | {e.b for e in edges})
    n = len(ids)
    if n < 3:
        raise SurveyError(f"測距できたアンカーが {n} 台しかありません。3 台以上が必要です")
    index = {anchor_id: i for i, anchor_id in enumerate(ids)}
    z = [z_by_id.get(anchor_id, 0.0) for anchor_id in ids]

    estimate_bias = fixed_bias_m is None
    # 未知数: 1 番の x、2 番以降の (x, y)、(推定するなら) b
    unknowns = 1 + 2 * (n - 2) + (1 if estimate_bias else 0)
    redundancy = len(edges) - unknowns
    if redundancy < 0:
        raise SurveyError(
            f"観測 {len(edges)} 本に対して未知数が {unknowns} 個あり、解が一意に決まりません。"
            "測距の組を増やすか、--fixed-bias-mm でバイアスを固定してください"
        )

    initial_bias = 0.0 if fixed_bias_m is None else fixed_bias_m
    mds_xy, eigenvalues = _mds(ids, z, edges, initial_bias)
    xy = _gauge_fix(mds_xy)

    def pack(points: list[tuple[float, float]], bias: float) -> list[float]:
        theta = [points[1][0]]
        for x, y in points[2:]:
            theta += [x, y]
        if estimate_bias:
            theta.append(bias)
        return theta

    def unpack(theta: list[float]) -> tuple[list[tuple[float, float]], float]:
        points = [(0.0, 0.0), (theta[0], 0.0)]
        for k in range(n - 2):
            points.append((theta[1 + 2 * k], theta[2 + 2 * k]))
        bias = theta[-1] if estimate_bias else initial_bias
        return points, bias

    def x_index(i: int) -> int | None:
        return None if i == 0 else (0 if i == 1 else 1 + 2 * (i - 2))

    def y_index(i: int) -> int | None:
        return None if i < 2 else 2 + 2 * (i - 2)

    def evaluate(theta: list[float]) -> tuple[list[float], list[list[float]]]:
        points, bias = unpack(theta)
        residuals: list[float] = []
        jacobian: list[list[float]] = []
        for e in edges:
            i, j = index[e.a], index[e.b]
            dx = points[i][0] - points[j][0]
            dy = points[i][1] - points[j][1]
            dz = z[i] - z[j]
            dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            residuals.append(dist + bias - e.distance_m)
            row = [0.0] * unknowns
            if dist > 0:
                for k, sign in ((i, 1.0), (j, -1.0)):
                    xi, yi = x_index(k), y_index(k)
                    if xi is not None:
                        row[xi] += sign * dx / dist
                    if yi is not None:
                        row[yi] += sign * dy / dist
            if estimate_bias:
                row[-1] = 1.0
            jacobian.append(row)
        return residuals, jacobian

    theta = pack(xy, initial_bias)
    residuals, jacobian = evaluate(theta)
    cost = sum(r * r for r in residuals)
    damping = 1e-3
    iterations = 0
    while iterations < _MAX_ITERATIONS:
        iterations += 1
        jtj = [[sum(row[a] * row[b] for row in jacobian) for b in range(unknowns)] for a in range(unknowns)]
        jtr = [sum(row[a] * r for row, r in zip(jacobian, residuals, strict=True)) for a in range(unknowns)]
        improved = False
        while damping < 1e12:
            lhs = [
                [jtj[a][b] + (damping * jtj[a][a] if a == b else 0.0) for b in range(unknowns)]
                for a in range(unknowns)
            ]
            for a in range(unknowns):
                # 対角が 0 の未知数 (どの観測にも効かない) があっても解けるようにする
                lhs[a][a] += 1e-12
            step = _solve_linear(lhs, [-g for g in jtr])
            candidate = [t + s for t, s in zip(theta, step, strict=True)]
            cand_residuals, cand_jacobian = evaluate(candidate)
            cand_cost = sum(r * r for r in cand_residuals)
            if cand_cost <= cost:
                theta, residuals, jacobian, cost = candidate, cand_residuals, cand_jacobian, cand_cost
                damping = max(damping / 10, 1e-12)
                improved = True
                break
            damping *= 10
        if not improved or max(abs(s) for s in step) < _STEP_TOLERANCE_M:
            break

    points, bias = unpack(theta)
    # 最小二乗の途中で鏡映が入れ替わることはないが、2 番の y の符号だけは揃え直す
    points = _gauge_fix(points)
    by_edge = {(e.a, e.b): r for e, r in zip(edges, residuals, strict=True)}
    return Solution(ids, points, z, bias, estimate_bias, by_edge, redundancy, iterations, eigenvalues)


def align(solution: Solution, reference_xy: dict[int, tuple[float, float]]) -> Alignment | None:
    """推定した座標を、手測りの座標へ回転・平行移動 (必要なら鏡映) で重ねる。

    スケールは変えない。スケールのずれはバイアスの推定の誤りを表すので、重ね合わせで隠さない。
    共通のアンカーが 2 台未満なら None を返す。
    """
    common = [i for i, anchor_id in enumerate(solution.ids) if anchor_id in reference_xy]
    if len(common) < 2:
        return None
    best: Alignment | None = None
    for mirrored in (False, True):
        src = [(x, -y if mirrored else y) for x, y in solution.xy]
        dst = {i: reference_xy[solution.ids[i]] for i in common}
        sx = sum(src[i][0] for i in common) / len(common)
        sy = sum(src[i][1] for i in common) / len(common)
        dx = sum(dst[i][0] for i in common) / len(common)
        dy = sum(dst[i][1] for i in common) / len(common)
        # 2 次元の Kabsch: 回転角は相互共分散から直接求まる
        num = sum((src[i][0] - sx) * (dst[i][1] - dy) - (src[i][1] - sy) * (dst[i][0] - dx) for i in common)
        den = sum((src[i][0] - sx) * (dst[i][0] - dx) + (src[i][1] - sy) * (dst[i][1] - dy) for i in common)
        angle = math.atan2(num, den)
        c, s = math.cos(angle), math.sin(angle)
        moved = [(c * (x - sx) - s * (y - sy) + dx, s * (x - sx) + c * (y - sy) + dy) for x, y in src]
        errors = [math.dist(moved[i], dst[i]) for i in common]
        rms = math.sqrt(sum(e * e for e in errors) / len(errors))
        if best is None or rms < best.rms_m:
            full_errors = [math.dist(moved[i], dst[i]) if i in dst else math.nan for i in range(len(moved))]
            best = Alignment(moved, mirrored, full_errors, rms)
    return best


# ---------------------------------------------------------------------------
# 表示と出力


def _hex(anchor_id: int) -> str:
    return f"0x{anchor_id:04X}"


def _mm_text(value_m: float | None) -> str:
    return "-" if value_m is None or math.isnan(value_m) else f"{value_m * 1000:+.0f}"


def format_report(
    pairs: list[PairSamples],
    edges: list[Edge],
    solution: Solution,
    alignment: Alignment | None,
    reference: dict[int, dict[str, Any]],
) -> list[str]:
    lines: list[str] = []
    failed = [p for p in pairs if not p.samples_m]
    lines.append(f"測距の組: {len(pairs)} 方向 (測距値なし {len(failed)})、距離 {len(edges)} 本")
    for p in failed:
        lines.append(f"  測距値なし: {_hex(p.initiator)} -> {_hex(p.responder)} ({p.status})")
    one_way = [e for e in edges if e.directions == 1]
    for e in one_way:
        lines.append(f"  片方向だけ: {_hex(e.a)} - {_hex(e.b)}")

    lines.append("")
    eig = solution.mds_eigenvalues
    if len(eig) >= 3 and eig[1] > 0:
        # 4.2 のフェーズ 5 の固有値チェック。高さの差は MDS の前に除いているので、
        # λ3 が大きいのは測距の乱れ (遮蔽など) を表す
        lines.append(
            "MDS の固有値: "
            + ", ".join(f"{v:.4f}" for v in eig[:3])
            + f" (λ3/λ2 = {eig[2] / eig[1]:.3f}。大きければ測距が乱れている)"
        )
    bias_kind = "推定" if solution.bias_estimated else "固定"
    lines.append(f"バイアス b ({bias_kind}): {solution.bias_m * 1000:+.1f} mm")
    lines.append(f"冗長度: {solution.redundancy}、反復: {solution.iterations}")
    if solution.redundancy == 0:
        lines.append(
            "  冗長度が 0 なので残差は常に 0 になり、測距の誤りを残差から見つけられません。"
            "手測りの座標と突き合わせてください"
        )

    lines.append("")
    lines.append(
        "組ごとの値 (mm)。残差 = 推定座標での距離 + b - 測距、手測り差 = 測距 - b - 手測り座標での距離"
    )
    lines.append(
        _left("組", 15)
        + _right("測距", 9)
        + _right("両方向差", 10)
        + _right("残差", 8)
        + _right("手測り差", 10)
    )
    for e in edges:
        residual = solution.residuals_m[(e.a, e.b)]
        ref_diff: float | None = None
        if e.a in reference and e.b in reference:
            ra, rb = reference[e.a], reference[e.b]
            ref_dist = math.dist((ra["x"], ra["y"], ra["z"]), (rb["x"], rb["y"], rb["z"]))
            ref_diff = e.distance_m - solution.bias_m - ref_dist
        lines.append(
            f"{_hex(e.a)}-{_hex(e.b)}  {e.distance_m * 1000:>9.0f}{_mm_text(e.asymmetry_m):>10}"
            f"{_mm_text(residual):>8}{_mm_text(ref_diff):>10}"
        )
    lines.append(f"残差 RMS: {solution.rms_m * 1000:.1f} mm")

    lines.append("")
    if alignment is None:
        lines.append("座標 (m)。0 番を原点、1 番を +X 軸上、2 番を y > 0 に置いた座標系")
        lines.append("  2 番が実際に 0→1 の向きの左にあるか右にあるかは、目で確かめてください")
        coords = solution.xy
    else:
        mirror = "鏡映あり" if alignment.mirrored else "鏡映なし"
        lines.append(f"座標 (m)。手測りの座標系へ回転・平行移動で重ねた ({mirror})")
        coords = alignment.xy
    lines.append(f"{'ID':<8}{'x':>9}{'y':>9}{'z':>8}" + _right("手測りとの差 (mm)", 20))
    for i, anchor_id in enumerate(solution.ids):
        x, y = coords[i]
        err = _mm_text(alignment.errors_m[i]).lstrip("+") if alignment is not None else "-"
        lines.append(f"{_hex(anchor_id):<8}{x:>9.3f}{y:>9.3f}{solution.z[i]:>8.3f}{err:>20}")
    if alignment is not None:
        lines.append(f"手測りとの差の RMS: {alignment.rms_m * 1000:.1f} mm")
    return lines


def build_document(
    solution: Solution,
    alignment: Alignment | None,
    reference: dict[int, dict[str, Any]],
    note: str | None,
) -> dict[str, Any]:
    """`anchor_cli.py apply` と `POST /api/v1/anchors:bulk` に渡せる座標表を作る。"""
    coords = alignment.xy if alignment is not None else solution.xy
    anchors: list[dict[str, Any]] = []
    for i, anchor_id in enumerate(solution.ids):
        x, y = coords[i]
        entry: dict[str, Any] = {
            "id": _hex(anchor_id),
            "x": round(x, 3),
            "y": round(y, 3),
            "z": round(solution.z[i], 3),
        }
        label = reference.get(anchor_id, {}).get("label")
        if label is not None:
            entry["label"] = label
        anchors.append(entry)
    document: dict[str, Any] = {"source": "survey", "anchors": anchors}
    if note is not None:
        document["note"] = note
    return anchor_cli._check_document(document)


def _load_reference(path: Path) -> dict[int, dict[str, Any]]:
    try:
        document = anchor_cli._load_anchor_file(path)
    except anchor_cli.InputError as exc:
        raise SurveyError(str(exc)) from exc
    return {anchor_cli._anchor_id(a["id"]): a for a in document["anchors"]}


def _parse_exclude(values: list[str]) -> set[frozenset[int]]:
    excluded: set[frozenset[int]] = set()
    for value in values:
        match = _EXCLUDE_PATTERN.fullmatch(value)
        if match is None:
            raise SurveyError(f"--exclude は 0x0100-0x0102 の形式で書いてください: {value}")
        try:
            excluded.add(frozenset((_parse_id(match.group("a")), _parse_id(match.group("b")))))
        except ValueError as exc:
            raise SurveyError(f"--exclude の ID を解釈できません: {value}") from exc
    return excluded


def run(args: argparse.Namespace) -> list[str]:
    lines: list[str] = []
    for path in args.logs:
        try:
            lines += Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            raise SurveyError(f"{path} を読めません: {exc}") from exc
    pairs = parse_log(lines)
    if not pairs:
        raise SurveyError("SURVEY_PAIR 行が見つかりません")

    reference = _load_reference(Path(args.reference)) if args.reference else {}
    edges = build_edges(pairs, _parse_exclude(args.exclude))
    ids = sorted({e.a for e in edges} | {e.b for e in edges})
    z_by_id = {
        anchor_id: reference[anchor_id]["z"] if anchor_id in reference else args.z for anchor_id in ids
    }
    fixed_bias = None if args.fixed_bias_mm is None else args.fixed_bias_mm / 1000
    solution = solve(edges, z_by_id, fixed_bias_m=fixed_bias)
    reference_xy = {anchor_id: (a["x"], a["y"]) for anchor_id, a in reference.items()}
    alignment = align(solution, reference_xy) if reference else None

    report = format_report(pairs, edges, solution, alignment, reference)
    if args.out:
        document = build_document(solution, alignment, reference, args.note)
        Path(args.out).write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report.append("")
        report.append(f"{args.out} に座標表を書きました (source: survey)")
        report.append(f"  バイアス b ({solution.bias_m * 1000:+.0f} mm) は座標表に入りません")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="self-survey のログ (SURVEY_PAIR 行) からアンカー座標を推定する",
    )
    parser.add_argument("logs", nargs="+", help="コーディネータのシリアル出力を保存したファイル")
    parser.add_argument(
        "--reference",
        help="手測りの座標表 (anchor_cli.py apply と同じ形式)。高さ (z) をここから取り、推定結果と比べる",
    )
    parser.add_argument("--z", type=float, default=0.0, help="手測りの座標表に無いアンカーの高さ (m)。既定 0")
    parser.add_argument(
        "--fixed-bias-mm",
        type=float,
        help="バイアス b を推定せず、この値 (mm) に固定する。4 台以下で冗長度を残したいときに使う",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="ID-ID",
        help="計算から外す組 (例: 0x0100-0x0102)。繰り返し指定できる。残差の大きい組を外すのに使う",
    )
    parser.add_argument("--out", help="推定した座標表を書き出すファイル (source: survey)")
    parser.add_argument("--note", help="座標表の note")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        for line in run(args):
            print(line)
    except SurveyError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

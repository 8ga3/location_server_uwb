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

    # ファーム側。コーディネータのモニタで `survey 257 258 259` と打つ
    pio device monitor --echo -f send_on_enter -f default -f log2file
    python tools/survey_solve.py logs/device-monitor-*.log --reference rooms/room-1.json --out survey.json
    python tools/anchor_cli.py apply survey.json --dry-run
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
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
# 解での JᵀJ の最小固有値が最大固有値のこの割合以下なら、未知数のどれかが観測で決まっていないと見なす
_RANK_TOLERANCE = 1e-9
# 手測りの座標で、共通のアンカーの短軸方向の広がりが長軸方向のこの割合未満なら一直線に並ぶと見なし、
# 鏡映の有無を判定しない
_COLLINEAR_RATIO = 0.05
# ゲージの基準を ID 順に選ぶとき、最も離れた候補に対してこの割合未満しか離れていないアンカーは飛ばす
_GAUGE_MIN_RATIO = 0.3
# 手測りの座標系へ重ねるとき、共通のアンカーの手測りの水平位置がこれ以上離れた組が無ければ、回転を決められない
_MIN_REFERENCE_SPAN_M = 0.1
# 推定した座標でこの距離以内に一直線に並ぶアンカーは、その直線について他のアンカーを折り返せる境界とみなす
_FLIP_LINE_TOLERANCE_M = 0.1
# 鏡映の有無を手測りとの差で選ぶとき、逆の向きとの差の RMS がこれ以上離れていなければ判定しない。
# survey の座標の誤差 (数十 mm) 程度の違いは、向きを決める根拠にならない
_MIRROR_MARGIN_M = 0.05
# MDS の初期解が退化したときに、全組の水平距離が少なくともこれだけ正になるようバイアスの初期値を下げる
_BIAS_RETRY_MARGIN_M = 0.1


class SurveyError(ValueError):
    """ログや入力から座標を推定できない場合に送出する。

    推定はできたが座標表を書き出せない場合は、診断に使えるよう推定結果の表示を `report` に持たせる。
    """

    def __init__(self, message: str, report: list[str] | None = None) -> None:
        super().__init__(message)
        self.report = report


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
    # 推定した水平位置 (ids と同じ並び)。ゲージは gauge_ids の 1 台目を原点、2 台目を +X 軸上、
    # 3 台目を y > 0 に固定する
    xy: list[tuple[float, float]]
    z: list[float]
    bias_m: float
    bias_estimated: bool
    residuals_m: dict[tuple[int, int], float]
    redundancy: int
    iterations: int
    mds_eigenvalues: list[float]
    # 測距の組み合わせで向きが決まらないアンカーの群。(境界, 群) は、推定した座標で一直線に並ぶ境界の
    # アンカーを取り除くと、群が残りから切り離されることを表す。群をその直線について折り返しても測距は
    # 変わらないので、どちら側に置かれたかは初期解で決まる。_flippable_groups() を参照
    flippable: list[tuple[list[int], list[int]]] = field(default_factory=list)
    # ゲージの基準にしたアンカー (原点、+X 軸上、y > 0)。_choose_gauge() を参照
    gauge_ids: tuple[int, int, int] = (0, 0, 0)
    # 1 本でも欠けると形が崩れる測距の組 (冗長剛性が無い)。空でなければ、同じ測距を満たす別の形が
    # ありうる。_critical_edges() を参照
    critical_edges: list[tuple[int, int]] = field(default_factory=list)
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
    # 共通のアンカーが 3 台未満か一直線に並ぶため、鏡映の有無を手測りとの差で判定できなかった
    ambiguous: bool = False
    # 選ばなかった側 (鏡映の有無を逆にした場合) の差の RMS。--mirror で向きを指定したときは None
    alternative_rms_m: float | None = None


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


def parse_survey_anchors(lines: list[str]) -> set[int]:
    """最後の SURVEY_START 行の anchors (コーディネータが測距しようとしたアンカーの一覧) を読む。"""
    anchors: set[int] = set()
    for line in lines:
        match = _START_PATTERN.search(line)
        if match is None:
            continue
        anchors = set()
        for item in line[match.end() :].strip().split(","):
            key, _, value = item.partition("=")
            if key == "anchors" and value:
                try:
                    anchors = {_parse_id(v) for v in value.split(";")}
                except ValueError as exc:
                    raise SurveyError(f"SURVEY_START 行を解釈できません: {line.strip()}") from exc
    return anchors


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


def _choose_gauge(xy: list[tuple[float, float]]) -> tuple[int, int, int]:
    """ゲージの基準にする 3 台を MDS の初期解から選ぶ。

    原点は 0 番 (ID が最小のアンカー) とする。+X 軸上に置くアンカーと y > 0 に置くアンカーは、
    これまでどおり ID 順に 1 番、2 番を使い、部屋の座標の取り方 (0 番から 1 番の向きを X 軸にする) と
    揃える。ただし真上と真下のように水平位置がほぼ同じ 2 台を選ぶと回転が決まらず、位置を決められる
    データでもランクの検査で断ってしまう。そこで、候補のうち最も離れたものに対して離れ方が
    _GAUGE_MIN_RATIO 未満のアンカーは飛ばし、ID 順で次のアンカーを使う。+X 軸上の基準は原点からの
    水平距離、y > 0 の基準は原点と +X 軸上の基準を結ぶ直線からの距離で比べる。
    """
    n = len(xy)
    g0 = 0

    def pick(candidates: list[int], spread: list[float]) -> int:
        widest = max(spread)
        return next(k for k, d in zip(candidates, spread, strict=True) if d >= widest * _GAUGE_MIN_RATIO)

    first = [k for k in range(n) if k != g0]
    g1 = pick(first, [math.dist(xy[k], xy[g0]) for k in first])
    ax, ay = xy[g1][0] - xy[g0][0], xy[g1][1] - xy[g0][1]
    second = [k for k in range(n) if k not in (g0, g1)]
    g2 = pick(second, [abs(ax * (xy[k][1] - xy[g0][1]) - ay * (xy[k][0] - xy[g0][0])) for k in second])
    return g0, g1, g2


def _gauge_fix(xy: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """0 番目を原点、1 番目を +X 軸上、2 番目を y > 0 に置く (4.2 のフェーズ 4)。

    並びは solve() の内部の順 (ゲージの基準の 3 台が先頭) を前提とする。
    """
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


def _flippable_groups(
    ids: list[int], edges: list[Edge], xy: list[tuple[float, float]]
) -> list[tuple[list[int], list[int]]]:
    """取り除くと測距のつながりが分かれる、一直線に並んだアンカーの組 (境界) と、切り離される側の群を返す。

    群は境界の直線について折り返しても全ての測距が変わらない。JᵀJ のランクは落ちない (連続には動かせない)
    のでランクの検査では見つからず、全体の鏡映 (--mirror) とも別に決まらない。最も大きい側を基準とし、
    それ以外の群を返す。

    2 台は常に一直線に並ぶので、全ての 2 台の組を境界の候補にする (測距が 2 本だけのアンカーもここで
    見つかる)。3 台以上は、推定した座標 xy (ids と同じ並び) で _FLIP_LINE_TOLERANCE_M 以内に一直線に
    並ぶものを候補にする。こちらは測距のつながりだけでは決まらない、実際の配置に依る折り返しである
    (_critical_edges() の一般の位置での検査では見つからない)。
    """
    neighbors: dict[int, set[int]] = {anchor_id: set() for anchor_id in ids}
    for e in edges:
        neighbors[e.a].add(e.b)
        neighbors[e.b].add(e.a)

    separators: set[frozenset[int]] = set()
    for a, b in itertools.combinations(range(len(ids)), 2):
        separators.add(frozenset((ids[a], ids[b])))
        length = math.dist(xy[a], xy[b])
        if length < _FLIP_LINE_TOLERANCE_M:
            continue
        ux, uy = (xy[b][0] - xy[a][0]) / length, (xy[b][1] - xy[a][1]) / length
        on_line = [
            ids[k]
            for k in range(len(ids))
            if abs(ux * (xy[k][1] - xy[a][1]) - uy * (xy[k][0] - xy[a][0])) <= _FLIP_LINE_TOLERANCE_M
        ]
        if len(on_line) > 2:
            separators.add(frozenset(on_line))

    groups: list[tuple[list[int], list[int]]] = []
    for separator in sorted(separators, key=lambda sep: (len(sep), sorted(sep))):
        unseen = {anchor_id for anchor_id in ids if anchor_id not in separator}
        components: list[list[int]] = []
        while unseen:
            stack = [unseen.pop()]
            component = []
            while stack:
                node = stack.pop()
                component.append(node)
                for other in neighbors[node]:
                    if other in unseen:
                        unseen.remove(other)
                        stack.append(other)
            components.append(sorted(component))
        components.sort(key=lambda c: (-len(c), c))
        groups += [(sorted(separator), component) for component in components[1:]]
    return groups


def _critical_edges(ids: list[int], edges: list[Edge]) -> list[tuple[int, int]]:
    """1 本でも欠けると形が崩れる測距の組を返す (アンカーが 4 台以上のとき)。

    2 次元で測距から形が一意に決まる (大域剛性) には、測距のつながりが 3 連結で、かつどの 1 本を
    外しても形が崩れない (冗長剛性) ことが要る (Jackson-Jordán)。3 連結でない場合は _flippable_groups()
    が見つけるので、ここでは冗長剛性を調べる。例えば三角形 2 つを 3 本でつないだ 6 台は、ランクも
    落ちず 2 台で分断もされないが、同じ測距を満たす別の形がある。

    測距の組が欠けて形が崩れるかは配置によらない性質 (一般の位置では) なので、乱数で置いた座標で
    剛性行列 R を作って調べる。組 k を外しても階数が落ちないのは、R の行の一次従属 (自己応力) に
    組 k が加わる場合だけなので、Rᵀ の簡約行階段形から 1 回で求まる。
    """
    n = len(ids)
    if n < 4:
        return []
    rng = random.Random(0)
    position = {anchor_id: (rng.random(), rng.random()) for anchor_id in ids}
    column = {anchor_id: 2 * k for k, anchor_id in enumerate(ids)}
    # Rᵀ: 行が座標 (2n)、列が測距の組
    m = len(edges)
    rt = [[0.0] * m for _ in range(2 * n)]
    for k, e in enumerate(edges):
        dx = position[e.a][0] - position[e.b][0]
        dy = position[e.a][1] - position[e.b][1]
        rt[column[e.a]][k], rt[column[e.a] + 1][k] = dx, dy
        rt[column[e.b]][k], rt[column[e.b] + 1][k] = -dx, -dy
    pivots: list[tuple[int, int]] = []  # (行, 列)
    row = 0
    for col in range(m):
        best = max(range(row, 2 * n), key=lambda r: abs(rt[r][col]), default=None)
        if best is None or abs(rt[best][col]) < 1e-9:
            continue
        rt[row], rt[best] = rt[best], rt[row]
        pivot = rt[row][col]
        rt[row] = [value / pivot for value in rt[row]]
        for other in range(2 * n):
            if other != row and rt[other][col] != 0.0:
                factor = rt[other][col]
                rt[other] = [a - factor * b for a, b in zip(rt[other], rt[row], strict=True)]
        pivots.append((row, col))
        row += 1
        if row == 2 * n:
            break
    if len(pivots) < 2 * n - 3:
        # そもそも形が決まらない。solve() のランクの検査で断るので、ここでは挙げない
        return []
    free = [col for col in range(m) if col not in {c for _, c in pivots}]
    # 自由な列は自己応力の基底に必ず入るので、外しても形は崩れない。主元の列は、どれかの自由な列との
    # 係数が 0 でなければ自己応力に加わる
    return [(edges[col].a, edges[col].b) for r, col in pivots if all(abs(rt[r][f]) < 1e-9 for f in free)]


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
    z = [z_by_id.get(anchor_id, 0.0) for anchor_id in ids]

    # 測距が 1 本以下のアンカーは、位置が円周上 (または全く) 決まらない。観測数の勘定が足りていても
    # 任意の座標を書き出してしまうので、先に断る
    degree = {anchor_id: 0 for anchor_id in ids}
    for e in edges:
        degree[e.a] += 1
        degree[e.b] += 1
    loose = [anchor_id for anchor_id in ids if degree[anchor_id] < 2]
    if loose:
        names = ", ".join(f"0x{anchor_id:04X}" for anchor_id in loose)
        raise SurveyError(
            f"測距が 1 本以下のアンカーがあり、位置が決まりません: {names}。"
            "そのアンカーとの測距を取り直すか、--exclude を見直してください"
        )
    critical = _critical_edges(ids, edges)

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

    def refine(
        gauge: tuple[int, int, int], start_xy: list[tuple[float, float]], start_bias: float
    ) -> tuple[list[tuple[float, float]], float, list[float], int, bool]:
        """gauge をゲージの基準にして最小二乗を解く。座標は ids の並びで受け渡す。

        内部ではゲージの基準の 3 台を先頭に並べ替えて解く。戻り値の最後は、解での JᵀJ が特異に
        近くない (未知数が全て観測で決まる) か。
        """
        order = list(gauge) + [k for k in range(n) if k not in gauge]
        index = {ids[o]: pos for pos, o in enumerate(order)}
        z_inner = [z[o] for o in order]

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
                dz = z_inner[i] - z_inner[j]
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

        theta = pack(_gauge_fix([start_xy[o] for o in order]), start_bias)
        residuals, jacobian = evaluate(theta)
        cost = sum(r * r for r in residuals)
        damping = 1e-3
        iterations = 0
        while iterations < _MAX_ITERATIONS:
            iterations += 1
            jtj = [
                [sum(row[a] * row[b] for row in jacobian) for b in range(unknowns)] for a in range(unknowns)
            ]
            jtr = [
                sum(row[a] * r for row, r in zip(jacobian, residuals, strict=True)) for a in range(unknowns)
            ]
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

        # 観測数が足りていても、2 つの塊が 2 本の測距だけでつながっている場合などは、解の近くで
        # 座標を動かしても残差が変わらない方向が残る。解での JᵀJ が特異に近いかを調べる
        jtj = [[sum(row[a] * row[b] for row in jacobian) for b in range(unknowns)] for a in range(unknowns)]
        jtj_eigen, _ = _jacobi_eigen(jtj)
        full_rank = jtj_eigen[-1] > jtj_eigen[0] * _RANK_TOLERANCE

        inner, bias = unpack(theta)
        # 最小二乗の途中で鏡映が入れ替わることはないが、y > 0 に置くアンカーの符号だけは揃え直す
        inner = _gauge_fix(inner)
        points = [(0.0, 0.0)] * n
        for pos, o in enumerate(order):
            points[o] = inner[pos]
        return points, bias, residuals, iterations, full_rank

    # 初期値の候補。まずバイアス 0 (固定するならその値) で試す。負のバイアスで測距が高さの差より短く
    # なると、MDS の水平距離が全て 0 になって全点が重なり、最小二乗がそこから動けない。その場合やランク
    # 不足になった場合は、全組の水平距離が正になるようバイアスの初期値を下げて試し直す
    zmap = dict(zip(ids, z, strict=True))
    starts = [initial_bias]
    if estimate_bias:
        lowered = min(e.distance_m - abs(zmap[e.a] - zmap[e.b]) for e in edges) - _BIAS_RETRY_MARGIN_M
        if lowered < initial_bias:
            starts.append(lowered)

    full_rank = False
    iterations = 0
    for attempt, start_bias in enumerate(starts):
        if attempt > 0:
            mds_xy, eigenvalues = _mds(ids, z, edges, start_bias)
        if max(math.dist(p, mds_xy[0]) for p in mds_xy) < 1e-9:
            continue
        gauge = _choose_gauge(mds_xy)
        points, bias, residuals, more, full_rank = refine(gauge, mds_xy, start_bias)
        iterations += more
        if not full_rank:
            # ゲージの基準はバイアスを推定する前の MDS の座標で選ぶ。バイアスがあると、実際には水平位置が
            # 重なる 2 台 (真上と真下) が離れて見え、それを基準に選ぶと、推定が進んで 2 台が重なったときに
            # 回転が決まらなくなる。推定後の座標で基準を選び直せる場合は、そこからもう一度だけ解く
            retry = _choose_gauge(points)
            if retry != gauge:
                gauge = retry
                points, bias, residuals, more, full_rank = refine(gauge, points, bias)
                iterations += more
        if full_rank:
            break
    if not full_rank:
        raise SurveyError(
            "測距の組み合わせでは座標が一意に決まりません (残差を変えずに動かせる向きがあります)。"
            "欠けている組を取り直すか、--exclude を見直してください"
        )

    by_edge = {(e.a, e.b): r for e, r in zip(edges, residuals, strict=True)}
    gauge_ids = (ids[gauge[0]], ids[gauge[1]], ids[gauge[2]])
    flippable = _flippable_groups(ids, edges, points) if n > 3 else []
    return Solution(
        ids,
        points,
        z,
        bias,
        estimate_bias,
        by_edge,
        redundancy,
        iterations,
        eigenvalues,
        flippable,
        gauge_ids,
        critical,
    )


def _is_collinear(points: list[tuple[float, float]]) -> bool:
    """点の並びの短軸方向の広がりが、長軸方向に比べて十分小さいか。2 点以下なら常に真。"""
    if len(points) < 3:
        return True
    mx = sum(x for x, _ in points) / len(points)
    my = sum(y for _, y in points) / len(points)
    sxx = sum((x - mx) ** 2 for x, _ in points)
    syy = sum((y - my) ** 2 for _, y in points)
    sxy = sum((x - mx) * (y - my) for x, y in points)
    # 2x2 の共分散行列の固有値
    half_trace = (sxx + syy) / 2
    root = math.sqrt(max(half_trace**2 - (sxx * syy - sxy * sxy), 0.0))
    major, minor = half_trace + root, max(half_trace - root, 0.0)
    return major == 0 or math.sqrt(minor / major) < _COLLINEAR_RATIO


def align(
    solution: Solution, reference_xy: dict[int, tuple[float, float]], *, mirror: bool | None = None
) -> Alignment | None:
    """推定した座標を、手測りの座標へ回転・平行移動 (必要なら鏡映) で重ねる。

    スケールは変えない。スケールのずれはバイアスの推定の誤りを表すので、重ね合わせで隠さない。
    手測りの水平位置が _MIN_REFERENCE_SPAN_M 以上離れた共通のアンカーの組が無い (共通のアンカーが
    2 台未満か、真上と真下のように重なる) ときは、回転を決められないので None を返す。鏡映の指定では
    この曖昧さは解けない。

    鏡映の有無は、`mirror` を渡せばその向きにし、渡さなければ手測りとの差の小さい方を選ぶ。
    ただし次の場合は判定できないので、`ambiguous` を立てる。
    - 共通のアンカーが 3 台未満か、手測りの座標か推定した座標のどちらかで一直線に並ぶ。推定した側が
      一直線なら、手測りの誤差で手測り側が少し曲がっていても、両方の向きの差は同じになる
    - 逆の向きとの差の RMS が _MIRROR_MARGIN_M 以上離れていない
    """
    common = [i for i, anchor_id in enumerate(solution.ids) if anchor_id in reference_xy]
    spans = [
        math.dist(reference_xy[solution.ids[a]], reference_xy[solution.ids[b]])
        for a, b in itertools.combinations(common, 2)
    ]
    if not spans or max(spans) < _MIN_REFERENCE_SPAN_M:
        return None
    candidates: list[Alignment] = []
    for mirrored in (False, True) if mirror is None else (mirror,):
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
        full_errors = [math.dist(moved[i], dst[i]) if i in dst else math.nan for i in range(len(moved))]
        candidates.append(Alignment(moved, mirrored, full_errors, rms))
    if mirror is not None:
        return candidates[0]
    best, other = sorted(candidates, key=lambda a: a.rms_m)
    best.alternative_rms_m = other.rms_m
    best.ambiguous = (
        _is_collinear([reference_xy[solution.ids[i]] for i in common])
        or _is_collinear([solution.xy[i] for i in common])
        or other.rms_m - best.rms_m < _MIRROR_MARGIN_M
    )
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
    if solution.critical_edges:
        names = ", ".join(f"{_hex(a)}-{_hex(b)}" for a, b in solution.critical_edges)
        lines.append(
            f"  次の組の測距は 1 本でも欠けると形が崩れ、同じ測距を満たす別の形がありえます: {names}。"
            "測距の組を足すか、形が正しいか目で確かめてください"
        )
    for separator, group in solution.flippable:
        names = ", ".join(_hex(anchor_id) for anchor_id in group)
        line = " と ".join(_hex(anchor_id) for anchor_id in separator)
        lines.append(
            f"  {names} は、{line} を結ぶ直線について折り返しても測距が変わりません。"
            "置かれた側が正しいか目で確かめてください"
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
        g0, g1, g2 = (_hex(anchor_id) for anchor_id in solution.gauge_ids)
        lines.append(f"座標 (m)。{g0} を原点、{g1} を +X 軸上、{g2} を y > 0 に置いた座標系")
        lines.append(f"  {g2} が実際に {g0}→{g1} の向きの左にあるか右にあるかは、目で確かめてください")
        coords = solution.xy
    else:
        mirror = "鏡映あり" if alignment.mirrored else "鏡映なし"
        lines.append(f"座標 (m)。手測りの座標系へ回転・平行移動で重ねた ({mirror})")
        if alignment.ambiguous:
            lines.append(
                "  手測りの座標表と共通のアンカーが 3 台未満か一直線に並ぶ (または逆の向きとの差が小さい)"
                "ので、鏡映の有無を判定できません。"
                "向きを目で確かめ、--mirror yes / no で指定してください"
            )
        elif alignment.alternative_rms_m is not None:
            lines.append(
                f"  鏡映を逆にした場合の手測りとの差の RMS: {alignment.alternative_rms_m * 1000:.1f} mm"
            )
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
    if alignment is not None and alignment.ambiguous:
        raise SurveyError(
            "鏡映の有無を判定できないので座標表を書き出しません。向きを目で確かめ、--mirror yes / no で"
            "指定してください"
        )
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
    # 測るはずだったのに測距値が 1 本も残らなかったアンカー (全ての組が失敗した、--exclude で全て外した、
    # 起床しなかった)。計算から消えるので、そのまま座標表を書き出すと apply でサーバーから削除されてしまう
    expected = parse_survey_anchors(lines) | {p.initiator for p in pairs} | {p.responder for p in pairs}
    missing = sorted(expected - set(ids))
    z_by_id = {
        anchor_id: reference[anchor_id]["z"] if anchor_id in reference else args.z for anchor_id in ids
    }
    fixed_bias = None if args.fixed_bias_mm is None else args.fixed_bias_mm / 1000
    solution = solve(edges, z_by_id, fixed_bias_m=fixed_bias)
    reference_xy = {anchor_id: (a["x"], a["y"]) for anchor_id, a in reference.items()}
    mirror = {"auto": None, "yes": True, "no": False}[args.mirror]
    alignment = align(solution, reference_xy, mirror=mirror) if reference else None

    report = format_report(pairs, edges, solution, alignment, reference)
    problems: list[str] = []
    if missing:
        names = ", ".join(_hex(anchor_id) for anchor_id in missing)
        problems.append(
            f"測距値が 1 本も無いアンカーがあります: {names}。座標表に入らないので、apply するとサーバーの"
            "構成から削除されます"
        )
    if reference and alignment is None:
        problems.append(
            "--reference の座標表と、水平位置の異なる共通のアンカーが 2 台未満なので、手測りの座標系へ"
            "重ねられません (回転が決まりません)。"
            f"上の座標は手測りとは別の座標系 ({_hex(solution.gauge_ids[0])} が原点) です"
        )
    if (solution.flippable or solution.critical_edges) and not args.accept_nonunique:
        problems.append(
            "測距の組み合わせでは配置が一意に決まりません (上の表示を参照)。--mirror では決まらないので、"
            "測距の組を足すか、置かれた形を目で確かめてから --accept-nonunique を付けてください"
        )
    if problems:
        report.append("")
        report += [f"注意: {problem}" for problem in problems]

    if args.out:
        if problems:
            raise SurveyError("座標表を書き出しません。" + " ".join(problems), report=report)
        # 座標表の検証 (note の長さなど) と書き込みに失敗しても、推定結果の表示は残す
        try:
            document = build_document(solution, alignment, reference, args.note)
            Path(args.out).write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except SurveyError as exc:
            exc.report = report
            raise
        except anchor_cli.InputError as exc:
            raise SurveyError(f"座標表が正しくないので書き出しません: {exc}", report=report) from exc
        except OSError as exc:
            raise SurveyError(f"{args.out} に書き込めません: {exc}", report=report) from exc
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
    parser.add_argument(
        "--mirror",
        choices=("auto", "yes", "no"),
        default="auto",
        help="手測りの座標系へ重ねるときの鏡映。auto は手測りとの差で決める。共通のアンカーが 3 台未満か"
        "一直線に並ぶときは判定できないので、yes / no で指定する",
    )
    parser.add_argument(
        "--accept-nonunique",
        action="store_true",
        help="測距の組み合わせで配置が一意に決まらない (一部を折り返せる、1 本欠けると形が崩れる) ときに、"
        "置かれた形を目で確かめたことを示す。付けないとその配置の座標表は書き出さない",
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
        for line in exc.report or []:
            print(line)
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

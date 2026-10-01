"""蓄積したテレメトリの参照 (設計文書 5.5)。

可視化ページの再生モードと、セッション一覧が使う。書き込みは行わないが、SQLite の接続は
構成配信・保存系と共有しているので、同じロックの下で読む。

大量のデータは `max_points` に従って等間隔に間引く。1 時間ぶんを素で返すとブラウザが固まるためで、
間引きは行番号の剰余で行い、何行おきに取ったか (`stride`) を結果に添える。末尾の行は必ず含める。

`seq` と `t_tag_ms` はパケットでは 32 ビットで折り返すので、並べる前にセッションごとに展開する。
`seq` はセッションの最小値と最大値の差が 2^31 以上なら折り返したとみなし、2^31 未満の値に 2^32 を足す
(1 セッションが 2^31 サイクル未満であることを前提とする。30 Hz で 2 年以上)。`t_tag_ms` は展開した `seq` の
順に並べ、直前より 2^31 を超えて小さくなった回数だけ 2^32 を足す。返す `t` / `seq` と、`from_ms` / `to_ms` で
指定する範囲は、この展開した値である。
"""

from __future__ import annotations

import math
import sqlite3
import threading
from dataclasses import dataclass, field

from location_server.columns import FixColumns, RangeTable
from location_server.ingest.packet import STATUS_OK


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """セッション行と、測位記録から数えた受信状況。"""

    id: int
    tag_id: int
    boot_id: int
    config_rev: int | None
    fw_version: str | None
    started_at: str
    last_seen_at: str
    note: str | None
    cycles: int
    fix_ok_cycles: int
    first_seq: int | None
    last_seq: int | None
    first_t_ms: int | None
    last_t_ms: int | None

    @property
    def expected_cycles(self) -> int:
        """最初と最後の `seq` から見て、届いているはずのサイクル数。"""
        if self.first_seq is None or self.last_seq is None:
            return 0
        return self.last_seq - self.first_seq + 1

    @property
    def missing_cycles(self) -> int:
        """`seq` の欠番の数。UDP で落ちたサイクルに当たる (測距の失敗とは別に数える)。"""
        return max(0, self.expected_cycles - self.cycles)

    @property
    def fix_rate(self) -> float | None:
        return None if self.cycles == 0 else self.fix_ok_cycles / self.cycles

    @property
    def loss_rate(self) -> float | None:
        expected = self.expected_cycles
        return None if expected == 0 else self.missing_cycles / expected


@dataclass(frozen=True, slots=True)
class Decimated[T]:
    """間引いた結果。`total` は間引く前の行数、`stride` は何行おきに取ったか。"""

    total: int
    stride: int
    data: T


@dataclass(slots=True)
class AnchorStats:
    """セッション内のアンカー 1 台ぶんの測距の集計。"""

    anchor_id: int
    samples: int = 0
    ok_samples: int = 0
    elapsed_sum: int = 0
    elapsed_count: int = 0
    elapsed_max: int | None = None
    distance_sum_mm: int = 0
    status_counts: dict[int, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float | None:
        return None if self.samples == 0 else self.ok_samples / self.samples

    @property
    def elapsed_mean(self) -> float | None:
        return None if self.elapsed_count == 0 else self.elapsed_sum / self.elapsed_count

    @property
    def distance_mean_mm(self) -> float | None:
        return None if self.ok_samples == 0 else self.distance_sum_mm / self.ok_samples


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """`GET /api/v1/sessions/{id}/summary` の元になる集計。"""

    session: SessionInfo
    residual_rms_mm: float | None
    period_mean_ms: float | None
    period_max_ms: int | None
    used_min: int | None
    used_max: int | None
    anchors: tuple[AnchorStats, ...]
    # タグ側のカルマンフィルタの集計。`kf_predicted_cycles` はフィルタが有効で、観測による更新が
    # 無かった (予測のみの) サイクル数。`kf_sigma_mean_mm` はフィルタが有効なサイクルの標準偏差の平均
    kf_ok_cycles: int
    kf_predicted_cycles: int
    kf_init_count: int
    kf_rejected_ranges: int
    kf_sigma_mean_mm: float | None


MAX_POINTS_MIN = 2
# ranges の全アンカー合計の点数の上限。max_points はアンカーごとの上限なので、anchor_id を省略して
# 全アンカーを取ると、プロトコル上 255 台まで持てるアンカーの数だけ応答が膨らむ。合計をこの点数に抑える
RANGES_TOTAL_POINTS_LIMIT = 20_000


def stride_for(total: int, max_points: int) -> int:
    """`total` 行を、末尾の行を含めて `max_points` 行以下へ間引くときの間隔。

    先頭から `stride` 行おきに取り、最後に末尾の行を足す。SQL 側 (`_STRIDE`) と同じ式である。
    """
    if max_points < MAX_POINTS_MIN:
        raise ValueError(f"max_points は {MAX_POINTS_MIN} 以上です: {max_points}")
    if total <= max_points:
        return 1
    return math.ceil((total - 1) / (max_points - 1))


def per_anchor_points(max_points: int, anchors: int) -> int:
    """全アンカー合計の予算を守るための、1 台あたりの点数の上限。"""
    if anchors <= 0:
        return max_points
    return max(MAX_POINTS_MIN, min(max_points, RANGES_TOTAL_POINTS_LIMIT // anchors))


_U31 = 1 << 31
_U32 = 1 << 32


def _normalized(table: str, scope: str, partition: str) -> str:
    """`seq_n` / `t_n` (折り返しを展開した seq と t_tag_ms) を足した CTE を作る。

    `scope` は `table` に掛ける WHERE 句、`partition` は展開を独立に行う単位 (セッション) である。
    続けて書く CTE からは `n` という名前で参照する。
    """
    return f"""
    n0 AS (
        SELECT *, max(seq) OVER p - min(seq) OVER p > {_U31 - 1} AS seq_wrapped
        FROM {table} WHERE {scope}
        WINDOW p AS (PARTITION BY {partition})
    ),
    n1 AS (
        SELECT *, seq + CASE WHEN seq_wrapped AND seq < {_U31} THEN {_U32} ELSE 0 END AS seq_n FROM n0
    ),
    n2 AS (
        SELECT *, CASE WHEN t_tag_ms < lag(t_tag_ms) OVER w - {_U31} THEN 1 ELSE 0 END AS t_wrap
        FROM n1 WINDOW w AS (PARTITION BY {partition} ORDER BY seq_n)
    ),
    n AS (
        SELECT *, t_tag_ms + {_U32} * sum(t_wrap) OVER (
            PARTITION BY {partition} ORDER BY seq_n ROWS UNBOUNDED PRECEDING
        ) AS t_n
        FROM n2
    )"""


# 間引きの間隔。stride_for() と同じ式で、:max_points は 2 以上
_STRIDE = "CASE WHEN total <= :max_points THEN 1 ELSE (total + :max_points - 3) / (:max_points - 1) END"


_SESSION_COLUMNS = """
    s.id, s.tag_id, s.boot_id, s.config_rev, s.fw_version, s.started_at, s.last_seen_at, s.note,
    coalesce(f.cycles, 0) AS cycles, coalesce(f.ok_cycles, 0) AS ok_cycles,
    f.first_seq, f.last_seq, f.first_t, f.last_t
"""

# セッションごとの受信状況。seq と t は折り返しを展開した値で数える
_FIX_AGGREGATE = """
    SELECT session_id, count(*) AS cycles, sum(ok) AS ok_cycles,
           min(seq_n) AS first_seq, max(seq_n) AS last_seq, min(t_n) AS first_t, max(t_n) AS last_t
    FROM n GROUP BY session_id
"""

# 時刻範囲の条件。`from_ms` / `to_ms` は折り返しを展開したタグの millis() で、None なら端を切らない
_TIME_RANGE = "(:from_ms IS NULL OR t_n >= :from_ms) AND (:to_ms IS NULL OR t_n <= :to_ms)"


def _session_info(row: sqlite3.Row) -> SessionInfo:
    return SessionInfo(
        id=row["id"],
        tag_id=row["tag_id"],
        boot_id=row["boot_id"],
        config_rev=row["config_rev"],
        fw_version=row["fw_version"],
        started_at=row["started_at"],
        last_seen_at=row["last_seen_at"],
        note=row["note"],
        cycles=row["cycles"],
        fix_ok_cycles=row["ok_cycles"],
        first_seq=row["first_seq"],
        last_seq=row["last_seq"],
        first_t_ms=row["first_t"],
        last_t_ms=row["last_t"],
    )


class QueryStore:
    """参照 API のための読み取り専用のアクセス経路。"""

    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self._conn = conn
        self._lock = lock

    def list_sessions(self, limit: int) -> list[SessionInfo]:
        """新しいセッションから `limit` 件を返す。"""
        with self._lock:
            rows = self._conn.execute(
                f"""
                WITH picked AS (SELECT * FROM session ORDER BY id DESC LIMIT :limit),
                {_normalized("position_fix", "session_id IN (SELECT id FROM picked)", "session_id")},
                f AS ({_FIX_AGGREGATE})
                SELECT {_SESSION_COLUMNS}
                FROM picked AS s LEFT JOIN f ON f.session_id = s.id
                ORDER BY s.id DESC
                """,
                {"limit": limit},
            ).fetchall()
        return [_session_info(row) for row in rows]

    def get_session(self, session_id: int) -> SessionInfo | None:
        with self._lock:
            return self._get_session(session_id)

    def track(
        self, session_id: int, *, from_ms: int | None, to_ms: int | None, max_points: int
    ) -> Decimated[FixColumns]:
        """測位結果を時刻順に、`max_points` 以下へ間引いて返す。"""
        params = {"session_id": session_id, "from_ms": from_ms, "to_ms": to_ms, "max_points": max_points}
        with self._lock:
            rows = self._conn.execute(
                f"""
                WITH {_normalized("position_fix", "session_id = :session_id", "session_id")},
                d AS (
                    -- 周期は間引く前に、セッション全体で隣り合う seq どうしから求める
                    SELECT *, CASE WHEN seq_n - lag(seq_n) OVER w = 1 THEN t_n - lag(t_n) OVER w END AS dt
                    FROM n WINDOW w AS (ORDER BY seq_n)
                ),
                r AS (
                    SELECT *, row_number() OVER (ORDER BY seq_n) - 1 AS rn, count(*) OVER () AS total
                    FROM d WHERE {_TIME_RANGE}
                ),
                k AS (SELECT *, {_STRIDE} AS stride FROM r)
                SELECT * FROM k WHERE rn % stride = 0 OR rn = total - 1 ORDER BY rn
                """,
                params,
            ).fetchall()
        fix = FixColumns()
        for row in rows:
            fix.add(
                t_ms=row["t_n"],
                seq=row["seq_n"],
                dt_ms=row["dt"],
                ok=bool(row["ok"]),
                x_mm=row["x_mm"],
                y_mm=row["y_mm"],
                z_mm=row["z_mm"],
                used=row["used_count"],
                resid_mm=row["residual_mm"],
                kf_ok=bool(row["kf_ok"]),
                kf_updated=bool(row["kf_updated"]),
                kf_init=bool(row["kf_init"]),
                kf_x_mm=row["kf_x_mm"],
                kf_y_mm=row["kf_y_mm"],
                kf_z_mm=row["kf_z_mm"],
                kf_sigma_mm=row["kf_sigma_mm"],
                kf_used=row["kf_used"],
                kf_rejected=row["kf_rejected"],
            )
        if not rows:
            return Decimated(total=0, stride=1, data=fix)
        return Decimated(total=rows[0]["total"], stride=rows[0]["stride"], data=fix)

    def ranges(
        self,
        session_id: int,
        *,
        anchor_id: int | None,
        from_ms: int | None,
        to_ms: int | None,
        max_points: int,
    ) -> dict[int, Decimated[RangeTable]]:
        """アンカーごとの測距を時刻順に返す。間引きはアンカーごとに行う。

        `anchor_id` を省略すると、そのセッションに記録のある全アンカーを返す。1 台あたりの点数は
        `max_points` と、全アンカー合計の予算 (`RANGES_TOTAL_POINTS_LIMIT`) を台数で割った値の小さいほう
        (最低 `MAX_POINTS_MIN`) とする。実際の台数 (10 台まで) なら `max_points` がそのまま使われ、
        `track` と同じ位置で間引かれる。
        """
        params = {
            "session_id": session_id,
            "anchor_id": anchor_id,
            "from_ms": from_ms,
            "to_ms": to_ms,
            "max_points": max_points,
        }
        with self._lock:
            anchors: int = self._conn.execute(
                """
                SELECT count(DISTINCT anchor_id) FROM range_sample
                WHERE session_id = :session_id AND (:anchor_id IS NULL OR anchor_id = :anchor_id)
                """,
                params,
            ).fetchone()[0]
            params["max_points"] = per_anchor_points(max_points, anchors)
            rows = self._conn.execute(
                f"""
                WITH {_normalized("position_fix", "session_id = :session_id", "session_id")},
                -- 折り返しの展開はセッション共通にし、測位記録で求めた値を seq で測距へ当てる。
                -- アンカーごとに展開すると、折り返した後に初めて現れたアンカーだけ 2^32 ずれるため。
                -- 測距行は同じサイクルの測位行と同じトランザクションで書くので、対応する行は必ずある
                m AS (
                    SELECT rs.anchor_id, rs.status, rs.distance_mm, rs.elapsed_ms, n.seq_n, n.t_n
                    FROM range_sample AS rs JOIN n ON n.seq = rs.seq
                    WHERE rs.session_id = :session_id AND (:anchor_id IS NULL OR rs.anchor_id = :anchor_id)
                ),
                r AS (
                    SELECT *, row_number() OVER (PARTITION BY anchor_id ORDER BY seq_n) - 1 AS rn,
                           count(*) OVER (PARTITION BY anchor_id) AS total
                    FROM m WHERE {_TIME_RANGE}
                ),
                k AS (SELECT *, {_STRIDE} AS stride FROM r)
                SELECT * FROM k WHERE rn % stride = 0 OR rn = total - 1 ORDER BY anchor_id, rn
                """,
                params,
            ).fetchall()
        result: dict[int, Decimated[RangeTable]] = {}
        for row in rows:
            aid: int = row["anchor_id"]
            entry = result.get(aid)
            if entry is None:
                entry = Decimated(total=row["total"], stride=row["stride"], data=RangeTable())
                result[aid] = entry
            entry.data.add(
                anchor_id=aid,
                t_ms=row["t_n"],
                seq=row["seq_n"],
                status=row["status"],
                distance_mm=row["distance_mm"],
                elapsed_ms=row["elapsed_ms"],
            )
        return result

    def summary(self, session_id: int) -> SessionSummary | None:
        """セッションの成功率・欠測率・残差 RMS・周期・フィルタの集計と、アンカーごとの測距の集計を返す。"""
        with self._lock:
            info = self._get_session(session_id)
            if info is None:
                return None
            fix_row = self._conn.execute(
                """
                SELECT avg(CASE WHEN ok THEN residual_mm * residual_mm END) AS resid_ms,
                       min(used_count) AS used_min, max(used_count) AS used_max,
                       coalesce(sum(kf_ok), 0) AS kf_ok_cycles,
                       coalesce(sum(kf_ok AND NOT kf_updated), 0) AS kf_predicted,
                       coalesce(sum(kf_ok AND kf_init), 0) AS kf_init,
                       coalesce(sum(kf_rejected), 0) AS kf_rejected,
                       avg(CASE WHEN kf_ok THEN kf_sigma_mm END) AS kf_sigma_mean
                FROM position_fix WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            # 周期は隣り合う seq どうしの時刻差で見る。欠番をまたぐ差は周期ではないので含めない
            period_row = self._conn.execute(
                f"""
                WITH {_normalized("position_fix", "session_id = :id", "session_id")},
                d AS (
                    SELECT t_n - lag(t_n) OVER w AS dt, seq_n - lag(seq_n) OVER w AS dseq
                    FROM n WINDOW w AS (ORDER BY seq_n)
                )
                SELECT avg(dt) AS mean_dt, max(dt) AS max_dt FROM d WHERE dseq = 1
                """,
                {"id": session_id},
            ).fetchone()
            range_rows = self._conn.execute(
                """
                SELECT anchor_id, status, count(*) AS n, sum(elapsed_ms) AS el_sum,
                       count(elapsed_ms) AS el_n, max(elapsed_ms) AS el_max, sum(distance_mm) AS d_sum
                FROM range_sample WHERE session_id = ?
                GROUP BY anchor_id, status ORDER BY anchor_id, status
                """,
                (session_id,),
            ).fetchall()
        anchors: dict[int, AnchorStats] = {}
        for row in range_rows:
            stats = anchors.setdefault(row["anchor_id"], AnchorStats(anchor_id=row["anchor_id"]))
            n: int = row["n"]
            stats.samples += n
            stats.status_counts[row["status"]] = n
            if row["status"] == STATUS_OK:
                stats.ok_samples += n
                stats.distance_sum_mm += row["d_sum"] or 0
            stats.elapsed_sum += row["el_sum"] or 0
            stats.elapsed_count += row["el_n"]
            if row["el_max"] is not None:
                stats.elapsed_max = max(stats.elapsed_max or 0, row["el_max"])
        mean_square = fix_row["resid_ms"]
        return SessionSummary(
            session=info,
            residual_rms_mm=None if mean_square is None else math.sqrt(mean_square),
            period_mean_ms=period_row["mean_dt"],
            period_max_ms=period_row["max_dt"],
            used_min=fix_row["used_min"],
            used_max=fix_row["used_max"],
            anchors=tuple(anchors[aid] for aid in sorted(anchors)),
            kf_ok_cycles=fix_row["kf_ok_cycles"],
            kf_predicted_cycles=fix_row["kf_predicted"],
            kf_init_count=fix_row["kf_init"],
            kf_rejected_ranges=fix_row["kf_rejected"],
            kf_sigma_mean_mm=fix_row["kf_sigma_mean"],
        )

    def _get_session(self, session_id: int) -> SessionInfo | None:
        row = self._conn.execute(
            f"""
            WITH {_normalized("position_fix", "session_id = :id", "session_id")},
            f AS ({_FIX_AGGREGATE})
            SELECT {_SESSION_COLUMNS}
            FROM session AS s LEFT JOIN f ON f.session_id = s.id
            WHERE s.id = :id
            """,
            {"id": session_id},
        ).fetchone()
        return None if row is None else _session_info(row)

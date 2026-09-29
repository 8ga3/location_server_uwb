"""ライブ配信と参照 API が共有する列指向データの組み立て。

可視化ページはライブと再生で同じ描画処理を使う (設計文書 8.1)。そのため WebSocket の
`snapshot` / `append` と参照 API の `track` / `ranges` は、同じ列名・同じ単位の列指向 JSON を返す。
受信直後のパケット (`CycleRecord`) からも DB の行からも、ここにある組み立て器を通して作る。

- `t` はタグの `millis()` (整数ミリ秒)、`seq` はサイクル通番
- `dt` は直前のサイクル (`seq` が 1 つ前) からの時刻差 [ms]。欠番をまたぐ場合と直前が無い場合は `null`。
  間引いた再生データでも本来の周期が見えるよう、間引く前に計算して載せる
- 長さ (`x` / `y` / `z` / `resid` / `d`) はメートル。API の JSON でのみメートルへ直す方針に従う
- 測位に失敗したサイクルは `x` / `y` / `z` を `null`、測距に失敗した記録は `d` を `null` にする。
  DB 側も同じ行を NULL で保存しているので、ライブと再生で見え方が変わらない
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from location_server.ingest.packet import STATUS_OK, CycleRecord
from location_server.units import format_hex_id, mm_to_meters


def _meters_or_none(value_mm: int | None) -> float | None:
    return None if value_mm is None else mm_to_meters(value_mm)


@dataclass(slots=True)
class FixColumns:
    """測位結果の列。1 サイクル 1 要素。"""

    t: list[int] = field(default_factory=list)
    seq: list[int] = field(default_factory=list)
    dt: list[int | None] = field(default_factory=list)
    x: list[float | None] = field(default_factory=list)
    y: list[float | None] = field(default_factory=list)
    z: list[float | None] = field(default_factory=list)
    ok: list[bool] = field(default_factory=list)
    used: list[int | None] = field(default_factory=list)
    resid: list[float | None] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.t)

    def add(
        self,
        *,
        t_ms: int,
        seq: int,
        dt_ms: int | None,
        ok: bool,
        x_mm: int | None,
        y_mm: int | None,
        z_mm: int | None,
        used: int | None,
        resid_mm: int | None,
    ) -> None:
        """1 サイクルぶんを足す。測位に失敗したサイクルの座標は値が入っていても捨てる。"""
        self.t.append(t_ms)
        self.seq.append(seq)
        self.dt.append(dt_ms)
        self.x.append(_meters_or_none(x_mm) if ok else None)
        self.y.append(_meters_or_none(y_mm) if ok else None)
        self.z.append(_meters_or_none(z_mm) if ok else None)
        self.ok.append(ok)
        self.used.append(used)
        self.resid.append(_meters_or_none(resid_mm))

    def add_cycle(self, cycle: CycleRecord, dt_ms: int | None) -> None:
        self.add(
            t_ms=cycle.t_tag_ms,
            seq=cycle.seq,
            dt_ms=dt_ms,
            ok=cycle.fix_ok,
            x_mm=cycle.x_mm,
            y_mm=cycle.y_mm,
            z_mm=cycle.z_mm,
            used=cycle.used_count,
            resid_mm=cycle.residual_mm,
        )

    def to_json(self) -> dict[str, list[Any]]:
        return {
            "t": self.t,
            "seq": self.seq,
            "dt": self.dt,
            "x": self.x,
            "y": self.y,
            "z": self.z,
            "ok": self.ok,
            "used": self.used,
            "resid": self.resid,
        }


@dataclass(slots=True)
class RangeColumns:
    """1 台のアンカーに対する測距の列。`st` は status (0 = OK)、`el` は `elapsed_ms`。"""

    t: list[int] = field(default_factory=list)
    seq: list[int] = field(default_factory=list)
    d: list[float | None] = field(default_factory=list)
    st: list[int] = field(default_factory=list)
    el: list[int | None] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.t)

    def add(
        self, *, t_ms: int, seq: int, status: int, distance_mm: int | None, elapsed_ms: int | None
    ) -> None:
        """1 記録ぶんを足す。失敗した測距の距離は値が入っていても捨てる。

        成功した測距の距離は負の値でもそのまま残す。タグが解から外した値も生のまま見せるため。
        """
        self.t.append(t_ms)
        self.seq.append(seq)
        self.d.append(_meters_or_none(distance_mm) if status == STATUS_OK else None)
        self.st.append(status)
        self.el.append(elapsed_ms)

    def to_json(self) -> dict[str, list[Any]]:
        return {"t": self.t, "seq": self.seq, "d": self.d, "st": self.st, "el": self.el}


@dataclass(slots=True)
class RangeTable:
    """アンカーごとの測距の列。JSON では `0x0100` 形式の ID をキーにする。"""

    anchors: dict[int, RangeColumns] = field(default_factory=dict)

    def add(
        self,
        *,
        anchor_id: int,
        t_ms: int,
        seq: int,
        status: int,
        distance_mm: int | None,
        elapsed_ms: int | None,
    ) -> None:
        columns = self.anchors.get(anchor_id)
        if columns is None:
            columns = RangeColumns()
            self.anchors[anchor_id] = columns
        columns.add(t_ms=t_ms, seq=seq, status=status, distance_mm=distance_mm, elapsed_ms=elapsed_ms)

    def add_cycle(self, cycle: CycleRecord) -> None:
        for record in cycle.ranges:
            self.add(
                anchor_id=record.anchor_id,
                t_ms=cycle.t_tag_ms,
                seq=cycle.seq,
                status=record.status,
                distance_mm=record.distance_mm,
                elapsed_ms=record.elapsed_ms,
            )

    def to_json(self) -> dict[str, dict[str, list[Any]]]:
        return {
            format_hex_id(anchor_id): self.anchors[anchor_id].to_json() for anchor_id in sorted(self.anchors)
        }

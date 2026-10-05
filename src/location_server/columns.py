"""ライブ配信と参照 API が共有する列指向データの組み立て。

可視化ページはライブと再生で同じ描画処理を使う (設計文書 8.1)。そのため WebSocket の
`snapshot` / `append` と参照 API の `track` / `ranges` は、同じ列名・同じ単位の列指向 JSON を返す。
受信直後のパケット (`CycleRecord`) からも DB の行からも、ここにある組み立て器を通して作る。

- `t` はタグの `millis()` (整数ミリ秒)、`seq` はサイクル通番。どちらもパケットでは 32 ビットで折り返すが、
  セッションの中で単調に増えるよう折り返しを展開した値を返す (折り返した回数に 2^32 を掛けた値を足す)。
  折り返す前は受信した値そのままなので、通常の走行試験では生の値と一致する
- `dt` は直前のサイクル (`seq` が 1 つ前) からの時刻差 [ms]。欠番をまたぐ場合と直前が無い場合は `null`。
  間引いた再生データでも本来の周期が見えるよう、間引く前に計算して載せる
- 長さ (`x` / `y` / `z` / `resid` / `d`) はメートル。API の JSON でのみメートルへ直す方針に従う
- 測位に失敗したサイクルは `x` / `y` / `z` を `null`、測距に失敗した記録は `d` を `null` にする。
  DB 側も同じ行を NULL で保存しているので、ライブと再生で見え方が変わらない
- `kx` / `ky` / `kz` / `ksig` はタグ側のカルマンフィルタの位置と位置の標準偏差 [m]。最小二乗の `ok` とは
  独立で、フィルタの位置が無効 (`kok` が偽) なサイクルだけ `null` にする。`kupd` は観測で更新したか
  (偽なら予測のみ)、`kinit` は最小二乗の解から初期化したかで、`kok` が偽なら偽とする。`kused` / `krej` は
  フィルタが取り込んだ測距と棄却した測距の本数で、フィルタの列を足す前に記録した行では `null` になる
- 測距の `kf` は、その測距をタグ側のカルマンフィルタがどう扱ったか (0 = 使っていない、1 = 取り込んだ、
  2 = ゲートで棄却した)。測距レコードに `kf` を足す前 (パケット形式 version 2 まで) に記録した行では `null`
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
    kx: list[float | None] = field(default_factory=list)
    ky: list[float | None] = field(default_factory=list)
    kz: list[float | None] = field(default_factory=list)
    kok: list[bool] = field(default_factory=list)
    kupd: list[bool] = field(default_factory=list)
    kinit: list[bool] = field(default_factory=list)
    ksig: list[float | None] = field(default_factory=list)
    kused: list[int | None] = field(default_factory=list)
    krej: list[int | None] = field(default_factory=list)

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
        kf_ok: bool,
        kf_updated: bool,
        kf_init: bool,
        kf_x_mm: int | None,
        kf_y_mm: int | None,
        kf_z_mm: int | None,
        kf_sigma_mm: int | None,
        kf_used: int | None,
        kf_rejected: int | None,
    ) -> None:
        """1 サイクルぶんを足す。

        測位に失敗したサイクルの座標と、フィルタの位置が無効なサイクルのフィルタの座標・標準偏差は、
        値が入っていても捨てる。フィルタの列は最小二乗の成否と関係なく `kf_ok` だけで決める。
        """
        self.t.append(t_ms)
        self.seq.append(seq)
        self.dt.append(dt_ms)
        self.x.append(_meters_or_none(x_mm) if ok else None)
        self.y.append(_meters_or_none(y_mm) if ok else None)
        self.z.append(_meters_or_none(z_mm) if ok else None)
        self.ok.append(ok)
        self.used.append(used)
        self.resid.append(_meters_or_none(resid_mm))
        self.kx.append(_meters_or_none(kf_x_mm) if kf_ok else None)
        self.ky.append(_meters_or_none(kf_y_mm) if kf_ok else None)
        self.kz.append(_meters_or_none(kf_z_mm) if kf_ok else None)
        self.kok.append(kf_ok)
        self.kupd.append(kf_ok and kf_updated)
        self.kinit.append(kf_ok and kf_init)
        self.ksig.append(_meters_or_none(kf_sigma_mm) if kf_ok else None)
        self.kused.append(kf_used)
        self.krej.append(kf_rejected)

    def add_cycle(self, cycle: CycleRecord, *, t_ms: int, seq: int, dt_ms: int | None) -> None:
        """受信したサイクルを足す。`t_ms` / `seq` には折り返しを展開した値を渡す。"""
        self.add(
            t_ms=t_ms,
            seq=seq,
            dt_ms=dt_ms,
            ok=cycle.fix_ok,
            x_mm=cycle.x_mm,
            y_mm=cycle.y_mm,
            z_mm=cycle.z_mm,
            used=cycle.used_count,
            resid_mm=cycle.residual_mm,
            kf_ok=cycle.kf_ok,
            kf_updated=cycle.kf_updated,
            kf_init=cycle.kf_init,
            kf_x_mm=cycle.kf_x_mm,
            kf_y_mm=cycle.kf_y_mm,
            kf_z_mm=cycle.kf_z_mm,
            kf_sigma_mm=cycle.kf_sigma_mm,
            kf_used=cycle.kf_used,
            kf_rejected=cycle.kf_rejected,
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
            "kx": self.kx,
            "ky": self.ky,
            "kz": self.kz,
            "kok": self.kok,
            "kupd": self.kupd,
            "kinit": self.kinit,
            "ksig": self.ksig,
            "kused": self.kused,
            "krej": self.krej,
        }


@dataclass(slots=True)
class RangeColumns:
    """1 台のアンカーに対する測距の列。`st` は status (0 = OK)、`el` は `elapsed_ms`。

    `kf` はその測距をフィルタがどう扱ったか (`RANGE_KF_*` の値) で、記録していない行は `None`。
    """

    t: list[int] = field(default_factory=list)
    seq: list[int] = field(default_factory=list)
    d: list[float | None] = field(default_factory=list)
    st: list[int] = field(default_factory=list)
    el: list[int | None] = field(default_factory=list)
    kf: list[int | None] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.t)

    def add(
        self,
        *,
        t_ms: int,
        seq: int,
        status: int,
        distance_mm: int | None,
        elapsed_ms: int | None,
        kf: int | None,
    ) -> None:
        """1 記録ぶんを足す。失敗した測距の距離は値が入っていても捨てる。

        成功した測距の距離は負の値でもそのまま残す。タグが解から外した値も生のまま見せるため。
        """
        self.t.append(t_ms)
        self.seq.append(seq)
        self.d.append(_meters_or_none(distance_mm) if status == STATUS_OK else None)
        self.st.append(status)
        self.el.append(elapsed_ms)
        self.kf.append(kf)

    def to_json(self) -> dict[str, list[Any]]:
        return {"t": self.t, "seq": self.seq, "d": self.d, "st": self.st, "el": self.el, "kf": self.kf}


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
        kf: int | None,
    ) -> None:
        columns = self.anchors.get(anchor_id)
        if columns is None:
            columns = RangeColumns()
            self.anchors[anchor_id] = columns
        columns.add(t_ms=t_ms, seq=seq, status=status, distance_mm=distance_mm, elapsed_ms=elapsed_ms, kf=kf)

    def add_cycle(self, cycle: CycleRecord, *, t_ms: int, seq: int) -> None:
        """受信したサイクルの測距を足す。`t_ms` / `seq` には折り返しを展開した値を渡す。"""
        for record in cycle.ranges:
            self.add(
                anchor_id=record.anchor_id,
                t_ms=t_ms,
                seq=seq,
                status=record.status,
                distance_mm=record.distance_mm,
                elapsed_ms=record.elapsed_ms,
                kf=record.kf,
            )

    def to_json(self) -> dict[str, dict[str, list[Any]]]:
        return {
            format_hex_id(anchor_id): self.anchors[anchor_id].to_json() for anchor_id in sorted(self.anchors)
        }

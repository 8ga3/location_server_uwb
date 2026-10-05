"""テレメトリのテストで使うパケットの組み立て。"""

from __future__ import annotations

from location_server.ingest.packet import (
    FIX_FLAG_KF_OK,
    FIX_FLAG_KF_UPDATED,
    FIX_FLAG_OK,
    RANGE_KF_ACCEPTED,
    RANGE_KF_UNUSED,
    CycleRecord,
    RangeRecord,
    TelemetryPacket,
)

ANCHORS = (0x0100, 0x0101, 0x0102, 0x0103)


def make_cycle(
    seq: int,
    t_tag_ms: int,
    *,
    fix_ok: bool = True,
    kf_ok: bool = True,
    kf_updated: bool = True,
    anchors: tuple[int, ...] = ANCHORS,
) -> CycleRecord:
    """測距がすべて成功したサイクル。既定では最小二乗が解け、フィルタも観測で更新されている。

    フィルタが観測で更新したサイクルでは全測距を取り込んだことにし、それ以外は使っていないことにする。
    """
    range_kf = RANGE_KF_ACCEPTED if kf_ok and kf_updated else RANGE_KF_UNUSED
    ranges = tuple(
        RangeRecord(anchor_id=a, status=0, elapsed_ms=6, distance_mm=1000 + i * 100, kf=range_kf)
        for i, a in enumerate(anchors)
    )
    fix_flags = 0
    if fix_ok:
        fix_flags |= FIX_FLAG_OK
    if kf_ok:
        fix_flags |= FIX_FLAG_KF_OK
        if kf_updated:
            fix_flags |= FIX_FLAG_KF_UPDATED
    kf_used = len(anchors) if kf_ok and kf_updated else 0
    return CycleRecord(
        seq=seq,
        t_tag_ms=t_tag_ms,
        fix_flags=fix_flags,
        used_count=len(anchors) if fix_ok else 0,
        x_mm=1234,
        y_mm=-5678,
        z_mm=1000,
        residual_mm=42,
        # タグはフィルタの位置が無効なサイクルの座標欄を 0 のまま送る
        kf_x_mm=1200 if kf_ok else 0,
        kf_y_mm=-5600 if kf_ok else 0,
        kf_z_mm=1000 if kf_ok else 0,
        kf_sigma_mm=35 if kf_ok else 0,
        kf_used=kf_used,
        kf_rejected=0,
        ranges=ranges,
    )


def make_packet(
    *,
    tag_id: int = 1,
    boot_id: int = 0xAAAAAAAA,
    seq: int = 0,
    t_tag_ms: int = 1000,
    count: int = 4,
    period_ms: int = 50,
    flags: int = 0,
    anchors: tuple[int, ...] = ANCHORS,
) -> TelemetryPacket:
    return TelemetryPacket(
        flags=flags,
        tag_id=tag_id,
        boot_id=boot_id,
        seq=seq,
        t_tag_ms=t_tag_ms,
        anchor_n=len(anchors),
        cycles=tuple(
            make_cycle((seq + i) & 0xFFFFFFFF, t_tag_ms + i * period_ms, anchors=anchors)
            for i in range(count)
        ),
    )

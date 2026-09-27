"""テレメトリのテストで使うパケットの組み立て。"""

from __future__ import annotations

from location_server.ingest.packet import FIX_FLAG_OK, CycleRecord, RangeRecord, TelemetryPacket

ANCHORS = (0x0100, 0x0101, 0x0102, 0x0103)


def make_cycle(
    seq: int, t_tag_ms: int, *, fix_ok: bool = True, anchors: tuple[int, ...] = ANCHORS
) -> CycleRecord:
    ranges = tuple(
        RangeRecord(anchor_id=a, status=0, elapsed_ms=6, distance_mm=1000 + i * 100)
        for i, a in enumerate(anchors)
    )
    return CycleRecord(
        seq=seq,
        t_tag_ms=t_tag_ms,
        fix_flags=FIX_FLAG_OK if fix_ok else 0,
        used_count=len(anchors) if fix_ok else 0,
        x_mm=1234,
        y_mm=-5678,
        z_mm=1000,
        residual_mm=42,
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

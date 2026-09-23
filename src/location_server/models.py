"""構成配信で扱う値オブジェクト。

DB の行と API のスキーマの間に置き、座標はすべて整数ミリメートルのまま保持する。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

ANCHOR_SOURCE_MANUAL = "manual"
ANCHOR_SOURCE_SURVEY = "survey"
ANCHOR_SOURCES = (ANCHOR_SOURCE_MANUAL, ANCHOR_SOURCE_SURVEY)


@dataclass(frozen=True, slots=True)
class Anchor:
    """アンカー 1 台の設置情報。"""

    id: int
    label: str | None
    x_mm: int
    y_mm: int
    z_mm: int
    enabled: bool
    source: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Anchor:
        return cls(
            id=row["id"],
            label=row["label"],
            x_mm=row["x_mm"],
            y_mm=row["y_mm"],
            z_mm=row["z_mm"],
            enabled=bool(row["enabled"]),
            source=row["source"],
            updated_at=row["updated_at"],
        )


@dataclass(frozen=True, slots=True)
class ConfigMeta:
    """構成リビジョン 1 件ぶんの情報。"""

    rev: int
    pan_id: int
    bias_mm: int
    telemetry_host: str
    telemetry_port: int
    batch_cycles: int
    note: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ConfigMeta:
        return cls(
            rev=row["rev"],
            pan_id=row["pan_id"],
            bias_mm=row["bias_mm"],
            telemetry_host=row["telemetry_host"],
            telemetry_port=row["telemetry_port"],
            batch_cycles=row["batch_cycles"],
            note=row["note"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """ある `rev` 時点の構成一式。

    `anchors` にはそのリビジョンに含まれる全アンカーが入る。
    タグへ配る構成では無効化されたアンカーを除外する (`enabled_anchors`)。
    """

    meta: ConfigMeta
    anchors: tuple[Anchor, ...]

    @property
    def enabled_anchors(self) -> tuple[Anchor, ...]:
        return tuple(anchor for anchor in self.anchors if anchor.enabled)

    @property
    def etag(self) -> str:
        """構成配信の ETag。設計文書 5.1 の `"rev-7"` 形式。"""
        return f'"rev-{self.meta.rev}"'

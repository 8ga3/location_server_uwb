"""HTTP API のリクエスト / レスポンススキーマ。

座標は JSON でのみメートル表記とし、ID は `0x0100` 形式の文字列で表す (設計文書 4 節 / 5.4)。
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from location_server.ingest.packet import COUNT_MAX as PACKET_COUNT_MAX
from location_server.models import Anchor, ConfigSnapshot
from location_server.units import (
    COORD_MM_MAX,
    TAG_ID_MAX,
    TAG_ID_MIN,
    format_hex_id,
    meters_to_mm,
    mm_to_meters,
)

# 1 パケットに詰めるサイクル数の上限。パケット形式の count (設計文書 6.2) の上限と一致させる
BATCH_CYCLES_MAX = PACKET_COUNT_MAX
UDP_PORT_MAX = 65535
BOOT_ID_MAX = 0xFFFFFFFF
FW_VERSION_MAX_LEN = 64

Meters = Annotated[float, Field(ge=-COORD_MM_MAX / 1000, le=COORD_MM_MAX / 1000)]


class AnchorPut(BaseModel):
    """`PUT /api/v1/anchors/{id}` の本文。"""

    model_config = ConfigDict(extra="forbid")

    label: str | None = None
    x: Meters
    y: Meters
    z: Meters
    enabled: bool = True

    def to_mm(self) -> tuple[int, int, int]:
        return meters_to_mm(self.x), meters_to_mm(self.y), meters_to_mm(self.z)


class TelemetryPut(BaseModel):
    """`PUT /api/v1/config/telemetry` の本文。"""

    model_config = ConfigDict(extra="forbid")

    host: str = ""
    port: int = Field(ge=0, le=UDP_PORT_MAX)
    batch_cycles: int = Field(ge=1, le=BATCH_CYCLES_MAX)

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        if value == "":
            return value
        # タグ側は IP 直指定で解決する (設計文書 9 節) ため、名前は受け付けない
        ipaddress.ip_address(value)
        return value

    @model_validator(mode="after")
    def _check_host_present(self) -> Self:
        if self.port != 0 and self.host == "":
            raise ValueError("port が 0 でない場合は host が必要です")
        return self


class HelloIn(BaseModel):
    """`POST /api/v1/hello` の本文 (設計文書 5.2)。"""

    model_config = ConfigDict(extra="forbid")

    tag_id: int = Field(ge=TAG_ID_MIN, le=TAG_ID_MAX)
    boot_id: int = Field(ge=0, le=BOOT_ID_MAX)
    fw_version: Annotated[str, Field(max_length=FW_VERSION_MAX_LEN)] | None = None
    config_rev: int | None = Field(default=None, ge=1)


class HelloOut(BaseModel):
    session_id: int


class TelemetryOut(BaseModel):
    host: str
    port: int
    batch_cycles: int


class ConfigAnchorOut(BaseModel):
    """タグへ配る構成に含めるアンカー。座標だけを渡す。"""

    id: str
    x: float
    y: float
    z: float


class ConfigOut(BaseModel):
    """`GET /api/v1/config` の応答。"""

    rev: int
    pan_id: str
    bias_mm: int
    anchors: list[ConfigAnchorOut]
    telemetry: TelemetryOut


class AnchorOut(BaseModel):
    """管理 API が返すアンカー。運用に要る属性も含める。"""

    id: str
    label: str | None
    x: float
    y: float
    z: float
    enabled: bool
    source: str
    updated_at: str


class AnchorListOut(BaseModel):
    rev: int
    anchors: list[AnchorOut]


class AnchorPutOut(BaseModel):
    rev: int
    anchor: AnchorOut


def to_anchor_out(anchor: Anchor) -> AnchorOut:
    return AnchorOut(
        id=format_hex_id(anchor.id),
        label=anchor.label,
        x=mm_to_meters(anchor.x_mm),
        y=mm_to_meters(anchor.y_mm),
        z=mm_to_meters(anchor.z_mm),
        enabled=anchor.enabled,
        source=anchor.source,
        updated_at=anchor.updated_at,
    )


def to_config_out(snapshot: ConfigSnapshot) -> ConfigOut:
    meta = snapshot.meta
    return ConfigOut(
        rev=meta.rev,
        pan_id=format_hex_id(meta.pan_id),
        bias_mm=meta.bias_mm,
        anchors=[
            ConfigAnchorOut(
                id=format_hex_id(anchor.id),
                x=mm_to_meters(anchor.x_mm),
                y=mm_to_meters(anchor.y_mm),
                z=mm_to_meters(anchor.z_mm),
            )
            for anchor in snapshot.enabled_anchors
        ],
        telemetry=TelemetryOut(
            host=meta.telemetry_host,
            port=meta.telemetry_port,
            batch_cycles=meta.batch_cycles,
        ),
    )

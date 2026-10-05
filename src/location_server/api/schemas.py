"""HTTP API のリクエスト / レスポンススキーマ。

座標は JSON でのみメートル表記とし、ID は `0x0100` 形式の文字列で表す (設計文書 4 節 / 5.4)。
"""

from __future__ import annotations

import ipaddress
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from location_server.ingest.packet import COUNT_MAX as PACKET_COUNT_MAX
from location_server.models import Anchor, ConfigSnapshot
from location_server.store.query import SessionInfo, SessionSummary
from location_server.units import (
    COORD_MM_MAX,
    TAG_ID_MAX,
    TAG_ID_MIN,
    check_anchor_id,
    format_hex_id,
    meters_to_mm,
    mm_to_meters,
    parse_hex_id,
)

# 1 パケットに詰めるサイクル数の上限。パケット形式の count (設計文書 6.2) の上限と一致させる
BATCH_CYCLES_MAX = PACKET_COUNT_MAX
UDP_PORT_MAX = 65535
BOOT_ID_MAX = 0xFFFFFFFF
FW_VERSION_MAX_LEN = 64
# 一括置換で一度に渡せるアンカーの台数。パケットの anchor_n (1 バイト) で表せる台数に合わせる
BULK_ANCHORS_MAX = 255
CONFIG_NOTE_MAX_LEN = 200

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


class AnchorBulkItem(AnchorPut):
    """`POST /api/v1/anchors:bulk` の本文に並べるアンカー 1 台。`id` は `0x0100` 形式か 10 進数表記。"""

    id: str

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        check_anchor_id(parse_hex_id(value))
        return value

    @property
    def anchor_id(self) -> int:
        return parse_hex_id(self.id)


class AnchorBulkIn(BaseModel):
    """`POST /api/v1/anchors:bulk` の本文。ここに無いアンカーは削除される。"""

    model_config = ConfigDict(extra="forbid")

    anchors: list[AnchorBulkItem] = Field(min_length=1, max_length=BULK_ANCHORS_MAX)
    source: Literal["manual", "survey"] = "manual"
    note: Annotated[str, Field(max_length=CONFIG_NOTE_MAX_LEN)] | None = None

    @model_validator(mode="after")
    def _check_unique_ids(self) -> Self:
        # "0x0100" と "256" のように表記が違っても同じ ID なら重複とみなす
        seen: set[int] = set()
        for item in self.anchors:
            if item.anchor_id in seen:
                raise ValueError(f"アンカー ID が重複しています: {format_hex_id(item.anchor_id)}")
            seen.add(item.anchor_id)
        return self


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


# ---------------------------------------------------------------- 参照 API (設計文書 5.5)
#
# 列指向のデータはライブ配信 (WebSocket の snapshot / append) と同じ列名・単位で返す。
# 組み立ては `location_server.columns` に寄せてあり、ここでは応答の形を宣言するだけにする。

TRACK_MAX_POINTS_DEFAULT = 2000
TRACK_MAX_POINTS_LIMIT = 20_000
SESSION_LIST_LIMIT_DEFAULT = 100
SESSION_LIST_LIMIT_MAX = 1000


class FixColumnsOut(BaseModel):
    """測位結果の列。`t` はタグの millis()、長さはメートル。測位に失敗したサイクルの座標は null。

    `dt` は直前のサイクルからの時刻差 [ms] で、欠番をまたぐ場合は null。間引く前に求める。
    `k` で始まる列はタグ側のカルマンフィルタの出力で、最小二乗の `ok` とは独立に入る。
    `kx` / `ky` / `kz` / `ksig` はフィルタの位置が無効 (`kok` が偽) なら null。`kupd` は観測で更新したか
    (偽なら予測のみ)、`kinit` は最小二乗の解から初期化したか。`kused` / `krej` はフィルタが取り込んだ測距と
    棄却した測距の本数で、フィルタの列を足す前に記録した行では null。
    """

    t: list[int]
    seq: list[int]
    dt: list[int | None]
    x: list[float | None]
    y: list[float | None]
    z: list[float | None]
    ok: list[bool]
    used: list[int | None]
    resid: list[float | None]
    kx: list[float | None]
    ky: list[float | None]
    kz: list[float | None]
    kok: list[bool]
    kupd: list[bool]
    kinit: list[bool]
    ksig: list[float | None]
    kused: list[int | None]
    krej: list[int | None]


class RangeColumnsOut(BaseModel):
    """アンカー 1 台ぶんの測距の列。`d` はメートルで、失敗 (`st != 0`) は null。`el` は elapsed_ms。

    `kf` はその測距をタグ側のカルマンフィルタがどう扱ったか。0 = 使っていない (測距の失敗、負の値で
    解から外した、フィルタが無効、そのサイクルで最小二乗の解から初期化した、アンカーの真下にいて飛ばした)、
    1 = 取り込んだ、2 = イノベーションのゲートで棄却した。パケット形式 version 2 までに記録した行は null。
    """

    t: list[int]
    seq: list[int]
    d: list[float | None]
    st: list[int]
    el: list[int | None]
    kf: list[int | None]


class DecimationOut(BaseModel):
    """間引きの情報。`total` は間引く前の件数、`stride` は何件おきに取ったか。"""

    total: int
    stride: int


class TrackOut(BaseModel):
    session_id: int
    decimation: DecimationOut
    fix: FixColumnsOut


class RangesOut(BaseModel):
    session_id: int
    decimation: dict[str, DecimationOut]
    ranges: dict[str, RangeColumnsOut]


class SessionOut(BaseModel):
    """セッション 1 件。`missing_cycles` / `loss_rate` は `seq` の欠番 (UDP の欠測) から数える。"""

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
    missing_cycles: int
    fix_rate: float | None
    loss_rate: float | None
    first_t_ms: int | None
    last_t_ms: int | None


class SessionListOut(BaseModel):
    sessions: list[SessionOut]


class LiveAnchorOut(BaseModel):
    """可視化ページへ渡すアンカー。XY 平面へ描くので呼び名も添える。"""

    id: str
    label: str | None
    x: float
    y: float
    z: float


class AnchorSummaryOut(BaseModel):
    """アンカー 1 台ぶんの測距の集計。`status_counts` のキーは status の 10 進表記。"""

    id: str
    samples: int
    ok_samples: int
    success_rate: float | None
    elapsed_mean_ms: float | None
    elapsed_max_ms: int | None
    distance_mean: float | None
    status_counts: dict[str, int]


class SummaryOut(BaseModel):
    """`GET /api/v1/sessions/{id}/summary` の応答。

    `anchors` はそのセッションが使った構成リビジョン (`anchors_rev`) の座標表。
    セッションの構成リビジョンがわからない場合は現在の構成を返し、`anchors_rev` でそれと示す。

    `kf_` で始まる項目はタグ側のカルマンフィルタの集計。`kf_ok_cycles` はフィルタの位置が有効だった
    サイクル数、`kf_predicted_cycles` はそのうち観測による更新が無かった (予測のみの) サイクル数、
    `kf_init_count` は最小二乗の解から初期化したサイクル数、`kf_rejected_ranges` はゲートで棄却した
    測距の合計本数。`kf_sigma_mean` はフィルタが有効なサイクルの位置の標準偏差の平均 [m] で、
    有効なサイクルが無ければ null。
    """

    session: SessionOut
    residual_rms: float | None
    period_mean_ms: float | None
    period_max_ms: int | None
    used_min: int | None
    used_max: int | None
    kf_ok_cycles: int
    kf_predicted_cycles: int
    kf_init_count: int
    kf_rejected_ranges: int
    kf_sigma_mean: float | None
    ranges: list[AnchorSummaryOut]
    anchors_rev: int
    anchors: list[LiveAnchorOut]


def to_live_anchor_out(anchor: Anchor) -> LiveAnchorOut:
    return LiveAnchorOut(
        id=format_hex_id(anchor.id),
        label=anchor.label,
        x=mm_to_meters(anchor.x_mm),
        y=mm_to_meters(anchor.y_mm),
        z=mm_to_meters(anchor.z_mm),
    )


def to_session_out(info: SessionInfo) -> SessionOut:
    return SessionOut(
        id=info.id,
        tag_id=info.tag_id,
        boot_id=info.boot_id,
        config_rev=info.config_rev,
        fw_version=info.fw_version,
        started_at=info.started_at,
        last_seen_at=info.last_seen_at,
        note=info.note,
        cycles=info.cycles,
        fix_ok_cycles=info.fix_ok_cycles,
        missing_cycles=info.missing_cycles,
        fix_rate=info.fix_rate,
        loss_rate=info.loss_rate,
        first_t_ms=info.first_t_ms,
        last_t_ms=info.last_t_ms,
    )


def to_summary_out(summary: SessionSummary, anchors: ConfigSnapshot) -> SummaryOut:
    return SummaryOut(
        session=to_session_out(summary.session),
        residual_rms=None if summary.residual_rms_mm is None else summary.residual_rms_mm / 1000,
        period_mean_ms=summary.period_mean_ms,
        period_max_ms=summary.period_max_ms,
        used_min=summary.used_min,
        used_max=summary.used_max,
        kf_ok_cycles=summary.kf_ok_cycles,
        kf_predicted_cycles=summary.kf_predicted_cycles,
        kf_init_count=summary.kf_init_count,
        kf_rejected_ranges=summary.kf_rejected_ranges,
        kf_sigma_mean=None if summary.kf_sigma_mean_mm is None else summary.kf_sigma_mean_mm / 1000,
        ranges=[
            AnchorSummaryOut(
                id=format_hex_id(stats.anchor_id),
                samples=stats.samples,
                ok_samples=stats.ok_samples,
                success_rate=stats.success_rate,
                elapsed_mean_ms=stats.elapsed_mean,
                elapsed_max_ms=stats.elapsed_max,
                distance_mean=None if stats.distance_mean_mm is None else stats.distance_mean_mm / 1000,
                status_counts={str(code): n for code, n in sorted(stats.status_counts.items())},
            )
            for stats in summary.anchors
        ],
        anchors_rev=anchors.meta.rev,
        anchors=[to_live_anchor_out(anchor) for anchor in anchors.enabled_anchors],
    )

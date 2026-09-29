"""セッションのエンドポイント。

タグが構成取得の直後に 1 回だけ呼ぶ `POST /api/v1/hello` を提供する (設計文書 5.2)。
これはタグ自身が呼ぶ API であり管理 API ではないので、共有トークンは要求しない。
タグは失敗しても走行を続けてよく、サーバー側も UDP を最初に受けた時点でセッションを作る。

可視化ページが使う参照 API (設計文書 5.5) とセッション一覧もここに置く。どれも読み取りだけなので
共有トークンは要求しない。時刻の範囲 (`from_ms` / `to_ms`) は、32 ビットの折り返しを展開したタグの
millis() で指定する。
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from location_server.api.deps import (
    get_live_hub,
    get_query_store,
    get_store,
    get_telemetry_store,
    parse_anchor_id_param,
    snapshot_for_session,
)
from location_server.api.schemas import (
    SESSION_LIST_LIMIT_DEFAULT,
    SESSION_LIST_LIMIT_MAX,
    TRACK_MAX_POINTS_DEFAULT,
    TRACK_MAX_POINTS_LIMIT,
    DecimationOut,
    FixColumnsOut,
    HelloIn,
    HelloOut,
    RangeColumnsOut,
    RangesOut,
    SessionListOut,
    SummaryOut,
    TrackOut,
    to_session_out,
    to_summary_out,
)
from location_server.db import utc_now_text
from location_server.store.query import MAX_POINTS_MIN, SessionInfo
from location_server.units import format_hex_id

router = APIRouter(prefix="/api/v1", tags=["sessions"])

# タグの millis() は 32 ビットで折り返すが、参照 API は折り返しを展開した値で扱う (store/query.py)。
# 上限はブラウザの Number が整数を正確に表せる範囲 (2^53) とする
TagMillis = Annotated[int | None, Query(ge=0, le=2**53)]
MaxPoints = Annotated[int, Query(ge=MAX_POINTS_MIN, le=TRACK_MAX_POINTS_LIMIT)]


@router.post("/hello", response_model=HelloOut)
async def post_hello(request: Request, body: HelloIn) -> HelloOut:
    """セッション開始を記録する。同じ `(tag_id, boot_id)` で呼ばれた場合は既存の行を更新する。

    DB への書き込みはスレッドで行い、決まったセッション ID と構成リビジョンをライブ配信へ伝える。
    ライブ配信は DB に触れないので、ここで教えないと UDP の受信だけでは構成リビジョンがわからない。
    """
    session_id = await asyncio.to_thread(
        get_telemetry_store(request).hello,
        tag_id=body.tag_id,
        boot_id=body.boot_id,
        fw_version=body.fw_version,
        config_rev=body.config_rev,
        now=utc_now_text(),
    )
    get_live_hub(request).note_hello(body.tag_id, body.boot_id, session_id, body.config_rev)
    return HelloOut(session_id=session_id)


@router.get("/sessions", response_model=SessionListOut)
def list_sessions(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=SESSION_LIST_LIMIT_MAX)] = SESSION_LIST_LIMIT_DEFAULT,
) -> SessionListOut:
    """セッションを新しい順に返す。成功率と欠測率を添える。"""
    sessions = get_query_store(request).list_sessions(limit)
    return SessionListOut(sessions=[to_session_out(info) for info in sessions])


def _require_session(request: Request, session_id: int) -> SessionInfo:
    info = get_query_store(request).get_session(session_id)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"セッションが存在しません: {session_id}"
        )
    return info


def _check_range(from_ms: int | None, to_ms: int | None) -> None:
    if from_ms is not None and to_ms is not None and from_ms > to_ms:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"from_ms ({from_ms}) が to_ms ({to_ms}) より後です",
        )


@router.get("/sessions/{session_id}/track", response_model=TrackOut)
def get_track(
    request: Request,
    session_id: int,
    from_ms: TagMillis = None,
    to_ms: TagMillis = None,
    max_points: MaxPoints = TRACK_MAX_POINTS_DEFAULT,
) -> TrackOut:
    """推定位置の軌跡。`max_points` を超える場合は等間隔に間引く。"""
    _check_range(from_ms, to_ms)
    _require_session(request, session_id)
    result = get_query_store(request).track(session_id, from_ms=from_ms, to_ms=to_ms, max_points=max_points)
    return TrackOut(
        session_id=session_id,
        decimation=DecimationOut(total=result.total, stride=result.stride),
        fix=FixColumnsOut.model_validate(result.data.to_json()),
    )


@router.get("/sessions/{session_id}/ranges", response_model=RangesOut)
def get_ranges(
    request: Request,
    session_id: int,
    anchor_id: str | None = None,
    from_ms: TagMillis = None,
    to_ms: TagMillis = None,
    max_points: MaxPoints = TRACK_MAX_POINTS_DEFAULT,
) -> RangesOut:
    """アンカーごとの測距。`anchor_id` を省略すると全アンカーを返し、間引きはアンカーごとに行う。"""
    _check_range(from_ms, to_ms)
    parsed = None if anchor_id is None else parse_anchor_id_param(anchor_id)
    _require_session(request, session_id)
    result = get_query_store(request).ranges(
        session_id, anchor_id=parsed, from_ms=from_ms, to_ms=to_ms, max_points=max_points
    )
    ranges: dict[str, RangeColumnsOut] = {}
    decimation: dict[str, DecimationOut] = {}
    for aid, entry in sorted(result.items()):
        key = format_hex_id(aid)
        ranges[key] = RangeColumnsOut.model_validate(entry.data.anchors[aid].to_json())
        decimation[key] = DecimationOut(total=entry.total, stride=entry.stride)
    return RangesOut(session_id=session_id, decimation=decimation, ranges=ranges)


@router.get("/sessions/{session_id}/summary", response_model=SummaryOut)
def get_summary(request: Request, session_id: int) -> SummaryOut:
    """セッション単位の成功率・欠測率・残差 RMS と、アンカーごとの測距の集計。"""
    summary = get_query_store(request).summary(session_id)
    if summary is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"セッションが存在しません: {session_id}"
        )
    anchors = snapshot_for_session(get_store(request), summary.session.config_rev)
    return to_summary_out(summary, anchors)

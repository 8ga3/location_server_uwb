"""構成配信のエンドポイント。

タグが起動時に 1 回だけ呼ぶ `GET /api/v1/config` と、その一部であるテレメトリ設定の更新を提供する。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from location_server.api.auth import require_write_token
from location_server.api.deps import get_store
from location_server.api.schemas import ConfigOut, TelemetryPut, to_config_out
from location_server.models import ConfigSnapshot
from location_server.units import TAG_ID_MAX, TAG_ID_MIN

router = APIRouter(prefix="/api/v1", tags=["config"])


def _matches_if_none_match(header: str | None, etag: str) -> bool:
    """`If-None-Match` が現在の ETag と一致するかを判定する。"""
    if header is None:
        return False
    for candidate in header.split(","):
        value = candidate.strip()
        if value == "*":
            return True
        if value.startswith("W/"):
            value = value[2:].strip()
        if value == etag:
            return True
    return False


def _config_response(
    snapshot: ConfigSnapshot, if_none_match: str | None, response: Response
) -> ConfigOut | Response:
    etag = snapshot.etag
    if _matches_if_none_match(if_none_match, etag):
        # 構成が変わっていなければタグは NVS キャッシュをそのまま使う (設計文書 5.1)
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"
    return to_config_out(snapshot)


@router.get("/config", response_model=ConfigOut)
def get_config(
    request: Request,
    response: Response,
    tag_id: Annotated[int | None, Query(ge=TAG_ID_MIN, le=TAG_ID_MAX)] = None,
) -> ConfigOut | Response:
    """タグへ配る構成一式を返す。

    `tag_id` は将来タグごとに構成を変えられるようにするための予約で、現時点では応答に影響しない。
    """
    del tag_id
    snapshot = get_store(request).current_snapshot()
    return _config_response(snapshot, request.headers.get("if-none-match"), response)


@router.put("/config/telemetry", response_model=ConfigOut, dependencies=[Depends(require_write_token)])
def put_telemetry(request: Request, response: Response, body: TelemetryPut) -> ConfigOut:
    """テレメトリ送信先と `batch_cycles` を更新する。`rev` が +1 される。"""
    snapshot = get_store(request).put_telemetry(
        host=body.host,
        port=body.port,
        batch_cycles=body.batch_cycles,
    )
    response.headers["ETag"] = snapshot.etag
    return to_config_out(snapshot)


@router.get("/config/revisions/{rev}", response_model=ConfigOut)
def get_config_revision(request: Request, rev: int) -> ConfigOut:
    """過去のリビジョン時点の構成を返す。走行当時の座標表を確認するために使う。"""
    try:
        snapshot = get_store(request).snapshot_at(rev)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"構成リビジョンが存在しません: {rev}",
        ) from exc
    return to_config_out(snapshot)

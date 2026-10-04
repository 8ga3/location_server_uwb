"""アンカー管理のエンドポイント。

座標を書き換えると `config_meta` に新しい `rev` が積まれ、その時点の全アンカーが
`config_anchor` へ記録される (設計文書 4.2 / 5.3)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from location_server.api.auth import require_write_token
from location_server.api.deps import get_store, parse_anchor_id_param
from location_server.api.schemas import (
    AnchorBulkIn,
    AnchorListOut,
    AnchorPut,
    AnchorPutOut,
    to_anchor_out,
)
from location_server.store import AnchorSpec

router = APIRouter(prefix="/api/v1", tags=["anchors"])


@router.get("/anchors", response_model=AnchorListOut)
def list_anchors(request: Request) -> AnchorListOut:
    """アンカー一覧を返す。無効化されているものも含めて返す。"""
    store = get_store(request)
    snapshot = store.current_snapshot()
    return AnchorListOut(
        rev=snapshot.meta.rev,
        anchors=[to_anchor_out(anchor) for anchor in snapshot.anchors],
    )


@router.put("/anchors/{anchor_id}", response_model=AnchorPutOut, dependencies=[Depends(require_write_token)])
def put_anchor(request: Request, response: Response, anchor_id: str, body: AnchorPut) -> AnchorPutOut:
    """アンカー 1 台の設置情報を登録または更新する。`rev` が +1 される。"""
    parsed_id = parse_anchor_id_param(anchor_id)
    try:
        x_mm, y_mm, z_mm = body.to_mm()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    store = get_store(request)
    result = store.put_anchor(
        anchor_id=parsed_id,
        label=body.label,
        x_mm=x_mm,
        y_mm=y_mm,
        z_mm=z_mm,
        enabled=body.enabled,
    )
    if result.created:
        response.status_code = status.HTTP_201_CREATED
    response.headers["ETag"] = result.snapshot.etag
    return AnchorPutOut(rev=result.snapshot.meta.rev, anchor=to_anchor_out(result.anchor))


@router.post("/anchors:bulk", response_model=AnchorListOut, dependencies=[Depends(require_write_token)])
def replace_anchors(request: Request, response: Response, body: AnchorBulkIn) -> AnchorListOut:
    """アンカー表を本文の内容で丸ごと置き換える。何台あっても `rev` は 1 つだけ進む。

    本文に無いアンカーは削除される。検証に失敗した場合は何も書き換えない。
    """
    try:
        specs = [AnchorSpec(item.anchor_id, item.label, *item.to_mm(), item.enabled) for item in body.anchors]
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    snapshot = get_store(request).replace_anchors(specs, source=body.source, note=body.note)
    response.headers["ETag"] = snapshot.etag
    return AnchorListOut(
        rev=snapshot.meta.rev,
        anchors=[to_anchor_out(anchor) for anchor in snapshot.anchors],
    )

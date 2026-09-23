"""アンカー管理のエンドポイント。

座標を書き換えると `config_meta` に新しい `rev` が積まれ、その時点の全アンカーが
`config_anchor` へ記録される (設計文書 4.2 / 5.3)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from location_server.api.auth import require_write_token
from location_server.api.deps import get_store
from location_server.api.schemas import AnchorListOut, AnchorPut, AnchorPutOut, to_anchor_out
from location_server.units import check_anchor_id, parse_hex_id

router = APIRouter(prefix="/api/v1", tags=["anchors"])


def _parse_anchor_id(raw: str) -> int:
    """パスパラメータのアンカー ID を解釈する。`0x0100` 形式と 10 進数表記を受け付ける。

    `ValueRangeError` だけでなく `ValueError` 全般を 400 に落とす。ID の解釈は
    `parse_hex_id` 側で完結しているが、未処理例外が 500 として漏れないよう二重に受ける。
    """
    try:
        return check_anchor_id(parse_hex_id(raw))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


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
    parsed_id = _parse_anchor_id(anchor_id)
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

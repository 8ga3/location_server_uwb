"""セッションのエンドポイント。

タグが構成取得の直後に 1 回だけ呼ぶ `POST /api/v1/hello` を提供する (設計文書 5.2)。
これはタグ自身が呼ぶ API であり管理 API ではないので、共有トークンは要求しない。
タグは失敗しても走行を続けてよく、サーバー側も UDP を最初に受けた時点でセッションを作る。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from location_server.api.deps import get_telemetry_store
from location_server.api.schemas import HelloIn, HelloOut
from location_server.db import utc_now_text

router = APIRouter(prefix="/api/v1", tags=["sessions"])


@router.post("/hello", response_model=HelloOut)
def post_hello(request: Request, body: HelloIn) -> HelloOut:
    """セッション開始を記録する。同じ `(tag_id, boot_id)` で呼ばれた場合は既存の行を更新する。"""
    session_id = get_telemetry_store(request).hello(
        tag_id=body.tag_id,
        boot_id=body.boot_id,
        fw_version=body.fw_version,
        config_rev=body.config_rev,
        now=utc_now_text(),
    )
    return HelloOut(session_id=session_id)

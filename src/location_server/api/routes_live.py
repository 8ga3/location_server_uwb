"""ライブ配信の WebSocket (設計文書 5.6)。

接続後にクライアントが購読対象を送る。接続を張り直さずに購読を切り替えられる。

```jsonc
{ "op": "subscribe", "tag_id": 1, "history_ms": 30000 }
{ "op": "unsubscribe" }
```

1 接続につき受信と送信の 2 つのタスクを動かす。送信側はハブが積んだフレームを順に送るだけで、
遅いクライアントのぶんはハブ側のキューが古い append から捨てる。`snapshot` と `session_start` に
載せるアンカー座標は DB を読むので、この接続の送信タスクの中でスレッドを使って引く。
ハブ (受信ループと定期処理) は DB を待たない。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from location_server.api.deps import snapshot_for_session
from location_server.api.schemas import to_live_anchor_out
from location_server.live import LiveHub, Subscriber
from location_server.live.hub import HISTORY_MS_DEFAULT, HISTORY_MS_MAX
from location_server.store import ConfigStore
from location_server.units import TAG_ID_MAX, TAG_ID_MIN

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["live"])


class SubscribeOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["subscribe"]
    tag_id: int = Field(ge=TAG_ID_MIN, le=TAG_ID_MAX)
    history_ms: int = Field(default=HISTORY_MS_DEFAULT, ge=0, le=HISTORY_MS_MAX)


class UnsubscribeOp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["unsubscribe"]


_OPS: TypeAdapter[SubscribeOp | UnsubscribeOp] = TypeAdapter(
    Annotated[SubscribeOp | UnsubscribeOp, Field(discriminator="op")]
)


def _error(detail: str) -> dict[str, Any]:
    return {"type": "error", "detail": detail}


def _describe(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}" for err in exc.errors()
    )


def _anchors_for(store: ConfigStore, config_rev: int | None) -> dict[str, Any]:
    snapshot = snapshot_for_session(store, config_rev)
    return {
        "anchors_rev": snapshot.meta.rev,
        "anchors": [to_live_anchor_out(anchor).model_dump() for anchor in snapshot.enabled_anchors],
    }


async def _receive_ops(websocket: WebSocket, hub: LiveHub, subscriber: Subscriber) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        text = message.get("text")
        if text is None:
            subscriber.push(_error("テキストフレームで JSON を送ってください"))
            continue
        try:
            op = _OPS.validate_json(text)
        except ValidationError as exc:
            subscriber.push(_error(_describe(exc)))
            continue
        if isinstance(op, SubscribeOp):
            hub.subscribe(subscriber, op.tag_id, op.history_ms)
        else:
            hub.unsubscribe(subscriber)


async def _send_frames(websocket: WebSocket, subscriber: Subscriber, store: ConfigStore) -> None:
    while True:
        frame = await subscriber.next()
        kind = frame["type"]
        if kind in ("snapshot", "session_start"):
            try:
                extra = await asyncio.to_thread(_anchors_for, store, frame.get("config_rev"))
            except Exception:
                # 座標が引けなくても計測値の配信は止めない。ページ側は座標なしで描く
                logger.exception("ライブ配信のアンカー座標を読めませんでした")
                extra = {"anchors_rev": None, "anchors": []}
            frame = {**frame, **extra}
        elif kind == "append":
            frame = {**frame, "lost": subscriber.lost}
        await websocket.send_text(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))


@router.websocket("/ws/live")
async def live(websocket: WebSocket) -> None:
    hub: LiveHub = websocket.app.state.live
    store: ConfigStore = websocket.app.state.store
    await websocket.accept()
    subscriber = hub.connect()
    tasks = {
        asyncio.create_task(_receive_ops(websocket, hub, subscriber), name="live-ws-receive"),
        asyncio.create_task(_send_frames(websocket, subscriber, store), name="live-ws-send"),
    }
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.warning("ライブ配信の接続を閉じます: %r", exc)
    finally:
        hub.disconnect(subscriber)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

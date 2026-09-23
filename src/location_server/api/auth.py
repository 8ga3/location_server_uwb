"""書き込み系 API の共有トークン検証。

LAN 内利用を前提に既定では無効とし、`UWB_AUTH_TOKEN` を設定したときだけ有効になる。
有効にした場合は書き込み系のすべてのエンドポイントへ一貫して適用する (設計文書 5 節)。
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Request, status

AUTH_HEADER = "X-Auth-Token"


def _tokens_match(presented: str, expected: str) -> bool:
    """トークンを定数時間で比較する。

    `secrets.compare_digest()` は非 ASCII を含む `str` を渡すと `TypeError` を送出する。
    Starlette はヘッダのバイト列を latin-1 で復号するため、0x80..0xFF のバイトを 1 つ送るだけで
    この例外に到達できてしまう。バイト列へ直してから比較し、長さの違いも含めて必ず真偽値を返す。
    """
    return secrets.compare_digest(
        presented.encode("utf-8", "surrogateescape"),
        expected.encode("utf-8", "surrogateescape"),
    )


def require_write_token(request: Request, x_auth_token: str | None = Header(default=None)) -> None:
    """トークンが設定されている場合のみ検証する FastAPI 依存関数。"""
    expected: str | None = request.app.state.settings.auth_token
    if expected is None:
        return
    if x_auth_token is None or not _tokens_match(x_auth_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{AUTH_HEADER} ヘッダが必要です",
        )

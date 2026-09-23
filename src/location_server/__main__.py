"""`python -m location_server` でサーバーを起動する。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from location_server import __version__
from location_server.api import create_app
from location_server.settings import Settings, load_settings


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="location-server",
        description="UWB 測位デバッグ用の構成配信サーバーを起動する",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", type=Path, default=None, help="SQLite ファイルのパス")
    parser.add_argument("--host", default=None, help="待ち受けアドレス")
    parser.add_argument("--port", type=int, default=None, help="待ち受けポート")
    parser.add_argument("--log-level", default="info", help="uvicorn のログレベル")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    base = load_settings()
    settings = Settings(
        db_path=base.db_path if args.db is None else args.db,
        host=base.host if args.host is None else args.host,
        port=base.port if args.port is None else args.port,
        auth_token=base.auth_token,
    )
    logging.basicConfig(level=args.log_level.upper())
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

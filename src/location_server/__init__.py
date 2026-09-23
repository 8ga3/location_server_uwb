"""測位デバッグ用サーバーのパッケージ。

フェーズ A では構成配信 (アンカー座標の管理と `GET /api/v1/config`) だけを提供する。
"""

from location_server.version import __version__, get_version

__all__ = ["__version__", "get_version"]

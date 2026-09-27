"""測位デバッグ用サーバーのパッケージ。

構成配信 (アンカー座標の管理と `GET /api/v1/config`) とテレメトリ収集 (UDP 受信と一括保存) を提供する。
"""

from location_server.version import __version__, get_version

__all__ = ["__version__", "get_version"]

"""起動設定のテスト。"""

from __future__ import annotations

import pytest

from location_server.__main__ import _parse_args
from location_server.settings import DEFAULT_UDP_PORT, load_settings, udp_port_setting


def test_udp_port_defaults_to_design_value() -> None:
    assert load_settings({}).udp_port == DEFAULT_UDP_PORT == 47100


def test_udp_port_zero_disables_ingest() -> None:
    assert load_settings({"UWB_UDP_PORT": "0"}).udp_port is None
    assert udp_port_setting(0) is None
    assert udp_port_setting(47101) == 47101


def test_udp_port_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        udp_port_setting(65536)
    with pytest.raises(ValueError):
        udp_port_setting(-1)


def test_cli_accepts_udp_port() -> None:
    assert _parse_args(["--udp-port", "0"]).udp_port == 0

"""単位と識別子の変換のテスト。"""

from __future__ import annotations

import pytest

from location_server.units import (
    ValueRangeError,
    check_anchor_id,
    check_tag_id,
    format_hex_id,
    meters_to_mm,
    mm_to_meters,
    parse_hex_id,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0x0100", 0x0100), ("0X0100", 0x0100), ("0xFFFE", 0xFFFE), ("256", 256), (" 257 ", 257)],
)
def test_parse_hex_id(text: str, expected: int) -> None:
    assert parse_hex_id(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "0x", "abc", "0x0100.5", "1_0", "-1", "+1", "0x 100"])
def test_parse_hex_id_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueRangeError):
        parse_hex_id(text)


def test_format_hex_id() -> None:
    assert format_hex_id(0x0100) == "0x0100"
    assert format_hex_id(0xFFFE) == "0xFFFE"


@pytest.mark.parametrize("value", [0x0100, 0x0101, 0xFFFE])
def test_check_anchor_id_accepts_valid_range(value: int) -> None:
    assert check_anchor_id(value) == value


@pytest.mark.parametrize("value", [0, 1, 0x00FF, 0xFFFF, -1])
def test_check_anchor_id_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueRangeError):
        check_anchor_id(value)


@pytest.mark.parametrize("value", [0x0001, 0x00FF])
def test_check_tag_id_accepts_valid_range(value: int) -> None:
    assert check_tag_id(value) == value


@pytest.mark.parametrize("value", [0, 0x0100])
def test_check_tag_id_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueRangeError):
        check_tag_id(value)


@pytest.mark.parametrize(
    ("meters", "expected_mm"),
    [
        (0.0, 0),
        (5.12, 5120),
        (-5.12, -5120),
        (4.3105, 4311),  # 0.5 mm 相当は切り上げに統一する
        (0.0005, 1),
        (1.8, 1800),
        (0.001, 1),
    ],
)
def test_meters_to_mm(meters: float, expected_mm: int) -> None:
    assert meters_to_mm(meters) == expected_mm


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 1e9])
def test_meters_to_mm_rejects_unusable_values(value: float) -> None:
    with pytest.raises(ValueRangeError):
        meters_to_mm(value)


def test_mm_to_meters_roundtrip() -> None:
    for mm in (0, 1, -1, 5120, -4311, 1800):
        assert meters_to_mm(mm_to_meters(mm)) == mm

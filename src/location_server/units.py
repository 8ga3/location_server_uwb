"""単位と識別子の変換。

座標は API の JSON でのみメートル表記とし、通信と DB 内部では整数ミリメートルで保持する
(設計文書 4 節)。浮動小数の丸め差で同じ値が一致しなくなる事故を避けるため、
メートルからミリメートルへの変換は十進数として扱い、四捨五入で整数化する。
"""

from __future__ import annotations

import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# アンカー ID は UWB の responderAddress そのもの (設計文書 5.4)
ANCHOR_ID_MIN = 0x0100
ANCHOR_ID_MAX = 0xFFFE

# タグ ID の範囲 (設計文書 4.1 の session.tag_id)
TAG_ID_MIN = 0x0001
TAG_ID_MAX = 0x00FF

# 座標のミリメートル値が収まるべき範囲。符号付き 32 ビット整数に合わせる
COORD_MM_MIN = -(2**31)
COORD_MM_MAX = 2**31 - 1

_HEX_ID_PATTERN = re.compile(r"0[xX](?P<digits>[0-9a-fA-F]+)")
_DECIMAL_ID_PATTERN = re.compile(r"[0-9]+")

# 受け付ける桁数の上限。扱う ID は 16 ビットなので 16 進 4 桁・10 進 5 桁あれば足りる。
# 余裕を持たせつつ上限を設けるのは、CPython の int_max_str_digits (既定 4300) を超える
# 長さの数字列で int() が ValueError を送出するのを、その手前で防ぐため。
_MAX_ID_DIGITS = 16


class ValueRangeError(ValueError):
    """値が仕様上の範囲に収まっていない場合に送出する。"""


def parse_hex_id(text: str) -> int:
    """`0x0100` 形式または 10 進数表記の ID を整数へ変換する。

    暗黙の補正は行わず、解釈できない文字列はすべて `ValueRangeError` とする。
    Python の数値リテラル表記 (`1_0` のようなアンダースコア区切り) も受け付けない。
    桁数が極端に長い入力は `int()` へ渡す前に弾く。
    """
    stripped = text.strip()
    hex_match = _HEX_ID_PATTERN.fullmatch(stripped)
    if hex_match is not None:
        digits = hex_match.group("digits")
        if len(digits) > _MAX_ID_DIGITS:
            raise ValueRangeError(f"ID の桁数が多すぎます: {len(digits)} 桁")
        return int(digits, 16)
    if _DECIMAL_ID_PATTERN.fullmatch(stripped) is not None:
        if len(stripped) > _MAX_ID_DIGITS:
            raise ValueRangeError(f"ID の桁数が多すぎます: {len(stripped)} 桁")
        return int(stripped, 10)
    raise ValueRangeError(f"ID として解釈できません: {text!r}")


def format_hex_id(value: int) -> str:
    """整数の ID を JSON 表記の `0x0100` 形式へ変換する。"""
    return f"0x{value:04X}"


def check_anchor_id(value: int) -> int:
    """アンカー ID が `0x0100..0xFFFE` に収まっているか検証する。"""
    if not ANCHOR_ID_MIN <= value <= ANCHOR_ID_MAX:
        raise ValueRangeError(
            f"アンカー ID は {format_hex_id(ANCHOR_ID_MIN)}..{format_hex_id(ANCHOR_ID_MAX)} "
            f"の範囲です: {format_hex_id(value) if value >= 0 else value}"
        )
    return value


def check_tag_id(value: int) -> int:
    """タグ ID が `0x0001..0x00FF` に収まっているか検証する。"""
    if not TAG_ID_MIN <= value <= TAG_ID_MAX:
        raise ValueRangeError(
            f"タグ ID は {format_hex_id(TAG_ID_MIN)}..{format_hex_id(TAG_ID_MAX)} の範囲です: {value}"
        )
    return value


def meters_to_mm(value: float) -> int:
    """メートル表記の座標を整数ミリメートルへ変換する。

    `round()` の偶数丸めでは 0.5 mm 相当の入力が値によって切り上げ・切り捨てに分かれるため、
    十進数の四捨五入 (ROUND_HALF_UP) で統一する。
    """
    if not math.isfinite(value):
        raise ValueRangeError(f"座標に有限でない値は使えません: {value}")
    try:
        scaled = Decimal(str(value)).scaleb(3).to_integral_value(rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueRangeError(f"座標として解釈できません: {value}") from exc
    result = int(scaled)
    if not COORD_MM_MIN <= result <= COORD_MM_MAX:
        raise ValueRangeError(f"座標が扱える範囲を超えています: {value} m")
    return result


def mm_to_meters(value: int) -> float:
    """整数ミリメートルの座標をメートル表記へ変換する。"""
    return value / 1000

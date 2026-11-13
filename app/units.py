"""单位折算：把 mg/L、μg/L 统一换算到限值单位。"""
from __future__ import annotations

import re
from typing import Tuple

# 以 mg/L 为基准的换算系数：1 单位 == factor mg/L
_TO_MG_PER_L = {
    "mg/l": 1.0,
    "ug/l": 0.001,
    "μg/l": 0.001,
    "µg/l": 0.001,
}

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


class UnitError(ValueError):
    """单位无法识别或不在支持范围内。"""


def normalize_unit(unit: str) -> str:
    """归一化单位写法（去空白、统一大小写、micro 符号统一）。"""
    u = (unit or "").strip().lower()
    u = u.replace("µ", "μ").replace("ug/l", "μ" + "g/l")
    return u


def convert_value(value: float, from_unit: str, to_unit: str) -> float:
    """将 value 从 from_unit 折算到 to_unit。

    仅支持 mg/L 与 μg/L 互转；其它单位抛出 UnitError。
    """
    src = normalize_unit(from_unit)
    dst = normalize_unit(to_unit)
    if src not in _TO_MG_PER_L:
        raise UnitError(f"不支持的实测单位: {from_unit!r}（仅支持 mg/L、μg/L）")
    if dst not in _TO_MG_PER_L:
        raise UnitError(f"不支持的限值单位: {to_unit!r}（仅支持 mg/L、μg/L）")
    mg_per_l = value * _TO_MG_PER_L[src]
    return mg_per_l / _TO_MG_PER_L[dst]


def split_quantity(text: str) -> Tuple[float, str]:
    """拆分 '0.012 mg/L'、'<0.005μg/L' 这类文本中的数值与单位。

    若文本中没有可识别的数值则抛 ValueError；没有单位时单位返回空串。
    """
    s = (text or "").strip()
    m = _NUM_RE.search(s)
    if not m:
        raise ValueError(f"无法从 {text!r} 中解析数值")
    value = float(m.group())
    unit = (s[: m.start()] + " " + s[m.end():])
    for token in ("<", "≤", "<=", ">", "≥", ">=", "≈", "=", "＝", " "):
        unit = unit.replace(token, " ")
    return value, unit.strip()

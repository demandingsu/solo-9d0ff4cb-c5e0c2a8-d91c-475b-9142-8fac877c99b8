"""指标判定与水样汇总结论。"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from .parser import DetailRecord
from .schemas import IndicatorResult, LimitSpec
from .units import UnitError, convert_value, split_quantity

# 结论常量
OVER = "超限"
WITHIN = "未超限"
REVIEW = "待核实"

SAMPLE_RECHECK = "复检"
SAMPLE_WITHIN = "未超限"
SAMPLE_REVIEW = "待核实"

_NOT_DETECTED_TOKENS = ("未检出", "检出限", "ND", "N.D.", "nd")
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _is_not_detected(raw: str) -> bool:
    s = (raw or "").strip()
    if not s:
        return False
    if s in _NOT_DETECTED_TOKENS:
        return True
    return any(tok in s for tok in ("未检出", "检出限"))


def _is_below_detection(raw: str) -> bool:
    """形如 <0.005、＜0.01。"""
    s = (raw or "").strip()
    return bool(s) and s[0] in "<＜"


def evaluate_indicator(
    record: DetailRecord, limit: LimitSpec, source_url: str
) -> IndicatorResult:
    """按限值判定单条指标记录。"""
    raw = (record.raw_value or "").strip()
    base = IndicatorResult(
        name=record.indicator,
        unit=record.unit,
        raw_value=raw,
        limit_value=limit.value,
        limit_unit=limit.unit,
        source_url=source_url,
    )

    if not raw:
        base.conclusion = REVIEW
        base.note = "实测值缺失，无法判定"
        return base

    # ---- 情形一：未检出 / 检出限表述 ----
    if _is_not_detected(raw):
        m = _NUM_RE.search(raw)
        if not m:
            base.conclusion = REVIEW
            base.note = "未检出但未给出检出限，无法核实"
            return base
        detection = float(m.group())
        det_unit = record.unit
        if not det_unit:
            base.conclusion = REVIEW
            base.note = f"未检出，检出限 {detection} 缺少单位"
            return base
        try:
            det_in_limit = convert_value(detection, det_unit, limit.unit)
        except UnitError as exc:
            base.conclusion = REVIEW
            base.note = str(exc)
            return base
        base.converted_value = det_in_limit
        if det_in_limit <= limit.value:
            base.conclusion = WITHIN
            base.note = f"未检出，检出限 {detection}{det_unit} 不高于上限"
        else:
            base.conclusion = REVIEW
            base.note = (
                f"未检出，但检出限 {detection}{det_unit}（折算 {det_in_limit:g}"
                f"{limit.unit}）高于上限 {limit.value:g}{limit.unit}"
            )
        return base

    # ---- 情形二：<数值 ----
    if _is_below_detection(raw):
        try:
            detection, inline_unit = split_quantity(raw)
        except ValueError:
            base.conclusion = REVIEW
            base.note = f"无法解析检出限数值: {raw!r}"
            return base
        det_unit = inline_unit or record.unit
        if not det_unit:
            base.conclusion = REVIEW
            base.note = f"{raw} 缺少单位"
            return base
        try:
            det_in_limit = convert_value(detection, det_unit, limit.unit)
        except UnitError as exc:
            base.conclusion = REVIEW
            base.note = str(exc)
            return base
        base.converted_value = det_in_limit
        if det_in_limit <= limit.value:
            base.conclusion = WITHIN
            base.note = f"实测值低于检出限 {detection}{det_unit}，检出限不高于上限"
        else:
            base.conclusion = REVIEW
            base.note = (
                f"实测值低于检出限 {detection}{det_unit}（折算 {det_in_limit:g}"
                f"{limit.unit}），检出限高于上限 {limit.value:g}{limit.unit}"
            )
        return base

    # ---- 情形三：普通实测值 ----
    try:
        measured, inline_unit = split_quantity(raw)
    except ValueError:
        base.conclusion = REVIEW
        base.note = f"无法解析实测值: {raw!r}"
        return base
    measured_unit = inline_unit or record.unit
    if not measured_unit:
        base.conclusion = REVIEW
        base.note = "实测值缺少单位"
        return base
    try:
        converted = convert_value(measured, measured_unit, limit.unit)
    except UnitError as exc:
        base.conclusion = REVIEW
        base.note = str(exc)
        return base
    base.converted_value = converted
    if converted > limit.value:
        base.conclusion = OVER
        base.note = (
            f"实测 {measured:g}{measured_unit} 折算 {converted:g}{limit.unit}"
            f" > 上限 {limit.value:g}{limit.unit}"
        )
    else:
        base.conclusion = WITHIN
        base.note = (
            f"实测 {measured:g}{measured_unit} 折算 {converted:g}{limit.unit}"
            f" ≤ 上限 {limit.value:g}{limit.unit}"
        )
    return base


def evaluate_approved(
    approved_value: float,
    approved_unit: str,
    limit: Optional[LimitSpec],
) -> tuple:
    """用复核核准值替换原实测值，按现有限值重新判定。

    返回 ``(converted_value, conclusion, note)``。复核值单位仅支持
    mg/L / μg/L；限值缺失或单位无法折算时结论为待核实。
    """
    if limit is None:
        return None, REVIEW, "该指标没有可用限值，无法判定复核值"
    try:
        converted = convert_value(approved_value, approved_unit, limit.unit)
    except UnitError as exc:
        return None, REVIEW, str(exc)
    if converted > limit.value:
        return (
            converted,
            OVER,
            f"复核值 {approved_value:g}{approved_unit} 折算 {converted:g}"
            f"{limit.unit} > 上限 {limit.value:g}{limit.unit}",
        )
    return (
        converted,
        WITHIN,
        f"复核值 {approved_value:g}{approved_unit} 折算 {converted:g}"
        f"{limit.unit} ≤ 上限 {limit.value:g}{limit.unit}",
    )


def sample_conclusion(indicator_conclusions: List[str]) -> str:
    """水样级汇总：超限优先，其次待核实，否则未超限。"""
    conclusions = set(indicator_conclusions)
    if OVER in conclusions:
        return SAMPLE_RECHECK
    if REVIEW in conclusions:
        return SAMPLE_REVIEW
    return SAMPLE_WITHIN


def build_limit_index(limits: List[LimitSpec]) -> Dict[str, LimitSpec]:
    """按归一化指标名建立限值索引。"""
    index: Dict[str, LimitSpec] = {}
    for spec in limits:
        index["".join(spec.name.split())] = spec
    return index


def aggregate_samples(
    records: List[DetailRecord],
    limits: List[LimitSpec],
    source_url: str,
) -> List[dict]:
    """把同一详情页的指标行按样本编号汇总，给出水样级结论。"""
    limit_index = build_limit_index(limits)
    grouped: Dict[str, List[DetailRecord]] = {}
    points: Dict[str, Optional[str]] = {}
    order: List[str] = []

    for rec in records:
        code = rec.sample_code or "(未知编号)"
        if code not in grouped:
            grouped[code] = []
            points[code] = rec.sampling_point
            order.append(code)
        grouped[code].append(rec)
        if not points[code] and rec.sampling_point:
            points[code] = rec.sampling_point

    samples: List[dict] = []
    for code in order:
        indicator_results: List[IndicatorResult] = []
        for rec in grouped[code]:
            spec = limit_index.get("".join(rec.indicator.split()))
            if spec is None:
                indicator_results.append(
                    IndicatorResult(
                        name=rec.indicator,
                        unit=rec.unit,
                        raw_value=rec.raw_value or "",
                        limit_value=None,
                        limit_unit="",
                        conclusion=REVIEW,
                        note="请求中未提供该指标的限值，待核实",
                        source_url=source_url,
                    )
                )
                continue
            indicator_results.append(evaluate_indicator(rec, spec, source_url))

        conclusions = [r.conclusion for r in indicator_results]
        summary = sample_conclusion(conclusions)

        samples.append(
            {
                "sample_code": code,
                "sampling_point": points[code],
                "conclusion": summary,
                "detail_url": source_url,
                "indicators": [r.model_dump() for r in indicator_results],
            }
        )
    return samples

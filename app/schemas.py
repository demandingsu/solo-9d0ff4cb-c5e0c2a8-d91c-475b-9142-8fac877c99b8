"""请求与响应数据模型。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class LimitSpec(BaseModel):
    """单个指标的限值（名称、单位、数值）。"""

    name: str = Field(..., description="指标名称，如 砷 / 铅 / 浊度")
    unit: str = Field(..., description="限值单位，如 mg/L、μg/L")
    value: float = Field(..., gt=0, description="限值数值，仅支持正上限")


class CollectRequest(BaseModel):
    """采集 API 的请求体。"""

    list_url: str = Field(..., description="监测站抽检记录列表页网址")
    limits: List[LimitSpec] = Field(..., min_length=1, description="各指标上限")
    timeout: float = Field(15.0, gt=0, le=60.0, description="单次请求超时秒数")


class IndicatorResult(BaseModel):
    """单条指标行的判定结果。"""

    name: str
    unit: str
    raw_value: str = Field(..., description="页面原始值，如 0.012 / <0.005 / 未检出")
    converted_value: Optional[float] = Field(
        None, description="折算到限值单位后的实测值（无法折算时为 None）"
    )
    limit_value: Optional[float] = Field(
        None, description="对应限值；请求未提供该指标限值时为 None"
    )
    limit_unit: str
    conclusion: str = Field(
        "", description="超限 / 未超限 / 待核实"
    )
    note: Optional[str] = None
    source_url: str


class SampleResult(BaseModel):
    """一份水样的汇总结论。"""

    sample_code: str
    sampling_point: Optional[str] = None
    conclusion: str = Field(..., description="复检 / 未超限 / 待核实")
    detail_url: str
    indicators: List[IndicatorResult]


class CollectResponse(BaseModel):
    """采集结果汇总。"""

    list_url: str
    list_pages_visited: List[str]
    detail_pages_visited: List[str]
    samples: List[SampleResult]
    failed_urls: List[str] = Field(
        default_factory=list, description="读取失败的页面网址；非空时结果不完整"
    )
    incomplete: bool = Field(False, description="是否存在失败页面，结果是否不完整")
    counts: dict

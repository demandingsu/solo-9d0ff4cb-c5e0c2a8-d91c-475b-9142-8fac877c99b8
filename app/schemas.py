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
    published: bool = Field(
        False,
        description="是否已把本轮完整结果写入 SQLite；不完整、失败或重复键时不发布",
    )
    run_id: Optional[int] = Field(None, description="已发布结果对应的记录编号")
    duplicate_keys: List[str] = Field(
        default_factory=list,
        description="同轮采集内重复的 详情URL/样本编号/指标 定位键；非空时不发布",
    )
    publish_note: Optional[str] = Field(
        None, description="未发布原因；发布成功时为 None"
    )
    stored_results_url: Optional[str] = Field(
        None, description="发布后可用于查询该入口结果的 GET 地址"
    )


# ---------------------------------------------------------------- 复核与查询


class ReviewItemIn(BaseModel):
    """单条人工复核：按 详情URL + 样本编号 + 指标 定位，并带上查询到的修订号。"""

    detail_url: str = Field(..., description="指标所在详情页网址")
    sample_code: str = Field(..., min_length=1, description="样本编号")
    indicator: str = Field(..., min_length=1, description="指标名称")
    revision: int = Field(..., ge=0, description="查询结果中该指标行的当前修订号")
    approved_value: float = Field(..., ge=0, description="核准数值（非负）")
    unit: str = Field(..., description="核准值单位，仅支持 mg/L 或 μg/L")
    reason: str = Field(..., min_length=1, description="复核理由")
    reviewer: Optional[str] = Field(None, description="复核人（可选）")


class ReviewRequest(BaseModel):
    """人工复核请求；整批原子生效，任一行修订号过期则整批不生效。"""

    list_url: str = Field(..., description="采集时使用的列表入口网址")
    reviews: List[ReviewItemIn] = Field(..., min_length=1, description="待复核指标")


class ReviewApplied(BaseModel):
    """单条复核生效结果。"""

    detail_url: str
    sample_code: str
    indicator: str
    revision: int = Field(..., description="复核后的新修订号")
    approved_value: float
    approved_unit: str
    reason: str
    reviewer: Optional[str] = None
    created_at: str
    indicator_view: dict = Field(
        default_factory=dict, description="复核后的指标行（保留原始行与有效值）"
    )
    sample: dict = Field(
        default_factory=dict, description="复核后该水样的最新结论"
    )


class ReviewResponse(BaseModel):
    """复核提交结果。"""

    list_url: str
    run_id: int
    applied: List[ReviewApplied]


class StoredIndicator(BaseModel):
    """查询返回的指标行：原始行、修订号与当前生效值。"""

    name: str
    unit: str
    raw_value: str = Field(..., description="采集到的原始值，永不覆盖")
    converted_value: Optional[float] = None
    limit_value: Optional[float] = None
    limit_unit: str = ""
    collected_conclusion: str = Field(..., description="采集时的原始判定")
    collected_note: Optional[str] = None
    revision: int = Field(..., description="当前修订号，复核时须原样带回")
    review: Optional[dict] = Field(None, description="最新复核记录；未复核为 None")
    effective_value: Optional[float] = Field(
        None, description="当前生效值：有复核时为核准值，否则为折算实测值"
    )
    effective_unit: str = ""
    effective_converted_value: Optional[float] = None
    conclusion: str = Field(..., description="当前生效判定（复核后按限值重算）")
    conclusion_note: Optional[str] = None


class StoredSample(BaseModel):
    """查询返回的水样。"""

    sample_code: str
    sampling_point: Optional[str] = None
    detail_url: str
    conclusion: str = Field(..., description="当前生效结论（含复核影响）")
    collected_conclusion: str = Field(..., description="仅按采集值计算的结论")
    indicators: List[StoredIndicator]


class ResultsResponse(BaseModel):
    """某入口最新已发布结果的查询响应。"""

    list_url: str
    run_id: int
    published_at: str
    incomplete: bool = False
    failed_urls: List[str] = Field(default_factory=list)
    list_pages_visited: List[str] = Field(default_factory=list)
    detail_pages_visited: List[str] = Field(default_factory=list)
    limits: List[LimitSpec] = Field(default_factory=list)
    samples: List[StoredSample]
    counts: dict


class ReviewHistoryItem(BaseModel):
    detail_url: str
    sample_code: str
    indicator: str
    raw_value: str = Field(..., description="复核时的原始值快照")
    base_revision: int
    new_revision: int
    approved_value: float
    approved_unit: str
    reason: str
    reviewer: Optional[str] = None
    created_at: str


class ReviewHistoryResponse(BaseModel):
    """某入口当前结果的复核历史。"""

    list_url: str
    run_id: int
    reviews: List[ReviewHistoryItem]

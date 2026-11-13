"""FastAPI 入口：饮用水抽检采集、持久化查询与人工复核 API。"""
from __future__ import annotations

import asyncio
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query

from .crawler import collect
from .schemas import (
    CollectRequest,
    CollectResponse,
    ResultsResponse,
    ReviewHistoryResponse,
    ReviewRequest,
    ReviewResponse,
)
from .storage import (
    KeyConflict,
    NotFound,
    ReviewItem,
    RevisionConflict,
    find_duplicate_keys,
    get_storage,
)
from .units import UnitError

app = FastAPI(
    title="饮用水抽检采集与复核 API",
    description=(
        "提交列表页网址与指标限值，抓取同站分页与详情，"
        "还原 rowspan 跨行表格并折算单位，输出复检/未超限/待核实结论。\n\n"
        "**采集**（POST /api/collect）在结果完整时自动按入口网址保存到 SQLite；"
        "不完整、失败或同轮出现重复定位键时不发布，原数据保持不变。\n\n"
        "**查询**（GET /api/results）返回原始行、修订号与复核后的生效结论。\n\n"
        "**复核**（POST /api/reviews）按 详情 URL + 样本编号 + 指标 定位，"
        "提交修订号、核准值、单位（mg/L 或 μg/L）与理由；原值保留，"
        "按现有限值重算水样结论；修订号过期返回 409。"
    ),
    version="2.0.0",
)


def _storage_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RevisionConflict):
        return HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "conflicts": exc.conflicts,
            },
        )
    if isinstance(exc, UnitError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, KeyConflict):
        return HTTPException(
            status_code=409,
            detail={"message": str(exc), "duplicate_keys": exc.keys},
        )
    raise exc


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/collect", response_model=CollectResponse)
async def api_collect(req: CollectRequest) -> dict:
    try:
        result = await collect(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response = {
        **result,
        "published": False,
        "run_id": None,
        "duplicate_keys": [],
        "publish_note": None,
    }

    duplicates = find_duplicate_keys(result["samples"])
    if duplicates:
        response["duplicate_keys"] = duplicates
        response["publish_note"] = (
            "同一轮采集出现重复的 详情URL/样本编号/指标 定位键，未发布会话结果"
        )
        return response

    if result["incomplete"] or result["failed_urls"]:
        response["publish_note"] = "采集不完整（存在失败页面），未发布，保留原有数据"
        return response

    try:
        run_id = await asyncio.to_thread(
            get_storage().publish,
            list_url=result["list_url"],
            limits=[spec.model_dump() for spec in req.limits],
            samples=result["samples"],
            failed_urls=result["failed_urls"],
            list_pages=result["list_pages_visited"],
            detail_pages=result["detail_pages_visited"],
            counts=result["counts"],
        )
    except KeyConflict as exc:
        response["duplicate_keys"] = exc.keys
        response["publish_note"] = "同一轮采集出现重复定位键，未发布"
        return response
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    response["published"] = True
    response["run_id"] = run_id
    response["stored_results_url"] = f"/api/results?list_url={quote(result['list_url'], safe='')}"
    return response


@app.get("/api/results", response_model=ResultsResponse)
def api_results(
    list_url: str = Query(..., description="采集时使用的列表入口网址"),
) -> dict:
    try:
        return get_storage().get_results(list_url)
    except Exception as exc:  # noqa: BLE001 - 统一转换为 HTTP 错误
        raise _storage_errors(exc)


@app.post("/api/reviews", response_model=ReviewResponse)
def api_review(req: ReviewRequest) -> dict:
    items = [
        ReviewItem(
            detail_url=item.detail_url,
            sample_code=item.sample_code,
            indicator=item.indicator,
            revision=item.revision,
            approved_value=item.approved_value,
            unit=item.unit,
            reason=item.reason,
            reviewer=item.reviewer,
        )
        for item in req.reviews
    ]
    try:
        outcomes = get_storage().apply_reviews(req.list_url, items)
    except Exception as exc:  # noqa: BLE001 - 统一转换为 HTTP 错误
        raise _storage_errors(exc)

    return {
        "list_url": req.list_url,
        "run_id": outcomes[0].run_id,
        "applied": [
            {
                "detail_url": o.detail_url,
                "sample_code": o.sample_code,
                "indicator": o.indicator,
                "revision": o.revision,
                "approved_value": o.approved_value,
                "approved_unit": o.approved_unit,
                "reason": o.reason,
                "reviewer": o.reviewer,
                "created_at": o.created_at,
                "indicator_view": o.indicator_view,
                "sample": o.sample,
            }
            for o in outcomes
        ],
    }


@app.get("/api/reviews", response_model=ReviewHistoryResponse)
def api_review_history(
    list_url: str = Query(..., description="采集时使用的列表入口网址"),
) -> dict:
    try:
        return get_storage().get_reviews(list_url)
    except Exception as exc:  # noqa: BLE001 - 统一转换为 HTTP 错误
        raise _storage_errors(exc)

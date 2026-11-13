"""FastAPI 入口：饮用水抽检记录采集 API。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .crawler import collect
from .schemas import CollectRequest, CollectResponse

app = FastAPI(
    title="饮用水抽检采集 API",
    description=(
        "提交列表页网址与指标限值，抓取同站分页与详情，"
        "还原 rowspan 跨行表格并折算单位，输出复检/未超限/待核实结论。"
    ),
    version="1.0.0",
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/collect", response_model=CollectResponse)
async def api_collect(req: CollectRequest) -> dict:
    try:
        return await collect(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

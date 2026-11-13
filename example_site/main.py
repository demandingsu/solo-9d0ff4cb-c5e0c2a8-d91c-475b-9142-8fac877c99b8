"""本地示例站：托管 example-site 静态页，并可演示慢页、改值与失败。

除静态页面外提供控制接口（默认不带任何故障，采集可完整通过）：

* ``POST /control/slow``  ``{"path": "/detail/s005.html", "seconds": 3}``
  让指定路径延迟返回，用于演示“复采等待页面时复核仍能完成”。
* ``POST /control/value`` ``{"sample_code": "S003", "indicator": "砷",
  "raw_value": "0.02"}`` 修改详情页中的实测值，用于演示原始行变化。
* ``POST /control/fail``  ``{"path": "/detail/s006.html", "status": 500}``
  让指定路径失败（默认 500），用于演示失败页面与不完整采集。
* ``POST /control/reset`` 恢复初始状态。
* ``GET  /control/state`` 查看当前控制状态。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Dict, Optional

from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

BASE_DIR = Path(
    os.environ.get("EXAMPLE_SITE_DIR", Path(__file__).resolve().parent.parent / "example-site")
).resolve()

app = FastAPI(title="本地示例站", version="2.0.0")

# 路径 -> 秒数；路径 -> 失败状态码；样本+指标 -> 新的实测值
_delays: Dict[str, float] = {}
_failures: Dict[str, int] = {}
_value_overrides: Dict[str, str] = {}


class SlowIn(BaseModel):
    path: str = Field(..., description="路径，如 /detail/s005.html")
    seconds: float = Field(..., ge=0, le=60)


class ValueIn(BaseModel):
    sample_code: str = Field(..., min_length=1)
    indicator: str = Field(..., min_length=1)
    raw_value: str = Field(..., min_length=1)


class FailIn(BaseModel):
    path: str = Field(..., description="路径，如 /detail/s006.html")
    status: int = Field(500, ge=400, le=599)
    enabled: bool = True


def _normalize(path: str) -> str:
    clean = "/" + path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
    return "/" if clean == "/" else clean.rstrip("/") or "/"


def _override_key(sample_code: str, indicator: str) -> str:
    return f"{sample_code.strip()}|{''.join(indicator.split())}"


def _apply_value_overrides(path: Path, html: str) -> str:
    """按样本编号与指标替换详情表中的实测值单元格。"""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        current_sample: Optional[str] = None
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) >= 5 and cells[0].name == "td":
                current_sample = cells[0].get_text(strip=True)
                indicator_cell, value_cell = cells[2], cells[4]
            elif len(cells) == 3 and cells[0].name == "td" and current_sample:
                indicator_cell, value_cell = cells[0], cells[2]
            else:
                continue
            indicator = "".join(indicator_cell.get_text(strip=True).split())
            key = _override_key(current_sample, indicator)
            if key in _value_overrides:
                value_cell.string = _value_overrides[key]
    return str(soup)


@app.get("/control/state")
def control_state() -> dict:
    return {
        "delays": dict(_delays),
        "failures": dict(_failures),
        "value_overrides": [
            {"sample_code": k.split("|", 1)[0], "indicator": k.split("|", 1)[1], "raw_value": v}
            for k, v in _value_overrides.items()
        ],
    }


@app.post("/control/slow")
def control_slow(body: SlowIn) -> dict:
    path = _normalize(body.path)
    if body.seconds <= 0:
        _delays.pop(path, None)
    else:
        _delays[path] = body.seconds
    return {"path": path, "seconds": _delays.get(path, 0)}


@app.post("/control/value")
def control_value(body: ValueIn) -> dict:
    key = _override_key(body.sample_code, body.indicator)
    _value_overrides[key] = body.raw_value
    return {"sample_code": body.sample_code, "indicator": body.indicator, "raw_value": body.raw_value}


@app.post("/control/fail")
def control_fail(body: FailIn) -> dict:
    path = _normalize(body.path)
    if not body.enabled:
        _failures.pop(path, None)
    else:
        _failures[path] = body.status
    return {"path": path, "status": _failures.get(path), "enabled": body.enabled}


@app.post("/control/reset")
def control_reset() -> dict:
    _delays.clear()
    _failures.clear()
    _value_overrides.clear()
    return {"reset": True}


@app.get("/")
def index() -> Response:
    return RedirectResponse("/list/index.html")


@app.get("/{full_path:path}")
async def serve(full_path: str) -> Response:
    path = _normalize("/" + full_path)

    if path in _failures:
        return JSONResponse(
            {"detail": f"示例站模拟失败：{path}"},
            status_code=_failures[path],
        )

    delay = _delays.get(path)
    if delay:
        await asyncio.sleep(delay)

    if path.endswith("/"):
        path += "index.html"

    target = (BASE_DIR / path.lstrip("/")).resolve()
    if BASE_DIR not in target.parents and target != BASE_DIR:
        raise HTTPException(status_code=404, detail="路径越界")
    if not target.is_file():
        return HTMLResponse(
            (BASE_DIR / "404.html").read_text(encoding="utf-8")
            if (BASE_DIR / "404.html").is_file()
            else "<h1>404</h1>",
            status_code=404,
        )

    if target.suffix.lower() in (".html", ".htm"):
        html = target.read_text(encoding="utf-8")
        if path.startswith("/detail/"):
            html = _apply_value_overrides(target, html)
        return HTMLResponse(html)
    return Response(target.read_bytes(), media_type="application/octet-stream")

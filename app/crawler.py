"""抓取同站分页列表与详情页，聚合复检结论。"""
from __future__ import annotations

from typing import List, Set, Tuple
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .evaluator import aggregate_samples
from .parser import parse_detail_html
from .schemas import CollectRequest, LimitSpec

_PAGINATION_REL = {"next", "last", "prev", "first", "previous"}
_PAGINATION_TEXT = ("下一页", "上一页", "尾页", "首页", "末页", "更多", "next", "previous", "last", "first", "»", "›")
_MAX_PAGES = 50  # 防止异常站点造成无限翻页


def _same_site(url: str, base_netloc: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and parts.netloc == base_netloc


def _is_pagination(a_tag) -> bool:
    rel = {r.lower() for r in (a_tag.get("rel") or [])}
    if rel & _PAGINATION_REL:
        return True
    text = a_tag.get_text(strip=True).lower()
    aria = (a_tag.get("aria-label") or "").lower()
    if any(tok in text for tok in _PAGINATION_TEXT) or any(
        tok in aria for tok in _PAGINATION_TEXT
    ):
        return True
    # 位于分页容器内
    parent = a_tag.parent
    for _ in range(3):
        if parent is None:
            break
        cls = " ".join(parent.get("class") or []).lower()
        role = (parent.get("role") or "").lower()
        if "pagination" in cls or "pager" in cls or role == "navigation":
            return True
        parent = parent.parent
    return False


def _extract_links(html: str, base_url: str) -> Tuple[List[str], List[str]]:
    """返回 (分页链接, 其它同站链接)，均已绝对化、去片段。"""
    soup = BeautifulSoup(html, "html.parser")
    base_netloc = urlsplit(base_url).netloc
    pagination: List[str] = []
    others: List[str] = []
    seen: Set[str] = set()

    for a in soup.find_all("a", href=True):
        absolute, _ = urldefrag(urljoin(base_url, a["href"]))
        if not _same_site(absolute, base_netloc) or absolute in seen:
            continue
        seen.add(absolute)
        if _is_pagination(a):
            pagination.append(absolute)
        else:
            others.append(absolute)
    return pagination, others


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url, follow_redirects=True)
    resp.raise_for_status()
    # 避免把非 HTML（如 PDF）当页面解析
    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and "xml" not in content_type:
        raise ValueError(f"响应不是 HTML 页面（content-type: {content_type or '未知'}）")
    return resp.text


async def collect(req: CollectRequest) -> dict:
    """主采集流程。"""
    limits: List[LimitSpec] = req.limits
    list_url, _ = urldefrag(req.list_url)
    if urlsplit(list_url).scheme not in ("http", "https"):
        raise ValueError("list_url 必须是 http/https 绝对网址")

    failed_urls: List[str] = []
    list_pages: List[str] = []
    detail_pages: List[str] = []
    detail_candidates: Set[str] = set()
    samples: List[dict] = []

    async with httpx.AsyncClient(
        timeout=req.timeout,
        headers={"User-Agent": "water-quality-collector/1.0"},
    ) as client:
        # 1) 广度优先遍历同站分页
        queue: List[str] = [list_url]
        queued: Set[str] = {list_url}
        while queue and len(list_pages) < _MAX_PAGES:
            url = queue.pop(0)
            try:
                html = await _fetch(client, url)
            except Exception:
                failed_urls.append(url)
                continue
            list_pages.append(url)
            pagination, others = _extract_links(html, url)
            detail_candidates.update(others)
            for p in pagination:
                if p not in queued:
                    queued.add(p)
                    queue.append(p)

        # 列表页本身通常也指向详情，已在 others 中；去掉列表页自身
        detail_candidates.difference_update(list_pages)

        # 2) 逐个抓取详情页，解析跨行表格
        for detail_url in sorted(detail_candidates):
            try:
                html = await _fetch(client, detail_url)
                records = parse_detail_html(html)
            except Exception:
                failed_urls.append(detail_url)
                continue
            detail_pages.append(detail_url)
            samples.extend(
                aggregate_samples(records, limits, detail_url)
            )

    counts = {
        "list_pages": len(list_pages),
        "detail_pages_ok": len(detail_pages),
        "samples_total": len(samples),
        "recheck": sum(1 for s in samples if s["conclusion"] == "复检"),
        "within_limit": sum(1 for s in samples if s["conclusion"] == "未超限"),
        "need_review": sum(1 for s in samples if s["conclusion"] == "待核实"),
        "failed_pages": len(failed_urls),
    }

    return {
        "list_url": list_url,
        "list_pages_visited": list_pages,
        "detail_pages_visited": detail_pages,
        "samples": samples,
        "failed_urls": failed_urls,
        "incomplete": bool(failed_urls),
        "counts": counts,
    }

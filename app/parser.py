"""用 BeautifulSoup 解析详情表，正确还原 rowspan/colspan 跨行归属。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from bs4 import BeautifulSoup, Tag

# 表头别名 -> 标准字段
_HEADER_ALIASES = {
    "样本编号": "sample_code",
    "样品编号": "sample_code",
    "水样编号": "sample_code",
    "编号": "sample_code",
    "采样点": "sampling_point",
    "采样地点": "sampling_point",
    "监测点": "sampling_point",
    "采样点位": "sampling_point",
    "指标": "indicator",
    "指标名称": "indicator",
    "检测项目": "indicator",
    "项目": "indicator",
    "项目名称": "indicator",
    "单位": "unit",
    "实测值": "value",
    "检测结果": "value",
    "测定值": "value",
    "结果": "value",
    "实测结果": "value",
}

REQUIRED_FIELDS = ("sample_code", "indicator", "unit", "value")


@dataclass
class DetailRecord:
    """还原跨行后得到的一行指标数据。"""

    sample_code: str
    sampling_point: Optional[str]
    indicator: str
    unit: str
    raw_value: str


def _norm_header(text: str) -> Optional[str]:
    key = "".join((text or "").split())
    return _HEADER_ALIASES.get(key)


def _build_grid(table: Tag) -> List[List[Tag]]:
    """把含 rowspan/colspan 的表格展开成规则二维网格。

    同一单元格对象会被放入它覆盖的每个网格位置，
    从而把被跨行的样本编号/采样点还原到每一行。
    """
    grid: List[List[Optional[Tag]]] = []
    # pending[col] = (剩余跨行数, 单元格)
    pending: Dict[int, tuple] = {}

    rows = table.find_all("tr")
    for tr in rows:
        cells = tr.find_all(["td", "th"])
        row: List[Optional[Tag]] = []
        col = 0
        cell_iter = iter(cells)
        while col < len(row) or True:
            # 先处理该列上尚未结束的跨行单元格
            if col in pending:
                remain, cell = pending[col]
                row.append(cell)
                if remain - 1 <= 0:
                    del pending[col]
                else:
                    pending[col] = (remain - 1, cell)
                col += 1
                continue
            try:
                cell = next(cell_iter)
            except StopIteration:
                break
            rowspan = int(cell.get("rowspan", 1) or 1)
            colspan = int(cell.get("colspan", 1) or 1)
            for _ in range(colspan):
                row.append(cell)
                if rowspan > 1:
                    pending[col] = (rowspan - 1, cell)
                col += 1
        grid.append(row)
    return grid  # type: ignore[return-value]


def _cell_text(cell: Optional[Tag]) -> str:
    if cell is None:
        return ""
    return cell.get_text(strip=True)


def parse_detail_html(html: str) -> List[DetailRecord]:
    """解析详情页 HTML，返回还原跨行后的指标记录列表。

    页面内若有多张表，选取表头包含全部必需字段的那张。
    """
    soup = BeautifulSoup(html, "html.parser")
    chosen = None
    header_fields: List[Optional[str]] = []

    for table in soup.find_all("table"):
        grid = _build_grid(table)
        if not grid:
            continue
        fields = [_norm_header(_cell_text(c)) for c in grid[0]]
        if all(f in fields for f in REQUIRED_FIELDS):
            chosen = grid
            header_fields = fields
            break

    if chosen is None:
        raise ValueError("详情页未找到包含 样本编号/指标/单位/实测值 表头的表格")

    idx = {f: i for i, f in enumerate(header_fields) if f}
    records: List[DetailRecord] = []
    for row in chosen[1:]:
        def get(field: str) -> str:
            i = idx.get(field)
            return _cell_text(row[i]) if i is not None and i < len(row) else ""

        sample_code = get("sample_code")
        indicator = get("indicator")
        if not sample_code and not indicator:
            continue  # 空行/分隔行
        records.append(
            DetailRecord(
                sample_code=sample_code,
                sampling_point=get("sampling_point") or None,
                indicator=indicator,
                unit=get("unit"),
                raw_value=get("value"),
            )
        )
    if not records:
        raise ValueError("详情表格没有数据行")
    return records


def find_page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""

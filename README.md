# 饮用水抽检采集 API

提交监测站抽检记录**列表页网址**与各**指标上限（名称、单位、数值）**，服务会：

1. 抓取同站分页列表（按 `rel=next/prev`、分页文字与分页容器识别翻页链接，仅限同站点）；
2. 抓取列表中每个详情页，用 BeautifulSoup 解析检测表；
3. 还原 `rowspan` / `colspan` 跨行表格，把样本编号、采样点归属到每一条指标行；
4. 将 `mg/L`、`μg/L` 实测值折算到限值单位后判定；
5. 输出每份水样的结论（**复检 / 未超限 / 待核实**）、原值、折算值与来源链接；
6. 页面读取失败时收集失败网址，并以 `incomplete=true` 标明结果不完整。

## 判定规则

| 实测值形态 | 判定 |
|---|---|
| 普通数值，折算后 > 上限 | **超限**（该水样 → 复检） |
| 普通数值，折算后 ≤ 上限 | 未超限 |
| `<数值` / `未检出（检出限 x）`，检出限折算后 ≤ 上限 | 未超限 |
| `<数值` / `未检出`，检出限缺失或折算后 > 上限 | **待核实** |
| 单位不支持（非 mg/L、μg/L）、数值无法解析、未提供该指标限值 | 待核实（附原因 note） |

水样级汇总：任一指标超限 → **复检**；否则任一指标待核实 → **待核实**；全部未超限 → **未超限**。

## 目录结构

```
.
├── app/                    # FastAPI 服务
│   ├── main.py             # 路由：POST /api/collect、GET /health
│   ├── crawler.py          # 分页发现、同站过滤、抓取与失败收集
│   ├── parser.py           # rowspan/colspan 表格还原（BeautifulSoup）
│   ├── evaluator.py        # 检出限规则与限值判定、水样汇总
│   ├── units.py            # mg/L ↔ μg/L 折算
│   └── schemas.py          # Pydantic 请求/响应模型
├── example-site/           # 本地分页示例站（nginx 静态页，含跨行表格与 404）
│   ├── list/index.html     # 列表第 1 页（rel=next 翻页）
│   ├── list/page2.html     # 列表第 2 页（含一个已下线的 404 详情链接）
│   └── detail/s001..s005.html
├── nginx/default.conf
├── tests/                  # unittest：单位、跨行解析、判定逻辑（24 个用例）
├── Dockerfile
└── docker-compose.yml
```

## 用 Docker Compose 启动

```bash
docker compose up --build
```

- API：http://localhost:8000 （交互式文档 /docs）
- 示例站：http://localhost:8080/list/index.html

## 请求示例

```bash
curl -X POST http://localhost:8000/api/collect \
  -H "Content-Type: application/json" \
  -d '{
    "list_url": "http://example-site:80/list/index.html",
    "limits": [
      {"name": "砷",   "unit": "mg/L", "value": 0.01},
      {"name": "铅",   "unit": "mg/L", "value": 0.01},
      {"name": "浊度", "unit": "mg/L", "value": 1.0}
    ]
  }'
```

> 在宿主机上直接调用时，把 `example-site` 换成 `localhost:8080`。

## 响应要点

```json
{
  "samples": [
    {
      "sample_code": "S002",
      "sampling_point": "临江二水厂管网末梢",
      "conclusion": "复检",
      "detail_url": "http://.../detail/s002.html",
      "indicators": [
        {
          "name": "砷",
          "unit": "μg/L",
          "raw_value": "18",
          "converted_value": 0.018,
          "limit_value": 0.01,
          "limit_unit": "mg/L",
          "conclusion": "超限",
          "note": "实测 18μg/L 折算 0.018mg/L > 上限 0.01mg/L",
          "source_url": "http://.../detail/s002.html"
        }
      ]
    }
  ],
  "failed_urls": ["http://.../detail/s006-removed.html"],
  "incomplete": true,
  "counts": {"recheck": 2, "within_limit": 2, "need_review": 2, "failed_pages": 1}
}
```

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 另开一个终端托管示例站
python -m http.server 8080 -d example-site
python -m unittest discover -s tests -v
```

## 设计说明与边界

- 只跟随与入口 URL **同 netloc** 的 http/https 链接，避免爬到外站；
- 分页识别支持 `rel` 属性、“下一页/上一页/尾页”等文字、`aria-label` 与分页容器类名；
- 翻页设上限（50 页）防止异常站点导致无限循环；
- 非 HTML 响应（content-type 不含 html/xml）按失败处理并记入 `failed_urls`；
- 单位折算仅支持 `mg/L`、`μg/L`（含 `ug/L`、`µg/L` 写法），其它单位判待核实；
- 同一详情表中可含多份水样（不同 rowspan 分组），按样本编号分别汇总。

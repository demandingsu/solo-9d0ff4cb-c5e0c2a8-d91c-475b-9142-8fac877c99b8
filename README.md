# 饮用水抽检采集与人工复核 API

提交监测站抽检记录**列表页网址**与各**指标上限（名称、单位、数值）**，服务会：

1. 抓取同站分页列表（按 `rel=next/prev`、分页文字与分页容器识别翻页链接，仅限同站点）；
2. 抓取列表中每个详情页，用 BeautifulSoup 解析检测表；
3. 还原 `rowspan` / `colspan` 跨行表格，把样本编号、采样点归属到每一条指标行；
4. 将 `mg/L`、`μg/L` 实测值折算到限值单位后判定；
5. 输出每份水样的结论（**复检 / 未超限 / 待核实**）、原值、折算值与来源链接；
6. **完整采集**成功后按入口网址把结果保存到 SQLite（`published`）；
   采集不完整/失败、或同轮出现重复定位键时**不发布**，原有数据保持不变；
7. 提供**人工复核**：质控员为“待核实”指标补入核准值（mg/L 或 μg/L）并写明理由，
   原始值保留、修订号递增，按现有限值重算水样结论。

## 判定规则

| 实测值形态 | 判定 |
|---|---|
| 普通数值，折算后 > 上限 | **超限**（该水样 → 复检） |
| 普通数值，折算后 ≤ 上限 | 未超限 |
| `<数值` / `未检出（检出限 x）`，检出限折算后 ≤ 上限 | 未超限 |
| `<数值` / `未检出`，检出限缺失或折算后 > 上限 | **待核实** |
| 单位不支持（非 mg/L、μg/L）、数值无法解析、未提供该指标限值 | 待核实（附原因 note） |

水样级汇总：任一指标超限 → **复检**；否则任一指标待核实 → **待核实**；全部未超限 → **未超限**。
人工复核后，被复核指标改用核准值判定，再按同一规则重算水样结论。

## 目录结构

```
.
├── app/                    # FastAPI 服务
│   ├── main.py             # 路由：/api/collect、/api/results、/api/reviews、/health
│   ├── crawler.py          # 分页发现、同站过滤、抓取与失败收集
│   ├── parser.py           # rowspan/colspan 表格还原（BeautifulSoup）
│   ├── evaluator.py        # 检出限规则、限值判定、复核值判定、水样汇总
│   ├── storage.py          # SQLite 发布/查询/复核（修订号、复核沿用规则）
│   ├── units.py            # mg/L ↔ μg/L 折算
│   └── schemas.py          # Pydantic 请求/响应模型
├── example_site/main.py    # 本地示例站（控制慢页、改值、失败）
├── example-site/           # 示例站静态页（跨行表格、分页）
├── tests/                  # unittest：单位、解析、判定、存储、HTTP、示例站
├── Dockerfile
└── docker-compose.yml
```

## 用 Docker Compose 启动

```bash
docker compose up --build
```

- API 与交互式文档：http://localhost:8000/docs
- 示例站：http://localhost:8080/list/index.html
- 示例站控制接口：http://localhost:8080/docs
- SQLite 数据在命名卷 `water-data` 中，容器重启后结果与复核仍在。

## 接口一览

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/collect` | 采集；完整时发布到 SQLite |
| GET | `/api/results?list_url=...` | 查询原始行、修订号、复核与生效结论 |
| POST | `/api/reviews` | 提交复核（整批原子，修订号过期返回 409） |
| GET | `/api/reviews?list_url=...` | 查询当前结果的复核记录 |
| GET | `/health` | 健康检查 |

### 采集

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

响应在原有 `samples` / `failed_urls` / `incomplete` / `counts` 之外增加：

- `published`：本轮是否已写入 SQLite；
- `run_id`：已发布记录编号；
- `duplicate_keys`：同轮重复的 `详情URL / 样本编号 / 指标` 键；
- `publish_note`：未发布原因。

未发布的情形（结果仍照常返回，只是不落库、不覆盖旧数据）：

- `incomplete=true` 或存在 `failed_urls`；
- 同一轮内 `(详情 URL, 样本编号, 指标)` 重复。

### 查询

```bash
curl 'http://localhost:8000/api/results?list_url=http%3A%2F%2Fexample-site%3A80%2Flist%2Findex.html'
```

每条指标行返回：

- `raw_value` / `unit` / `converted_value` / `limit_value` 等原始行数据；
- `revision`：当前修订号（复核时须原样带回）；
- `review`：最新复核记录（核准值、单位、理由、复核人、时间），未复核为 `null`；
- `effective_value` / `effective_unit` / `effective_converted_value`：当前生效值；
- `collected_conclusion`（只按采集值）与 `conclusion`（含复核影响）。

水样级同时给出 `conclusion` 与 `collected_conclusion`。

### 复核

```bash
curl -X POST http://localhost:8000/api/reviews \
  -H "Content-Type: application/json" \
  -d '{
    "list_url": "http://localhost:8080/list/index.html",
    "reviews": [{
      "detail_url": "http://localhost:8080/detail/s003.html",
      "sample_code": "S003",
      "indicator": "砷",
      "revision": 0,
      "approved_value": 0.008,
      "unit": "mg/L",
      "reason": "实验室复测确认",
      "reviewer": "质控-张"
    }]
  }'
```

规则：

- 定位键为 `详情 URL + 样本编号 + 指标`（URL 去片段、host 大小写不敏感）；
- 单位仅接受 `mg/L`、`μg/L`（含 `ug/L`、`µg/L` 写法），否则 422；
- `revision` 必须等于查询到的当前修订号，否则整批不生效并返回 **409**，错误体列出
  `expected_revision` / `current_revision`；
- 复核只更新核准字段，`raw_value` 等原始行数据永不覆盖；复核记录与水样结论在同一事务内生效；
- 多条复核一次提交时整批原子：任一条过期或定位不到，其余也不写入。

### 复核的沿用规则

新一轮完整采集发布时（按 `详情 URL / 样本编号 / 指标` 对应）：

| 原始行 | 处理 |
|---|---|
| 样本编号、采样点、指标、单位、原值全部未变 | **保留**该行最新复核与修订号 |
| 原始行任一字段变化 | 丢弃旧复核（历史记录保留），修订号 +1 |
| 本轮不再出现 | 置为非活动（查询不可见），修订号 +1；以后重现也不沿用旧复核 |

“复采等待页面时复核仍能完成”由 SQLite 短连接实现：采集抓取慢页面期间不持有写锁，
复核请求可正常提交；随后发布时若原始行未变，复核被保留。

## 演示流程（/docs）

1. `POST /api/collect` 采集示例站，看到 `published=true`；
2. `GET /api/results` 找到 `待核实` 指标与 `revision`；
3. `POST /api/reviews` 提交核准值、单位与理由，响应中的 `sample.conclusion` 已按新值重算；
4. 再提交同一 `revision` 会得到 **409**（过期修订号）；
5. 在示例站 `POST /control/value` 修改该指标原值后再采集，`review` 变为 `null`、
   `revision` 前进；原复核仍可在 `GET /api/reviews` 查到；
6. `POST /control/slow` 让详情页变慢后再采集，期间仍可提交复核；
7. `POST /control/fail` 让某页返回 5xx 再采集，`published=false`、`incomplete=true`，
   而 `GET /api/results` 仍是上一次完整数据。

示例站控制接口（http://localhost:8080/docs）：

| 方法 | 路径 | 参数 |
|---|---|---|
| POST | `/control/slow` | `{"path": "/detail/s005.html", "seconds": 3}` |
| POST | `/control/value` | `{"sample_code": "S003", "indicator": "砷", "raw_value": "0.035"}` |
| POST | `/control/fail` | `{"path": "/detail/s002.html", "status": 500, "enabled": true}` |
| POST | `/control/reset` | 无 |
| GET | `/control/state` | 无 |

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 另开一个终端托管示例站
uvicorn example_site.main:app --reload --port 8080
python -m unittest discover -s tests -v
```

本地运行时数据库默认在 `./data/water_quality.db`（可用 `WQ_DB_PATH` 覆盖），
示例站页面目录可用 `EXAMPLE_SITE_DIR` 覆盖。

## 设计说明与边界

- 只跟随与入口 URL **同 netloc** 的 http/https 链接，避免爬到外站；
- 分页识别支持 `rel` 属性、“下一页/上一页/尾页”等文字、`aria-label` 与分页容器类名；
- 翻页设上限（50 页）防止异常站点导致无限循环；
- 非 HTML 响应（content-type 不含 html/xml）按失败处理并记入 `failed_urls`；
- 单位折算仅支持 `mg/L`、`μg/L`（含 `ug/L`、`µg/L` 写法），其它单位判待核实；
- 同一详情表中可含多份水样（不同 rowspan 分组），按样本编号分别汇总；
- 同一入口网址只保留最新一轮已发布结果；被替换的活动行保留在库中（`active=0`），
  复核记录不删除。

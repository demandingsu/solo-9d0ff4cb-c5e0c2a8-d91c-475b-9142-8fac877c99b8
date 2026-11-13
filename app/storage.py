"""SQLite 持久化：完整采集结果按入口网址保存，并提供人工复核。

数据模型要点：

* 一轮已发布的采集（run）以规范化后的入口 `list_url` 为键；只有完整采集才会
  发布。采集不完整/失败时调用方不发布，因此库中始终保留上一次完整数据。
* 指标行以 `(详情 URL, 样本编号, 指标名称)` 定位；详情 URL 去掉片段并统一
  scheme/host 大小写后比较。
* 每个指标行保存**原始行快照**（样本编号、采样点、指标、单位、原值及其哈希）
  与单调递增的**修订号** `revision`。人工复核只追加复核记录、更新行的核准字段，
  `raw_value` 等原始值永不覆盖。
* 新一轮发布时：
  - 原始行哈希未变 → 沿用该行最新复核与修订号；
  - 原始行改变 → 丢弃旧复核，修订号 +1；
  - 原始行消失 → 置为非活动、丢弃复核、修订号 +1（即使以后重现也不沿用）。
* 复核提交需带查询得到的修订号；修订号过期时整批复核回滚并报冲突。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple
from urllib.parse import urldefrag, urlsplit, urlunsplit

from .evaluator import (
    build_limit_index,
    evaluate_approved,
    sample_conclusion,
)
from .schemas import LimitSpec
from .units import UnitError, normalize_unit

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "water_quality.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    list_url       TEXT    NOT NULL,
    list_url_key   TEXT    NOT NULL UNIQUE,
    limits_json    TEXT    NOT NULL,
    incomplete     INTEGER NOT NULL DEFAULT 0,
    failed_urls    TEXT    NOT NULL DEFAULT '[]',
    list_pages     TEXT    NOT NULL DEFAULT '[]',
    detail_pages   TEXT    NOT NULL DEFAULT '[]',
    counts_json    TEXT    NOT NULL DEFAULT '{}',
    created_at     TEXT    NOT NULL,
    published_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS indicator_rows (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    detail_url            TEXT    NOT NULL,
    detail_url_key        TEXT    NOT NULL,
    sample_code           TEXT    NOT NULL,
    indicator_key         TEXT    NOT NULL,
    indicator_name        TEXT    NOT NULL,
    sampling_point        TEXT,
    raw_value             TEXT    NOT NULL,
    unit                  TEXT    NOT NULL,
    collected_converted   REAL,
    collected_limit_value REAL,
    collected_limit_unit  TEXT    NOT NULL DEFAULT '',
    collected_conclusion  TEXT    NOT NULL,
    collected_note        TEXT,
    row_hash              TEXT    NOT NULL,
    revision              INTEGER NOT NULL DEFAULT 0,
    review_id             INTEGER,
    reviewed_value        REAL,
    reviewed_unit         TEXT,
    review_reason         TEXT,
    reviewer              TEXT,
    reviewed_at           TEXT,
    active                INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    UNIQUE (run_id, detail_url_key, sample_code, indicator_key)
);

CREATE TABLE IF NOT EXISTS reviews (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    row_id             INTEGER NOT NULL REFERENCES indicator_rows(id) ON DELETE CASCADE,
    detail_url         TEXT    NOT NULL,
    sample_code        TEXT    NOT NULL,
    indicator_name     TEXT    NOT NULL,
    raw_value          TEXT    NOT NULL,
    base_revision      INTEGER NOT NULL,
    new_revision       INTEGER NOT NULL,
    approved_value     REAL    NOT NULL,
    approved_unit      TEXT    NOT NULL,
    reason             TEXT    NOT NULL,
    reviewer           TEXT,
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rows_run_active
    ON indicator_rows (run_id, active);
CREATE INDEX IF NOT EXISTS idx_reviews_row
    ON reviews (row_id, new_revision);
"""


class StorageError(Exception):
    """存储层错误基类。"""


class NotFound(StorageError):
    """定位不到已发布的结果或指标行。"""


class RevisionConflict(StorageError):
    """提交的修订号已过期。"""

    def __init__(self, conflicts: List[dict]):
        self.conflicts = conflicts
        super().__init__("修订号已过期，请重新查询后再提交")


class KeyConflict(StorageError):
    """同轮采集出现重复定位键，拒绝发布。"""

    def __init__(self, keys: List[str]):
        self.keys = keys
        super().__init__("同轮采集出现重复定位键: " + "; ".join(keys))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_url(url: str) -> str:
    """规范化 URL：去片段、统一 scheme/host 大小写、补空路径。"""
    clean, _ = urldefrag((url or "").strip())
    parts = urlsplit(clean)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def indicator_key(name: str) -> str:
    return "".join((name or "").split())


def _row_hash(
    sample_code: str,
    sampling_point: Optional[str],
    indicator: str,
    unit: str,
    raw_value: str,
) -> str:
    payload = json.dumps(
        [sample_code, sampling_point or "", indicator, unit or "", raw_value or ""],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find_duplicate_keys(samples: List[dict]) -> List[str]:
    """检查一轮采集内部是否有重复定位键，返回可读的重复键列表。"""
    seen: set[Tuple[str, str, str]] = set()
    duplicates: List[str] = []
    for sample in samples:
        durl = canonical_url(sample.get("detail_url", ""))
        code = (sample.get("sample_code") or "").strip()
        for ind in sample.get("indicators", []):
            loc = (durl, code, indicator_key(ind.get("name", "")))
            if loc in seen:
                duplicates.append(
                    f"{durl} / {code} / {ind.get('name', '')}"
                )
            else:
                seen.add(loc)
    return duplicates


def validate_review_unit(unit: str) -> str:
    """复核单位仅允许 mg/L、μg/L（含 ug/L、µg/L 写法），返回统一写法。"""
    normalized = normalize_unit(unit)
    if normalized == "mg/l":
        return "mg/L"
    if normalized == "μg/l":
        return "μg/L"
    raise UnitError(f"复核单位仅支持 mg/L、μg/L，收到 {unit!r}")


@dataclass
class ReviewItem:
    """单条复核请求。"""

    detail_url: str
    sample_code: str
    indicator: str
    revision: int
    approved_value: float
    unit: str
    reason: str
    reviewer: Optional[str] = None


@dataclass
class ReviewOutcome:
    list_url: str
    run_id: int
    detail_url: str
    sample_code: str
    indicator: str
    revision: int
    approved_value: float
    approved_unit: str
    reason: str
    reviewer: Optional[str]
    created_at: str
    indicator_view: dict = field(default_factory=dict)
    sample: dict = field(default_factory=dict)


class Storage:
    """线程安全的 SQLite 存储；每次操作使用独立短连接。"""

    def __init__(self, path: Optional[str] = None):
        self.path = str(
            path
            or os.environ.get("WQ_DB_PATH")
            or _DEFAULT_DB
        )
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------------------------------------------------------------- 发布
    def publish(
        self,
        *,
        list_url: str,
        limits: List[dict],
        samples: List[dict],
        failed_urls: List[str],
        list_pages: List[str],
        detail_pages: List[str],
        counts: dict,
    ) -> int:
        """发布一轮完整采集结果；同轮重复键则整体拒绝，返回 run id。"""
        key = canonical_url(list_url)
        if not key:
            raise ValueError("list_url 不能为空")

        duplicates = find_duplicate_keys(samples)
        if duplicates:
            raise KeyConflict(duplicates)

        now = _now()
        limit_specs = [
            LimitSpec(**item) if isinstance(item, dict) else item for item in limits
        ]

        with self._lock, self._connect() as conn:
            existing_run = conn.execute(
                "SELECT id FROM runs WHERE list_url_key = ?", (key,)
            ).fetchone()
            if existing_run is None:
                run_id = int(
                    conn.execute(
                        """INSERT INTO runs (list_url, list_url_key, limits_json,
                               incomplete, failed_urls, list_pages, detail_pages,
                               counts_json, created_at, published_at)
                           VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
                        (
                            list_url,
                            key,
                            json.dumps(
                                [s.model_dump() for s in limit_specs],
                                ensure_ascii=False,
                            ),
                            json.dumps(failed_urls, ensure_ascii=False),
                            json.dumps(list_pages, ensure_ascii=False),
                            json.dumps(detail_pages, ensure_ascii=False),
                            json.dumps(counts, ensure_ascii=False),
                            now,
                            now,
                        ),
                    ).lastrowid
                )
            else:
                run_id = int(existing_run["id"])
                conn.execute(
                    """UPDATE runs SET list_url = ?, limits_json = ?, incomplete = 0,
                           failed_urls = ?, list_pages = ?, detail_pages = ?,
                           counts_json = ?, published_at = ?
                       WHERE id = ?""",
                    (
                        list_url,
                        json.dumps(
                            [s.model_dump() for s in limit_specs],
                            ensure_ascii=False,
                        ),
                        json.dumps(failed_urls, ensure_ascii=False),
                        json.dumps(list_pages, ensure_ascii=False),
                        json.dumps(detail_pages, ensure_ascii=False),
                        json.dumps(counts, ensure_ascii=False),
                        now,
                        run_id,
                    ),
                )

            old_rows = conn.execute(
                "SELECT * FROM indicator_rows WHERE run_id = ?", (run_id,)
            ).fetchall()
            old_by_key = {
                (r["detail_url_key"], r["sample_code"], r["indicator_key"]): r
                for r in old_rows
            }

            incoming: Dict[Tuple[str, str, str], Tuple[dict, dict]] = {}
            for sample in samples:
                durl_key = canonical_url(sample.get("detail_url", ""))
                code = (sample.get("sample_code") or "").strip()
                for ind in sample.get("indicators", []):
                    ikey = indicator_key(ind.get("name", ""))
                    incoming[(durl_key, code, ikey)] = (sample, ind)

            # 1) 本轮未出现的旧行：不再活动、丢弃复核、修订号前进
            for loc, old in old_by_key.items():
                if loc in incoming:
                    continue
                if old["active"]:
                    conn.execute(
                        """UPDATE indicator_rows SET active = 0, revision = revision + 1,
                               review_id = NULL, reviewed_value = NULL,
                               reviewed_unit = NULL, review_reason = NULL,
                               reviewer = NULL, reviewed_at = NULL, updated_at = ?
                           WHERE id = ?""",
                        (now, old["id"]),
                    )

            # 2) 逐行写入/更新
            for loc, (sample, ind) in incoming.items():
                durl_key, code, ikey = loc
                name = ind.get("name", "")
                unit = ind.get("unit", "") or ""
                raw_value = ind.get("raw_value", "") or ""
                sampling_point = sample.get("sampling_point")
                rhash = _row_hash(code, sampling_point, name, unit, raw_value)
                old = old_by_key.get(loc)

                common = (
                    name,
                    sampling_point,
                    raw_value,
                    unit,
                    ind.get("converted_value"),
                    ind.get("limit_value"),
                    ind.get("limit_unit", "") or "",
                    ind.get("conclusion", ""),
                    ind.get("note"),
                    rhash,
                    now,
                )

                if old is None:
                    conn.execute(
                        """INSERT INTO indicator_rows
                           (run_id, detail_url, detail_url_key, sample_code,
                            indicator_key, indicator_name, sampling_point,
                            raw_value, unit, collected_converted,
                            collected_limit_value, collected_limit_unit,
                            collected_conclusion, collected_note, row_hash,
                            revision, active, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)""",
                        (
                            run_id,
                            sample.get("detail_url", ""),
                            durl_key,
                            code,
                            ikey,
                            name,
                            sampling_point,
                            raw_value,
                            unit,
                            ind.get("converted_value"),
                            ind.get("limit_value"),
                            ind.get("limit_unit", "") or "",
                            ind.get("conclusion", ""),
                            ind.get("note"),
                            rhash,
                            now,
                            now,
                        ),
                    )
                    continue

                if old["row_hash"] == rhash and old["active"]:
                    # 原始行未变：保留修订号与最新复核，仅刷新采集判定（限值可能变）
                    conn.execute(
                        """UPDATE indicator_rows SET indicator_name = ?,
                               sampling_point = ?, raw_value = ?, unit = ?,
                               collected_converted = ?, collected_limit_value = ?,
                               collected_limit_unit = ?, collected_conclusion = ?,
                               collected_note = ?, row_hash = ?, updated_at = ?
                           WHERE id = ?""",
                        common + (old["id"],),
                    )
                else:
                    # 原始行改变，或曾经消失后重现：旧复核不再沿用
                    conn.execute(
                        """UPDATE indicator_rows SET indicator_name = ?,
                               sampling_point = ?, raw_value = ?, unit = ?,
                               collected_converted = ?, collected_limit_value = ?,
                               collected_limit_unit = ?, collected_conclusion = ?,
                               collected_note = ?, row_hash = ?, updated_at = ?,
                               active = 1, revision = revision + 1, review_id = NULL,
                               reviewed_value = NULL, reviewed_unit = NULL,
                               review_reason = NULL, reviewer = NULL,
                               reviewed_at = NULL
                           WHERE id = ?""",
                        common + (old["id"],),
                    )

        return run_id

    # ---------------------------------------------------------------- 查询
    def _load_run(self, conn: sqlite3.Connection, list_url: str) -> sqlite3.Row:
        key = canonical_url(list_url)
        run = conn.execute(
            "SELECT * FROM runs WHERE list_url_key = ?", (key,)
        ).fetchone()
        if run is None:
            raise NotFound(f"入口 {list_url!r} 尚无已发布的采集结果")
        return run

    def _review_view(self, row: sqlite3.Row, limit: Optional[LimitSpec]) -> dict:
        approved_value = row["reviewed_value"]
        approved_unit = row["reviewed_unit"] or ""
        review = None
        effective_value: Optional[float] = approved_value
        effective_unit = approved_unit
        effective_converted = None
        conclusion = row["collected_conclusion"]
        note = row["collected_note"]

        if approved_value is not None:
            review = {
                "approved_value": approved_value,
                "approved_unit": approved_unit,
                "reason": row["review_reason"],
                "reviewer": row["reviewer"],
                "created_at": row["reviewed_at"],
            }
            converted, conclusion, note = evaluate_approved(
                approved_value, approved_unit, limit
            )
            effective_converted = converted
        else:
            effective_value = row["collected_converted"]
            effective_unit = row["collected_limit_unit"] or row["unit"]

        return {
            "name": row["indicator_name"],
            "unit": row["unit"],
            "raw_value": row["raw_value"],
            "converted_value": row["collected_converted"],
            "limit_value": row["collected_limit_value"],
            "limit_unit": row["collected_limit_unit"],
            "collected_conclusion": row["collected_conclusion"],
            "collected_note": row["collected_note"],
            "revision": row["revision"],
            "review": review,
            "effective_value": effective_value,
            "effective_unit": effective_unit,
            "effective_converted_value": effective_converted,
            "conclusion": conclusion,
            "conclusion_note": note,
        }

    @staticmethod
    def _sample_conclusion(indicator_views: List[dict]) -> str:
        return sample_conclusion([v["conclusion"] for v in indicator_views])

    def _samples_for_run(self, conn: sqlite3.Connection, run: sqlite3.Row) -> List[dict]:
        limits = [LimitSpec(**x) for x in json.loads(run["limits_json"])]
        limit_index = build_limit_index(limits)
        rows = conn.execute(
            """SELECT * FROM indicator_rows
               WHERE run_id = ? AND active = 1
               ORDER BY detail_url_key, sample_code, id""",
            (run["id"],),
        ).fetchall()

        grouped: Dict[Tuple[str, str], List[dict]] = {}
        meta: Dict[Tuple[str, str], dict] = {}
        order: List[Tuple[str, str]] = []
        for row in rows:
            loc = (row["detail_url_key"], row["sample_code"])
            if loc not in grouped:
                grouped[loc] = []
                order.append(loc)
                meta[loc] = {
                    "sample_code": row["sample_code"],
                    "sampling_point": row["sampling_point"],
                    "detail_url": row["detail_url"],
                }
            elif not meta[loc]["sampling_point"] and row["sampling_point"]:
                meta[loc]["sampling_point"] = row["sampling_point"]
            limit = limit_index.get(row["indicator_key"])
            grouped[loc].append(self._review_view(row, limit))

        samples: List[dict] = []
        for loc in order:
            indicators = grouped[loc]
            collected_conclusion = sample_conclusion(
                [v["collected_conclusion"] for v in indicators]
            )
            samples.append(
                {
                    **meta[loc],
                    "conclusion": self._sample_conclusion(indicators),
                    "collected_conclusion": collected_conclusion,
                    "indicators": indicators,
                }
            )
        return samples

    def get_results(self, list_url: str) -> dict:
        """查询某入口最新已发布结果（含原始行、复核与修订号）。"""
        with self._lock, self._connect() as conn:
            run = self._load_run(conn, list_url)
            samples = self._samples_for_run(conn, run)
            counts = {
                "samples_total": len(samples),
                "recheck": sum(1 for s in samples if s["conclusion"] == "复检"),
                "within_limit": sum(
                    1 for s in samples if s["conclusion"] == "未超限"
                ),
                "need_review": sum(
                    1 for s in samples if s["conclusion"] == "待核实"
                ),
            }
            return {
                "list_url": run["list_url"],
                "run_id": run["id"],
                "published_at": run["published_at"],
                "incomplete": bool(run["incomplete"]),
                "failed_urls": json.loads(run["failed_urls"]),
                "list_pages_visited": json.loads(run["list_pages"]),
                "detail_pages_visited": json.loads(run["detail_pages"]),
                "limits": json.loads(run["limits_json"]),
                "samples": samples,
                "counts": counts,
            }

    def get_reviews(self, list_url: str) -> dict:
        """查询某入口当前结果的全部复核记录（含已被新原始行淘汰的旧记录）。"""
        with self._lock, self._connect() as conn:
            run = self._load_run(conn, list_url)
            rows = conn.execute(
                "SELECT * FROM reviews WHERE run_id = ? ORDER BY id",
                (run["id"],),
            ).fetchall()
            return {
                "list_url": run["list_url"],
                "run_id": run["id"],
                "reviews": [
                    {
                        "detail_url": r["detail_url"],
                        "sample_code": r["sample_code"],
                        "indicator": r["indicator_name"],
                        "raw_value": r["raw_value"],
                        "base_revision": r["base_revision"],
                        "new_revision": r["new_revision"],
                        "approved_value": r["approved_value"],
                        "approved_unit": r["approved_unit"],
                        "reason": r["reason"],
                        "reviewer": r["reviewer"],
                        "created_at": r["created_at"],
                    }
                    for r in rows
                ],
            }

    # ---------------------------------------------------------------- 复核
    def apply_reviews(self, list_url: str, items: List[ReviewItem]) -> List[ReviewOutcome]:
        """原子地应用一批复核；任一行定位失败或修订号过期则整批回滚。"""
        for item in items:
            validate_review_unit(item.unit)

        now = _now()
        with self._lock, self._connect() as conn:
            run = self._load_run(conn, list_url)

            located: List[Tuple[ReviewItem, sqlite3.Row]] = []
            conflicts: List[dict] = []
            for item in items:
                durl_key = canonical_url(item.detail_url)
                ikey = indicator_key(item.indicator)
                row = conn.execute(
                    """SELECT * FROM indicator_rows
                       WHERE run_id = ? AND detail_url_key = ?
                         AND sample_code = ? AND indicator_key = ?
                         AND active = 1""",
                    (run["id"], durl_key, item.sample_code.strip(), ikey),
                ).fetchone()
                if row is None:
                    raise NotFound(
                        "未找到指标行: "
                        f"{item.detail_url} / {item.sample_code} / {item.indicator}"
                    )
                if row["revision"] != item.revision:
                    conflicts.append(
                        {
                            "detail_url": row["detail_url"],
                            "sample_code": row["sample_code"],
                            "indicator": row["indicator_name"],
                            "expected_revision": item.revision,
                            "current_revision": row["revision"],
                        }
                    )
                located.append((item, row))
            if conflicts:
                raise RevisionConflict(conflicts)

            applied: List[ReviewOutcome] = []
            for item, row in located:
                approved_unit = validate_review_unit(item.unit)
                new_revision = row["revision"] + 1
                reviewer = item.reviewer
                cur = conn.execute(
                    """INSERT INTO reviews (run_id, row_id, detail_url,
                           sample_code, indicator_name, raw_value,
                           base_revision, new_revision, approved_value,
                           approved_unit, reason, reviewer, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run["id"],
                        row["id"],
                        row["detail_url"],
                        row["sample_code"],
                        row["indicator_name"],
                        row["raw_value"],
                        row["revision"],
                        new_revision,
                        item.approved_value,
                        approved_unit,
                        item.reason,
                        reviewer,
                        now,
                    ),
                )
                review_id = int(cur.lastrowid)
                updated = conn.execute(
                    """UPDATE indicator_rows SET revision = ?, review_id = ?,
                           reviewed_value = ?, reviewed_unit = ?,
                           review_reason = ?, reviewer = ?, reviewed_at = ?,
                           updated_at = ? WHERE id = ? AND revision = ?""",
                    (
                        new_revision,
                        review_id,
                        item.approved_value,
                        approved_unit,
                        item.reason,
                        reviewer,
                        now,
                        now,
                        row["id"],
                        row["revision"],
                    ),
                )
                if updated.rowcount != 1:
                    # 并发写入导致修订号变化：整批回滚并报冲突
                    current = conn.execute(
                        "SELECT revision FROM indicator_rows WHERE id = ?",
                        (row["id"],),
                    ).fetchone()
                    raise RevisionConflict(
                        [
                            {
                                "detail_url": row["detail_url"],
                                "sample_code": row["sample_code"],
                                "indicator": row["indicator_name"],
                                "expected_revision": item.revision,
                                "current_revision": (
                                    current["revision"] if current else None
                                ),
                            }
                        ]
                    )
                applied.append(
                    ReviewOutcome(
                        list_url=run["list_url"],
                        run_id=int(run["id"]),
                        detail_url=row["detail_url"],
                        sample_code=row["sample_code"],
                        indicator=row["indicator_name"],
                        revision=new_revision,
                        approved_value=item.approved_value,
                        approved_unit=approved_unit,
                        reason=item.reason,
                        reviewer=reviewer,
                        created_at=now,
                    )
                )

            # 复核记录与水样结论同事务生效；返回受影响水样的最新结论
            samples = self._samples_for_run(conn, run)
            by_key = {
                (s["detail_url"], s["sample_code"]): s for s in samples
            }
            for outcome in applied:
                sample = by_key.get((outcome.detail_url, outcome.sample_code))
                if sample is not None:
                    outcome.sample = sample
                    for view in sample["indicators"]:
                        if indicator_key(view["name"]) == indicator_key(
                            outcome.indicator
                        ):
                            outcome.indicator_view = view
                            break

            return applied


_storage: Optional[Storage] = None
_storage_lock = threading.Lock()


def get_storage() -> Storage:
    """进程级 Storage 单例（首次调用时创建）；测试可用 reset_storage 重建。"""
    global _storage
    if _storage is None:
        with _storage_lock:
            if _storage is None:
                _storage = Storage()
    return _storage


def reset_storage() -> None:
    """丢弃单例，下次调用按当前环境变量重新打开数据库（测试用）。"""
    global _storage
    with _storage_lock:
        _storage = None

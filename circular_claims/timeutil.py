"""时间解析与 as-of 过滤。

业务发生时间不等于资料获知时间：known_at/reported_at 控制资料何时可得。
所有分析与审阅都支持 as_of 时点，时点之后才获知的记录不可见。
"""
from __future__ import annotations

import datetime as dt

TZ = dt.timezone(dt.timedelta(hours=8))  # 资料时间戳均为 +08:00


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def parse_ts(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"时间戳缺少时区偏移: {value}")
    return parsed.astimezone(TZ)


def latest_known_at(rows_by_table: dict[str, list[dict]]) -> dt.datetime:
    """取所有记录 known_at 的最大值，作为默认 as-of 时点。"""
    candidates: list[dt.datetime] = []
    for table, rows in rows_by_table.items():
        col = "known_at"
        for row in rows:
            if col in row and row[col]:
                candidates.append(parse_ts(row[col]))
    if not candidates:
        raise ValueError("没有任何带 known_at 的记录，无法确定 as-of 时点")
    return max(candidates)


def visible(row: dict, as_of: dt.datetime) -> bool:
    """记录在 as_of 时点是否已经获知。"""
    known = row.get("known_at")
    if not known:
        return True
    return parse_ts(known) <= as_of


def months_between(start: dt.date, end: dt.date) -> float:
    """按 30.4375 天/月折算两个日期间的月数。"""
    return (end - start).days / 30.4375


def years_between(start: dt.date, end: dt.date) -> float:
    return (end - start).days / 365.25

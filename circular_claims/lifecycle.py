"""设备生命周期：把流转事实重建为二次使用服务片段。

核心口径：
- 功能服务单位是"约定用途下的有效使用年"，2000 服务小时 = 1 服务年；
  所有权转移（销售）本身不产生任何服务小时。
- 一次售出不证明完整使用寿命已实现：只有 in_use 记录的 service_hours
  才是有记录支持的已观测服务。
- 退回 / 提前报废关闭服务片段，已支持服务随之缩短；退回后没有未来情景。
- 最终拆解关闭片段并进入处置环节；本数据中拆解为可扩展事件。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .contract import BurdenKind, MovementKind
from .timeutil import parse_date, parse_ts

SERVICE_HOURS_PER_YEAR = 2000.0


@dataclass
class ServiceEpisode:
    """一段二次使用服务：从售出（服务开始）到退回/拆解或仍在进行。"""

    device_id: str
    opened_on: dt.date                      # 售出日：服务权利起算日
    closed_on: dt.date | None = None        # 退回/拆解日；None 表示仍在进行
    close_reason: str | None = None         # returned | dismantled | None
    observed_hours: int = 0                 # 有记录支持的累计使用小时
    observed_through: dt.date | None = None # 最后一次在用观测日
    movement_ids: list[str] = field(default_factory=list)

    @property
    def observed_service_years(self) -> float:
        return self.observed_hours / SERVICE_HOURS_PER_YEAR

    @property
    def is_active(self) -> bool:
        return self.closed_on is None


@dataclass
class DeviceTimeline:
    device_id: str
    kind: str
    region: str
    serial: str | None
    movements: list[dict]
    episodes: list[ServiceEpisode]
    dismantled_on: dt.date | None

    def realized_service_years(self) -> float:
        return sum(e.observed_service_years for e in self.episodes)

    def has_return(self) -> bool:
        return any(e.close_reason == "returned" for e in self.episodes)


def build_timeline(
    device: dict,
    movements: list[dict],
    as_of: dt.datetime,
) -> DeviceTimeline:
    """按获知时间过滤后，重建单台规范设备的时间线与服务片段。"""
    visible_movs = sorted(
        (m for m in movements if parse_ts(m["known_at"]) <= as_of),
        key=lambda m: (m["day"], m["id"]),
    )
    episodes: list[ServiceEpisode] = []
    current: ServiceEpisode | None = None
    dismantled_on: dt.date | None = None
    previous_day: dt.date | None = None

    for mov in visible_movs:
        kind = MovementKind(mov["kind"])
        day = parse_date(mov["day"])
        if previous_day and day < previous_day:
            raise ValueError(
                f"设备 {device['id']} 流转日期乱序：{mov['id']} {day} < {previous_day}"
            )
        previous_day = day

        if kind == MovementKind.SOLD:
            if current is not None:
                raise ValueError(
                    f"设备 {device['id']} 在未关闭服务片段时再次售出：{mov['id']}"
                )
            current = ServiceEpisode(
                device_id=device["id"], opened_on=day, movement_ids=[mov["id"]]
            )
        elif kind == MovementKind.IN_USE:
            if current is None:
                raise ValueError(
                    f"设备 {device['id']} 的在用记录 {mov['id']} 缺少售出起点"
                )
            current.observed_hours += mov["service_hours"]
            current.observed_through = (
                day
                if current.observed_through is None
                else max(current.observed_through, day)
            )
            current.movement_ids.append(mov["id"])
        elif kind in (MovementKind.RETURNED, MovementKind.DISMANTLED):
            if current is None:
                raise ValueError(
                    f"设备 {device['id']} 的 {kind.value} 记录 {mov['id']} "
                    "没有进行中的服务片段"
                )
            current.closed_on = day
            current.close_reason = kind.value
            current.movement_ids.append(mov["id"])
            episodes.append(current)
            if kind == MovementKind.DISMANTLED:
                dismantled_on = day
            current = None
        else:
            # received / refurbished 不改变服务片段，仅保留在时间线中
            if current is not None:
                current.movement_ids.append(mov["id"])

    if current is not None:
        episodes.append(current)  # 仍在进行的片段

    return DeviceTimeline(
        device_id=device["id"],
        kind=device["kind"],
        region=device["region"],
        serial=device.get("serial"),
        movements=visible_movs,
        episodes=episodes,
        dismantled_on=dismantled_on,
    )


def build_all(
    devices: list[dict],
    movements: list[dict],
    as_of: dt.datetime,
    canonical_map: dict[str, str],
) -> dict[str, DeviceTimeline]:
    """按规范设备折叠流转后构建全部时间线。"""
    by_canonical: dict[str, list[dict]] = {}
    for mov in movements:
        root = canonical_map[mov["device"]]
        by_canonical.setdefault(root, []).append(mov)
    timelines: dict[str, DeviceTimeline] = {}
    for device in devices:
        root = canonical_map[device["id"]]
        if root != device["id"]:
            continue  # 被合并设备只通过根设备呈现
        timelines[root] = build_timeline(
            device, by_canonical.get(root, []), as_of
        )
    return timelines


def visible_burdens(
    burdens: list[dict], as_of: dt.datetime
) -> dict[str, list[dict]]:
    """按设备归组已发生且已获知的负担；null 不等同于零，未知负担不臆造。"""
    grouped: dict[str, list[dict]] = {}
    for row in burdens:
        if parse_ts(row["known_at"]) <= as_of:
            grouped.setdefault(row["device"], []).append(row)
    return grouped


def burden_totals(rows: list[dict]) -> dict[str, tuple[float, float]]:
    """按负担类别汇总 (low, high)；使用阶段负担单独列示。"""
    totals: dict[str, list[float]] = {}
    for kind in BurdenKind:
        totals[kind.value] = [0.0, 0.0]
    for row in rows:
        totals[row["kind"]][0] += row["kgco2e_low"]
        totals[row["kind"]][1] += row["kgco2e_high"]
    return {k: (v[0], v[1]) for k, v in totals.items()}

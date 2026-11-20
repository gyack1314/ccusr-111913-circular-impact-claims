"""设备身份解析：序列号事实与人工确认对应严格分离。

规则（对应资料契约与任务要求）：
- serial 为空表示无法确认实物，任何情况下都不能按型号/外观猜测合并。
- 只有人工确认的 confirmed_same_as（或审阅时登记的 identity_link）才能合并身份。
- 两台都有序列号且序列号不同，属于不同实物，拒绝合并。
- 对应关系可能成链，统一解析到规范设备（canonical）；检测环。
- 流转、负担、申请保留原始设备 id，分析时按 canonical 折叠。
"""
from __future__ import annotations

import datetime as dt
import uuid


class IdentityError(ValueError):
    pass


class IdentityResolver:
    """依据 devices 行与 identity_links 账本构建规范身份映射。"""

    def __init__(self, devices: list[dict], links: list[dict] | None = None):
        self.devices = {d["id"]: d for d in devices}
        # 直接边：merged_id -> canonical_kept
        self.edges: dict[str, str] = {}
        for d in devices:
            if d.get("confirmed_same_as"):
                self.edges[d["id"]] = d["confirmed_same_as"]
        for link in links or []:
            self.edges[link["merged_id"]] = link["canonical_kept"]
        self._validate_serials()
        self._map = self._build_map()

    def _validate_serials(self) -> None:
        for merged_id, kept_id in self.edges.items():
            if merged_id not in self.devices or kept_id not in self.devices:
                raise IdentityError(
                    f"身份对应 {merged_id} -> {kept_id} 引用了不存在的设备"
                )
            a, b = self.devices[merged_id], self.devices[kept_id]
            # 两个不同的非空序列号 = 两台不同实物，不能合并
            if a.get("serial") and b.get("serial") and a["serial"] != b["serial"]:
                raise IdentityError(
                    f"设备 {merged_id}({a['serial']}) 与 {kept_id}({b['serial']}) "
                    "序列号不同，属于不同实物，禁止确认同一"
                )

    def _build_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for device_id in self.devices:
            seen: set[str] = set()
            current = device_id
            while current in self.edges:
                if current in seen:
                    raise IdentityError(f"身份对应存在环：{sorted(seen)}")
                seen.add(current)
                current = self.edges[current]
                if current not in self.devices:
                    raise IdentityError(
                        f"身份对应链指向不存在的设备 {current}"
                    )
            mapping[device_id] = current
        return mapping

    def canonical(self, device_id: str) -> str:
        if device_id not in self._map:
            raise IdentityError(f"未知设备 {device_id}")
        return self._map[device_id]

    def members(self, canonical_id: str) -> list[str]:
        """规范设备下的全部成员（含根设备本身）。"""
        return sorted(k for k, v in self._map.items() if v == canonical_id)

    def merged_only(self, canonical_id: str) -> list[str]:
        """仅被合并进来的成员（不含根设备）。"""
        return sorted(
            k for k, v in self._map.items() if v == canonical_id and k != canonical_id
        )

    def canonical_devices(self) -> dict[str, dict]:
        """规范设备视图：合并后属性以根设备为准，记录合并来源。"""
        view: dict[str, dict] = {}
        for device_id, root in self._map.items():
            if root not in view:
                base = dict(self.devices[root])
                base["canonical_id"] = root
                base["merged_members"] = []
                view[root] = base
            if device_id != root:
                view[root]["merged_members"].append(device_id)
        return view

    def duplicated_serials(self) -> dict[str, list[str]]:
        """相同序列号但缺少人工确认的设备：只提示，绝不自动合并。"""
        groups: dict[str, list[str]] = {}
        for d in self.devices.values():
            if d.get("serial"):
                groups.setdefault(d["serial"], []).append(d["id"])
        return {
            serial: ids
            for serial, ids in groups.items()
            if len(ids) > 1
            and len({self.canonical(i) for i in ids}) > 1
        }

    def null_serial_unlinked(self) -> list[str]:
        """空序列号且无确认对应的设备：保持独立身份。"""
        return [
            d["id"]
            for d in self.devices.values()
            if not d.get("serial") and self.canonical(d["id"]) == d["id"]
        ]


def propose_link(store, kept_id: str, merged_id: str, reason: str, evidence: str) -> dict:
    """登记一条人工确认对应；执行全部保护校验后写入追加式账本。

    合并会改变后续分析口径，因此同时写一条 events，供声明影响复核使用。
    """
    devices = store.all_tables_ignoring_asof().get("devices", [])
    links = store.read("identity_links")
    resolver = IdentityResolver(devices, links)
    if kept_id not in resolver.devices:
        raise IdentityError(f"保留身份设备不存在：{kept_id}")
    if merged_id not in resolver.devices:
        raise IdentityError(f"被合并设备不存在：{merged_id}")
    if kept_id == merged_id:
        raise IdentityError("设备不能与自身确认同一")
    if not evidence or not reason:
        raise IdentityError("人工确认对应必须给出 reason 与 evidence")
    # 模拟加入新边后再构建一次，提前拦截环与序列号冲突
    trial_links = links + [
        {
            "id": "trial",
            "canonical_kept": kept_id,
            "merged_id": merged_id,
            "reason": reason,
            "evidence": evidence,
        }
    ]
    IdentityResolver(devices, trial_links)  # 抛错即拒绝
    now = dt.datetime.now(dt.timezone.utc).astimezone().isoformat()
    link_id = "L" + uuid.uuid4().hex[:10]
    link = {
        "id": link_id,
        "canonical_kept": kept_id,
        "merged_id": merged_id,
        "reason": reason,
        "evidence": evidence,
        "recorded_at": now,
        "batch_id": None,
    }
    store.append("identity_links", [link])
    event = {
        "id": "E" + uuid.uuid4().hex[:10],
        "kind": "identity_merge",
        "device": merged_id,
        "canonical_kept": kept_id,
        "occurred_at": now,
        "detail": {"link_id": link_id, "reason": reason, "evidence": evidence},
    }
    store.append("events", [event])
    return link

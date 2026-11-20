"""本地状态存储：.local/ 下的追加式 JSONL 账本。

设计原则：
- data/ 原始资料只读，永不改写；导入只做登记（批次清单 + 行复制进账本）。
- 所有账本只追加、不修改；声明更正通过新记录指向旧声明实现，原件保留。
- 不使用数据库服务，全部为公开格式（JSON Lines / JSON），可直接审阅。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import uuid

from . import contract

LEDGER_FILES = (
    "batches",          # 导入批次登记
    "records",          # 全部原始行（带批次与获知时间）
    "identity_links",   # 人工确认的同一实物对应
    "scenarios",        # 已登记情景定义
    "analyses",         # 分析运行摘要
    "applications",     # 申请审阅决定
    "claims",           # 已核发声明（不可变）
    "corrections",      # 更正/撤回记录（指向被影响声明）
    "events",           # 触发复核的业务事件（退回、因子修订、身份合并）
)


class Store:
    """封装 .local 目录下全部账本读写。"""

    def __init__(self, root: str | pathlib.Path = ".local"):
        self.root = pathlib.Path(root)
        self.state_dir = self.root / "state"
        self.output_dir = self.root / "outputs"
        for directory in (self.state_dir, self.output_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # ---- 基础读写 -------------------------------------------------------
    def path(self, ledger: str) -> pathlib.Path:
        if ledger not in LEDGER_FILES:
            raise KeyError(f"未知账本 {ledger}")
        return self.state_dir / f"{ledger}.jsonl"

    def read(self, ledger: str) -> list[dict]:
        path = self.path(ledger)
        if not path.exists():
            return []
        return contract.read_jsonl(path)

    def append(self, ledger: str, rows: list[dict]) -> None:
        if not rows:
            return
            # 身份唯一性在账本层面保护
        existing = {r["id"] for r in self.read(ledger)}
        for row in rows:
            if row["id"] in existing:
                raise ValueError(f"{ledger} 已存在身份 {row['id']}，拒绝重复写入")
        contract.append_jsonl(self.path(ledger), rows)

    def find(self, ledger: str, row_id: str) -> dict | None:
        for row in self.read(ledger):
            if row["id"] == row_id:
                return row
        return None

    # ---- 批次导入 -------------------------------------------------------
    def import_batch(self, source_dir: str | pathlib.Path) -> dict:
        manifest, tables = contract.load_batch(source_dir)
        source_dir = pathlib.Path(source_dir).resolve()
        batch_id = "B" + uuid.uuid4().hex[:10]
        now = dt.datetime.now(dt.timezone.utc).astimezone().isoformat()
        batch_record = {
            "id": batch_id,
            "source_dir": str(source_dir),
            "manifest": manifest,
            "imported_at": now,
        }
        # 同一目录重复导入直接拒绝，避免重复累计
        for prior in self.read("batches"):
            if prior["source_dir"] == str(source_dir):
                raise ValueError(
                    f"资料目录 {source_dir} 已登记为批次 {prior['id']}，"
                    "新资料请放入独立目录"
                )
        ledger_rows: list[dict] = []
        existing_records = {(r["table"], r["row_id"]) for r in self.read("records")}
        for table, rows in tables.items():
            for row in rows:
                key = (table, row["id"])
                if key in existing_records:
                    raise ValueError(
                        f"{table}/{row['id']} 已在账本中；修订资料必须使用新身份"
                    )
                ledger_rows.append(
                    {
                        "id": f"{batch_id}:{table}:{row['id']}",
                        "batch_id": batch_id,
                        "table": table,
                        "row_id": row["id"],
                        "data": row,
                    }
                )
        # 在账本完整设备集合上复核跨批次引用完整性
        known_devices = {
            r["row_id"]
            for r in self.read("records")
            if r["table"] == "devices"
        } | {r["row_id"] for r in ledger_rows if r["table"] == "devices"}
        for record in ledger_rows:
            if record["table"] in ("movements", "burdens", "applications"):
                if record["data"]["device"] not in known_devices:
                    raise contract.ContractError(
                        record["table"],
                        record["row_id"],
                        f"引用了账本中不存在的设备 {record['data']['device']}",
                    )
        # confirmed_same_as 同样在完整集合上复核
        for record in ledger_rows:
            if record["table"] == "devices" and record["data"].get("confirmed_same_as"):
                target = record["data"]["confirmed_same_as"]
                if target not in known_devices:
                    raise contract.ContractError(
                        "devices", record["row_id"],
                        f"confirmed_same_as 指向账本中不存在的设备 {target}",
                    )
        self.append("batches", [batch_record])
        self.append("records", ledger_rows)
        # devices 中自带的 confirmed_same_as 自动登记为身份对应（仍是人工确认来源）
        links = []
        merge_events = []
        for row in tables.get("devices", []):
            if row.get("confirmed_same_as"):
                link_id = "L" + uuid.uuid4().hex[:10]
                links.append(
                    {
                        "id": link_id,
                        "canonical_kept": row["confirmed_same_as"],
                        "merged_id": row["id"],
                        "reason": "import_confirmed_same_as",
                        "evidence": f"devices/{row['id']}.confirmed_same_as",
                        "recorded_at": now,
                        "batch_id": batch_id,
                    }
                )
                merge_events.append(
                    {
                        "id": "E" + uuid.uuid4().hex[:10],
                        "kind": "identity_merge",
                        "device": row["id"],
                        "canonical_kept": row["confirmed_same_as"],
                        "occurred_at": now,
                        "detail": {
                            "link_id": link_id,
                            "reason": "import_confirmed_same_as",
                            "evidence": f"devices/{row['id']}.confirmed_same_as",
                        },
                    }
                )
        if links:
            self.append("identity_links", links)
        if merge_events:
            self.append("events", merge_events)
        return batch_record

    # ---- 实体视图 -------------------------------------------------------
    def tables(self, as_of: dt.datetime | None = None) -> dict[str, list[dict]]:
        """按表汇总已导入行；可按 as-of 过滤获知时间。"""
        from .timeutil import visible

        out: dict[str, list[dict]] = {name: [] for name in contract.TABLE_NAMES}
        for record in self.read("records"):
            row = record["data"]
            if as_of is not None and not visible(row, as_of):
                continue
            out[record["table"]].append(row)
        return {k: v for k, v in out.items() if v}

    def all_tables_ignoring_asof(self) -> dict[str, list[dict]]:
        return self.tables(as_of=None)

    def write_output(self, name: str, payload: dict | list) -> pathlib.Path:
        path = self.output_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return path

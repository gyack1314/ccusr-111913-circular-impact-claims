"""资料契约：枚举、JSONL 读写、清单摘要与业务字段校验。

本模块只负责"资料是否符合契约"，不做任何领域计算。
所有原始资料为本地合成演练数据，null 表示未知/不适用，绝不自动等同于零。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass
from enum import Enum

# 契约固定的字段集合（与 docs/input-contract.md 一致）
FIELDS: dict[str, frozenset[str]] = {
    "devices": frozenset(
        {"id", "serial", "kind", "region", "model_age_years", "confirmed_same_as"}
    ),
    "movements": frozenset(
        {"id", "device", "kind", "day", "known_at", "service_hours", "holder"}
    ),
    "factors": frozenset(
        {
            "id", "kind", "region", "unit", "low", "high",
            "correlation_group", "valid_from", "valid_to", "known_at",
        }
    ),
    "substitution": frozenset(
        {
            "id", "kind", "region", "would_buy_new",
            "observed_service_months", "observation_complete", "known_at", "fold",
        }
    ),
    "burdens": frozenset(
        {
            "id", "device", "kind", "kgco2e_low", "kgco2e_high",
            "occurred_at", "known_at",
        }
    ),
    "applications": frozenset(
        {
            "id", "applicant", "device", "service_from",
            "service_to", "mode", "known_at",
        }
    ),
}

TABLE_NAMES = tuple(FIELDS)


class MovementKind(str, Enum):
    RECEIVED = "received"          # 接收
    REFURBISHED = "refurbished"    # 翻新
    SOLD = "sold"                  # 销售（一次售出不证明完整寿命已实现）
    IN_USE = "in_use"              # 在用（带记录支持的使用小时）
    RETURNED = "returned"          # 退回（缩短已支持的使用服务）
    DISMANTLED = "dismantled"      # 最终拆解


class BurdenKind(str, Enum):
    REPAIR_TRANSPORT = "repair_transport"  # 维修与额外运输（已发生负担）
    USE = "use"                            # 使用阶段负担（如电费对应的排放）
    DISMANTLING = "dismantling"            # 拆解处置负担


class ClaimMode(str, Enum):
    EXCLUSIVE = "exclusive"  # 独占认领：同一设备服务区间互斥
    JOINT = "joint"          # 共同披露：可多方引用，汇总只计一次


class Fold(str, Enum):
    DEVELOPMENT = "development"  # 参与估计的折
    HOLDOUT = "holdout"          # 留出折：不参与估计，仅做支持程度检查


# 功能服务单位（资料契约）：2000 服务小时 = 一个服务年
SERVICE_HOURS_PER_YEAR = 2000.0


class ContractError(ValueError):
    """资料违反契约时抛出；携带表名与记录身份以便定位。"""

    def __init__(self, table: str, row_id: str | None, message: str):
        self.table = table
        self.row_id = row_id
        prefix = f"[{table}" + (f"/{row_id}" if row_id is not None else "") + "] "
        super().__init__(prefix + message)


def read_jsonl(path: pathlib.Path) -> list[dict]:
    """逐行读取 JSON Lines；空行跳过。"""
    rows: list[dict] = []
    with pathlib.Path(path).open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ContractError(
                    path.stem, None, f"第 {line_no} 行不是合法 JSON: {exc}"
                ) from exc
    return rows


def write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    """原子式追加写入 JSON Lines（同目录临时文件再替换，供整批导入使用）。"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def append_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    """向追加式账本写入记录。"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Batch:
    """一次导入批次的登记信息。"""

    batch_id: str
    source_dir: str
    manifest_path: str
    imported_at: str
    files: tuple[dict, ...]


def _require_fields(table: str, rows: list[dict]) -> None:
    expected = FIELDS[table]
    seen: set[str] = set()
    for row in rows:
        rid = row.get("id")
        if not isinstance(rid, str) or not rid:
            raise ContractError(table, None, "缺少字符串身份 id")
        actual = frozenset(row)
        if actual != expected:
            missing = expected - actual
            extra = actual - expected
            detail = []
            if missing:
                detail.append(f"缺字段 {sorted(missing)}")
            if extra:
                detail.append(f"多字段 {sorted(extra)}")
            raise ContractError(table, rid, "字段集合不匹配：" + "；".join(detail))
        if rid in seen:
            raise ContractError(table, rid, "身份重复")
        seen.add(rid)


def _validate_row(table: str, row: dict) -> None:
    rid = row["id"]
    if table == "devices":
        if not isinstance(row["kind"], str) or not row["kind"]:
            raise ContractError(table, rid, "kind 必须为非空字符串")
        if not isinstance(row["region"], str) or not row["region"]:
            raise ContractError(table, rid, "region 必须为非空字符串")
        if row["serial"] is not None and not isinstance(row["serial"], str):
            raise ContractError(table, rid, "serial 只能为字符串或 null")
        if row["confirmed_same_as"] is not None and not isinstance(
            row["confirmed_same_as"], str
        ):
            raise ContractError(table, rid, "confirmed_same_as 只能为字符串或 null")
        if not isinstance(row["model_age_years"], (int, float)) or isinstance(
            row["model_age_years"], bool
        ) or row["model_age_years"] < 0:
            raise ContractError(table, rid, "model_age_years 必须为非负数值")
    elif table == "movements":
        try:
            MovementKind(row["kind"])
        except ValueError as exc:
            raise ContractError(
                table, rid, f"未知流转类型 {row['kind']!r}"
            ) from exc
        if not isinstance(row["service_hours"], int) or row["service_hours"] < 0:
            raise ContractError(table, rid, "service_hours 必须为非负整数")
        _date(table, rid, row["day"], "day")
        _ts(table, rid, row["known_at"], "known_at")
    elif table == "factors":
        if row["unit"] != "kgco2e_per_service_year":
            raise ContractError(
                table, rid, f"本流程只支持服务年制造因子单位，收到 {row['unit']!r}"
            )
        for col in ("low", "high"):
            if not isinstance(row[col], (int, float)) or isinstance(
                row[col], bool
            ) or row[col] < 0:
                raise ContractError(table, rid, f"{col} 必须为非负数值")
        if row["low"] > row["high"]:
            raise ContractError(table, rid, "low 不得大于 high")
        _date(table, rid, row["valid_from"], "valid_from")
        _date(table, rid, row["valid_to"], "valid_to")
        if row["valid_from"] >= row["valid_to"]:
            raise ContractError(table, rid, "valid_from 必须早于 valid_to")
        _ts(table, rid, row["known_at"], "known_at")
    elif table == "substitution":
        if row["would_buy_new"] not in (True, False, None):
            raise ContractError(table, rid, "would_buy_new 只能为 true/false/null")
        if (
            not isinstance(row["observed_service_months"], int)
            or row["observed_service_months"] <= 0
        ):
            raise ContractError(table, rid, "observed_service_months 必须为正整数")
        if not isinstance(row["observation_complete"], bool):
            raise ContractError(table, rid, "observation_complete 必须为布尔")
        try:
            Fold(row["fold"])
        except ValueError as exc:
            raise ContractError(table, rid, f"未知 fold {row['fold']!r}") from exc
        _ts(table, rid, row["known_at"], "known_at")
    elif table == "burdens":
        try:
            BurdenKind(row["kind"])
        except ValueError as exc:
            raise ContractError(
                table, rid, f"未知负担类型 {row['kind']!r}"
            ) from exc
        for col in ("kgco2e_low", "kgco2e_high"):
            if not isinstance(row[col], (int, float)) or isinstance(
                row[col], bool
            ) or row[col] < 0:
                raise ContractError(table, rid, f"{col} 必须为非负数值")
        if row["kgco2e_low"] > row["kgco2e_high"]:
            raise ContractError(table, rid, "kgco2e_low 不得大于 kgco2e_high")
        _ts(table, rid, row["occurred_at"], "occurred_at")
        _ts(table, rid, row["known_at"], "known_at")
    elif table == "applications":
        try:
            ClaimMode(row["mode"])
        except ValueError as exc:
            raise ContractError(table, rid, f"未知申请模式 {row['mode']!r}") from exc
        _date(table, rid, row["service_from"], "service_from")
        _date(table, rid, row["service_to"], "service_to")
        if row["service_from"] >= row["service_to"]:
            raise ContractError(table, rid, "service_from 必须早于 service_to")
        _ts(table, rid, row["known_at"], "known_at")


def _date(table: str, rid: str, value: str, col: str) -> None:
    try:
        import datetime as _dt

        _dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(table, rid, f"{col} 必须为 YYYY-MM-DD 日期") from exc


def _ts(table: str, rid: str, value: str, col: str) -> None:
    import datetime as _dt

    try:
        _dt.datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(table, rid, f"{col} 必须为带偏移的 ISO 时间戳") from exc


def validate_tables(
    tables: dict[str, list[dict]],
    known_device_ids: set[str] | None = None,
    check_refs: bool = True,
) -> None:
    """对资料执行契约校验（字段、身份、枚举、取值、引用）。

    - check_refs=False：只做表内契约校验，跨表/跨批次引用由存储层复核；
    - known_device_ids：并入设备集合后再做引用完整性校验。
    """
    unknown = set(tables) - set(FIELDS)
    if unknown:
        raise ContractError(",".join(sorted(unknown)), None, "未知资料表")
    for table, rows in tables.items():
        _require_fields(table, rows)
        for row in rows:
            _validate_row(table, row)
    device_ids = {r["id"] for r in tables.get("devices", [])}
    if known_device_ids:
        device_ids |= known_device_ids
    if check_refs:
        for table in ("movements", "burdens", "applications"):
            for row in tables.get(table, []):
                if row["device"] not in device_ids:
                    raise ContractError(
                        table, row["id"], f"引用了不存在的设备 {row['device']}"
                    )
    # 序列号只能在"没有人工确认对应"的情况下用于提示，重复序列号本身不禁止；
    # confirmed_same_as 的目标存在性在完整资料校验时检查，部分批次留给存储层。
    for row in tables.get("devices", []):
        target = row["confirmed_same_as"]
        if target is not None and target == row["id"]:
            raise ContractError("devices", row["id"], "设备不能确认与自身同一")
        if check_refs and target is not None and target not in device_ids:
            raise ContractError(
                "devices", row["id"], f"confirmed_same_as 指向不存在设备 {target}"
            )


def validate_full_dataset(tables: dict[str, list[dict]]) -> None:
    """对完整资料目录（六表齐全）执行含引用完整性的整组校验。"""
    validate_tables(tables, known_device_ids=None)


def load_manifest(manifest_path: pathlib.Path) -> dict:
    return json.loads(pathlib.Path(manifest_path).read_text(encoding="utf-8"))


def load_batch(source_dir: pathlib.Path) -> tuple[dict, dict[str, list[dict]]]:
    """按清单读取一个资料目录，校验摘要与契约，返回 (manifest, tables)。

    清单缺失时现场生成内存清单（不写回源目录），便于导入新资料。
    """
    source_dir = pathlib.Path(source_dir)
    manifest_path = source_dir / "manifest.json"
    if manifest_path.exists():
        manifest = load_manifest(manifest_path)
        tables: dict[str, list[dict]] = {}
        for entry in manifest["tables"]:
            listed = pathlib.PurePosixPath(entry["path"])
            # 清单路径相对项目根（如 data/devices.jsonl），导入时相对源目录解析
            candidates = [
                source_dir / listed,
                source_dir / listed.name,
                source_dir.joinpath(*listed.parts[1:]) if len(listed.parts) > 1 else None,
            ]
            path = next((p for p in candidates if p and p.exists()), None)
            if path is None:
                raise ContractError(
                    entry["name"], None, f"清单列出的文件不存在：{entry['path']}"
                )
            if sha256_file(path) != entry["sha256"]:
                raise ContractError(
                    entry["name"], None, "资料文件 sha256 摘要不匹配，拒绝导入"
                )
            rows = read_jsonl(path)
            if len(rows) != entry["rows"]:
                raise ContractError(entry["name"], None, "记录数与清单不一致")
            tables[entry["name"]] = rows
    else:
        tables = {}
        entries = []
        for table in TABLE_NAMES:
            path = source_dir / f"{table}.jsonl"
            if not path.exists():
                continue
            rows = read_jsonl(path)
            tables[table] = rows
            entries.append(
                {
                    "name": table,
                    "key": "id",
                    "path": f"{table}.jsonl",
                    "rows": len(rows),
                    "sha256": sha256_file(path),
                }
            )
        manifest = {
            "project": "circular-impact-claims",
            "synthetic": True,
            "tables": entries,
        }
    # 批次内只做表内契约校验（字段/枚举/取值）；跨表与跨批次引用完整性
    # 统一由存储层在账本完整设备集合上复核，避免部分批次误判。
    validate_tables(tables, check_refs=False)
    return manifest, tables

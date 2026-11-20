# 家电多次流转的替代减排证据与声明核发

本项目把回收家电的**实际流转**与**有依据的替代情景**联系起来，产出可供合作方审阅的
环境效益声明，并管理申请审阅、声明核发与后续更正。全部资料为本地合成数据与公开格式
演练因子，**不代表真实居民或企业，不办理真实碳交易或法定认证**。

回收、翻新转售、最终拆解不能分别宣称避免了一台新机的全部排放：本流程通过服务年口径、
替代率、已实现/预计分账、引用权利互斥与更正链，避免重复计算和无证据的未来收益声明。

## 环境

- Python 3.11；业务代码仅使用标准库，`requirements.txt` 中固定的科学计算依赖为环境基线。
- 运行状态只写入 `.local/`（追加式 JSONL 账本）与 `outputs/`（报告），**不改写 `data/`**。
- 无需外网、账号或令牌。容器：`docker build -t recovery-data .`。

## 一分钟演练

```bash
bash support/run_demo.sh
```

脚本顺序执行：导入基础资料 → 分析（相关蒙特卡洛 + 留出检查）→ 申请审阅核发 →
导入后续批次（因子修订/身份合并/退回/新申请）→ 事件复核与更正 → 三情景对照 → 下钻报告。

## 命令行流程（`python3 -m circular_claims ...`）

| 命令 | 作用 |
| --- | --- |
| `import-data <目录>` | 校验 sha256 摘要与资料契约，登记为追加式批次；新资料须放独立目录、用新身份 |
| `identities` | 查看规范设备、人工确认合并、空序列号与未确认重复序列号提示 |
| `merge-identity --kept --merged --reason --evidence` | 登记人工确认的同一实物对应（不同非空序列号拒绝合并） |
| `analyze --scenario {central,conservative,ambitious} [--as-of] [--trials] [--seed]` | 净效益分析：已实现/预计分账、相关 MC、holdout 留出检查 |
| `list-applications` | 列出申请及是否已决定 |
| `review [--application ID] [--issue]` | 审阅申请；加 `--issue` 同时核发（独占互斥、共同披露共存、区间裁剪） |
| `register-event --kind returned/factor_revision/... --device ...` | 登记触发复核的业务事件（退回事件会核验流转事实） |
| `recheck-event --event-id ...` | 事件后重算受影响声明，生成部分更正或撤回，原件保留 |
| `report --output-dir ...` | 产出 JSON + Markdown 下钻报告 |
| `show-claim <id>` | 查看声明原件（不可变）与更正链 |
| `reset --yes` | 清空 `.local/`（不动 `data/`） |

## 关键纪律（与方法论文档对应，详见 [docs/methodology.md](docs/methodology.md)）

1. **服务单位**：2000 服务小时 = 1 服务年；销售不等于服务，退回/提前报废截断已支持服务。
2. **三类量分账**：已发生负担、已观测服务（realized）、未来情景（projected）；
   没有证据的未来收益不计为已实现减排。
3. **身份不猜合并**：空序列号保持独立；只有人工确认对应才能折叠，不同序列号拒绝合并。
4. **因子范围**：跨地区/跨用途/超有效期不可比，越界区间显式裁剪并标注。
5. **反事实有出处**：替代率与延寿仅由 development 折估计，holdout 折只做支持检查；
   矛盾/不足单元预计项按零处理。
6. **相关性保留**：同源因子共享蒙特卡洛抽样；朴素独立束仅作"错误平均"对照。
7. **引用权利**：独占区间互斥、共同披露可共存但总计只计一次；情景锁定，禁止拼接最大项。
8. **更正链**：声明原件不可变，退回/因子修订/身份合并以更正或撤回关联，未受影响范围继续有效。
9. **下钻**：项目合计 → 设备 → 服务期间 → 实际负担/反事实/引用权利，已实现与预计可辨认。

## 资料与状态

- 原始资料契约：[docs/input-contract.md](docs/input-contract.md)；计算口径：[docs/methodology.md](docs/methodology.md)。
- 后续资料批次示例：`examples/incoming_a/`（含独立 `manifest.json` 与 sha256）。
- `.local/state/*.jsonl`：批次、原始行、身份对应、情景、分析、审阅决定、声明、更正、事件。
- `outputs/analysis_report.{json,md}`：含项目合计、单元留出检查、设备下钻与声明登记册。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

- 原有资料格式检查（`tests/test_materials.py`）继续保留。
- 新增业务测试覆盖契约/存储、身份、生命周期与引擎、反事实留出检查、
  不确定性相关性、声明权利与更正链、端到端流水线（共 40+ 用例）。
- 资料体检 `python3 -m support.inspect_data` 仍只验证文件格式，不代表业务验收。

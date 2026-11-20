#!/usr/bin/env bash
# 完整本地演练：导入 -> 分析 -> 审阅核发 -> 后续资料 -> 事件复核/更正 -> 报告。
# 仅使用本地合成资料与公开格式演练因子，不涉及真实碳交易或法定认证。
set -euo pipefail
cd "$(dirname "$0")/.."

STATE=.local
OUT=outputs
PY="python3 -m circular_claims"

echo "== 0. 清空本地状态（data/ 原始资料不动） =="
$PY reset --yes || true
rm -rf "$OUT"

echo "== 1. 导入基础资料批次（校验 sha256 摘要与契约） =="
$PY import-data data

echo "== 2. 身份视图：空序列号保持独立，禁止按型号猜测合并 =="
$PY identities

echo "== 3. 中枢情景分析（相关蒙特卡洛 + holdout 留出检查） =="
$PY analyze --scenario central --trials 2000

echo "== 4. 审阅全部申请并核发（独占互斥/共同披露共存/区间裁剪） =="
$PY review --issue

echo "== 5. 导入后续资料批次（因子修订、人工确认合并、退回、新共同披露申请） =="
$PY import-data examples/incoming_a

echo "== 6. 登记退回事件并复核受影响声明（原件保留 + 更正/撤回） =="
RET_EVENT=$($PY register-event --kind returned --device D005 \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['event_registered']['id'])")
$PY recheck-event --event-id "$RET_EVENT"

echo "== 7. 身份确认合并事件复核（指出受影响声明） =="
MERGE_EVENT=$(python3 -c "
import json
for line in open('$STATE/state/events.jsonl', encoding='utf-8'):
    row = json.loads(line)
    if row['kind'] == 'identity_merge':
        print(row['id'])
")
$PY recheck-event --event-id "$MERGE_EVENT"

echo "== 8. 因子修订后的新分析（as-of 自动推进，旧声明不重算） =="
$PY analyze --scenario central --trials 2000

echo "== 8b. 修订生效后复核 aircon/R1 声明（已实现留历史因子，预计切修订因子） =="
$PY register-event --kind factor_revision --device D001 \
  --occurred-at 2027-01-02T09:00:00+08:00 --detail '{"factor_id":"Faircon-1-r2"}' >/dev/null
FACTOR_EV=$(python3 -c "
import json
for line in open('$STATE/state/events.jsonl', encoding='utf-8'):
    row = json.loads(line)
    if row['kind'] == 'factor_revision':
        print(row['id'])
")
$PY recheck-event --event-id "$FACTOR_EV" --as-of 2027-02-01T09:00:00+08:00

echo "== 9. 审阅新的共同披露申请（与在先共同披露共存，总计不重复） =="
$PY review --application A51 --issue

echo "== 10. 保守/上限情景对照（同一 MC 证据，不拼接最大项） =="
$PY analyze --scenario conservative --trials 1000
$PY analyze --scenario ambitious --trials 1000

echo "== 11. 产出下钻报告（JSON + Markdown） =="
$PY report --output-dir "$OUT"

echo
echo "完成。产物："
echo "  账本：$STATE/state/*.jsonl（追加式，原件不可变）"
echo "  报告：$OUT/analysis_report.json / analysis_report.md"

# 有界派工与止损

Codex 主控负责选择任务、确定证据边界、最终判断和集成。Flash 执行明确的文件核对、提取、修改和已有验证命令。跨骨架语义、时钟含义、根因定论及已有错误结论的裁决，由主控收口。能用脚本确定提取的字段先直接提取，不为再读一遍原件调用模型。

## 1. 输入先准备

不要给执行者整个日志目录或大清单，再让它自己找。新工单必须包含 `execution`，声明 1–16 个精确输入文件、输出文件和允许的命令。

从 JSON 原件选择字段时，用确定性工具保留值、原件 SHA256、JSON pointer 和事实 ID：

```powershell
python control_center.py make-packet examples/selection.local.json input/packet.json
```

selection 格式：

```json
{"sources":[{"path":"REPLACE_WITH_SOURCE_JSON","fields":[
  {"id":"action-metadata","pointer":"/action"},
  {"id":"known-errors","pointer":"/errors"}
]}]}
```

源文件仅在本机读取，最多 32 MiB；生成包最多 12000 字节。字段不存在直接报错，不自动省略。包内注明“仅选择的字段”，未出现的证据不得推断为原件不存在。时钟/单位/骨架方向等字段由主控在派工前确认；程序不能替代专业判断。

```json
"execution": {
  "inputs": ["input/packet.json"],
  "outputs": [],
  "commands": []
}
```

```powershell
python control_center.py prepare-order order.local.json prepared.local.json
python control_center.py submit prepared.local.json
python control_center.py status --task TASK_ID
```

准备过程自动记录输入哈希；提交或启动时输入变动、目录输入、过大输入均在请求 API 前拒绝。只读任务默认 12000 字节输入、4 次工具、4096 单请求最大输出、30000 跨轮累计 token；修改任务默认 24000 字节、12 次工具、8192 单请求最大输出、60000 跨轮累计 token。调用者可在允许范围内明确配置。上游累计预算会为下一请求预留输出，因此可能早于 API 实测 token 数触发，不能把它解读为实际已用完账面额度。

## 2. 执行中止损

- 只读计划只用声明文件的 Read。修改计划可用 Read/Edit/Write；Bash 必须逐字匹配预先声明的命令，未声明的搜索、命令和写入立即停下。
- 命令输出超过 8000 字节、累计读取超限、工具次数超限，停止本次执行，交主控准备更小输入或结果。
- 同一个文件、工具、错误类别出现第二次就停止，保留产物和 recovery，不继续烧下一轮 token。日志仅存错误类别，不存命令、正文或原始错误全文。
- CRLF 原件的有界 Read 会返回 LF；Edit 仍精确匹配。使用单行精确替换或显式保留 CRLF；Write 仅创建新文件。不要用其它工具绕过保护。
- 这些是父进程的协作限制，Bash 仍是 trusted-host 命令执行，不是操作系统沙箱。原有资源、目录和权限边界保持。

## 3. 默认一次，最多一次有证据的修复重试

新工单最多 2 个 attempt。返修先分类：`input`、`tool`、`budget` 可在修复条件成立后重试；`factual`、`capability` 由主控接手，不再给同一个执行者复述一次答案。

```powershell
python control_center.py review TASK_ID rework review.local.json
python control_center.py requeue TASK_ID "已验证的改变" --repair repair.local.json
```

repair 必须包含当前任务/attempt、failure_class、changed_conditions、替换后的完整 instruction、execution，以及本轮交付和实际修复证据的 SHA256。参考 `examples/repair.example.json`。只有失败回执不够；过期证据、未改变的 input/budget 计划、同类失败再次重试均拒绝。不能提高累计额度或扩大命令/输出范围。新指令替换旧指令，避免把互相矛盾的旧要求不断拼接。剩余额度不足 5000 时不再启动模型。旧工单历史保持，不能换 ID 规避失败。

## 4. 验收不靠自报

新任务 accepted 回执必须有 `adoption_scope`，以及与 `alignment.acceptance` 一一对应的 `criteria`。每项都要 `result: pass`，并引用已哈希的证据文件；缺项、unknown、fail 或无哈希证据不能接受。资料分析、编译、离线检查、实机输入、视觉和听感分别验收。

主控先运行确定性检查和最可能的失败反例，再作语义判断。事实报告要区分原始观察、推断、未知；帧数不等于招数，受击不等于防御，播放 FPS 不等于原时钟。无法核正的报告 closed 回收，不能为了提高通过率降低标准。

累计用量包含失败和返修；缓存是输入中的子集。历史回放和模拟工具测试证明止损边界，不代表已获得某个通用成功率或未来省费比例。

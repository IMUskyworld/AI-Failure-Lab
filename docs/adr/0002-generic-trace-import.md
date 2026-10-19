# ADR 0002：通用 trace 导入，而不是绑定某个 harness

- 状态：已采纳（2026-09-22，用户选定）
- 背景：绑定单一 harness（Claude Code / Qoder / Cursor）的导出格式能省一次映射，但会把工具锁死在一个生态里；而「AI 失败」这件事恰恰跨模型、跨 harness 才看得出模式。
- 决策：
  1. 定义**最小 trace schema**（model/harness/task/prompt/tool_calls/error/retries/final_result/cwd/时间）；
  2. 两种通用形态：`jsonl-events`（每行一个事件，kind 决定语义）与 `json-object`（单对象 + 字段别名映射表）；
  3. **宽容归一**：缺字段记 warning 并置默认值，坏行跳过但保留告警，绝不因格式问题丢弃整个转录；
  4. trace_id 由文件内容哈希生成 → 重复导入幂等；事故按签名（归一化错误 + 失败工具名）去重合并。
- 理由：任何 harness 的导出都能用一个 20 行的映射接进来；本机验收也因此可能（合成 fixture 覆盖两种格式与别名路径）。
- 代价：真实 harness 的怪格式需要用户自己写映射；版本一先用别名表覆盖常见形态，真实样本到位后再扩展。

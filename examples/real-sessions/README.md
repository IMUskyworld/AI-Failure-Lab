# real-sessions

`DEVELOPMENT.md` §24.5 dogfooding 用到的 4 份真实 agent 会话转录（qoder harness 的事件流 JSONL，2026-09-22 采集）。

- 这些是历史样本，不是测试 fixture（fixture 在 `tests/fixtures/traces/`）；`scripts/verify_mvp.py` 不依赖它们。
- 为公开仓库做了路径脱敏：`cwd` 与报错文本中的本机路径统一替换为 `C:/projects/...`，报错原文与事件语义未改动。因此直接重放 `repro` 很可能会因目录不存在而无法复现，仅建议用于 `import` 演示。
- 导入示例：

  ```powershell
  faillab import examples/real-sessions/20260922-check-ps1-policy.jsonl --harness qoder
  ```

- 转录可能含敏感信息（提示词、代码片段）：新增示例前请先脱敏（见 `SECURITY.md`）。

# faillab（AI Failure Lab）

[![ci](https://github.com/IMUskyworld/AI-Failure-Lab/actions/workflows/ci.yml/badge.svg)](https://github.com/IMUskyworld/AI-Failure-Lab/actions/workflows/ci.yml)

**把 AI coding agent 的失败变成可检索、可分析、可复现的事故记录。**

大家都在研究「AI 能不能写代码」；faillab 反过来研究「AI 是怎么失败的」。它导入 agent 会话转录（任何 harness 的事件流或单对象 JSON），自动检测失败、生成事故记录（FAILURE #N），分类根因、给出恢复建议，然后**真的重放复现命令**并判定三态：

- `reproduced` —— 失败信号仍在（问题没修）；
- `fixed` —— 命令正常完成、失败信号消失（**事故已修复——同类工具基本不给的结论**）；
- `inconclusive` —— 超时/启动失败/信号部分命中（明确不装懂）。

| | |
| --- | --- |
| 可复现性优先 | 每条事故带复现命令与失败信号；`repro` 用真实退出码与输出正则判定，不依赖任何模型 |
| 不装懂 | 分类/根因引用证据（命中的签名、错误文本、工具调用）；LLM 不可用或越界时降级为离线规则并记录原因 |
| 导入即归一 | 最小 trace schema（model/harness/task/prompt/tool_calls/error/retry/final）；未知字段宽容并记 warning |
| 去重合并 | 同签名自动合并（occurrences 计数 + trace 关联），事故库不重复膨胀 |
| 双后端 | LLM（OpenAI 兼容，Qwen 可用）负责分类/根因；无 key 时离线 taxonomy 规则全流程可用 |
| 本地优先 | SQLite 单库 + 自包含 HTML 仪表盘；零外部服务、无遥测；`export` JSON bundle 可进 git（公开分享前先脱敏） |

## 快速开始

> 依赖 Python ≥ 3.11；命令以 [uv](https://docs.astral.sh/uv/) 为例（用 pip 等价替换即可）。

```powershell
git clone https://github.com/IMUskyworld/AI-Failure-Lab.git; cd "AI Failure Lab"
uv venv --python 3.11
uv pip install -e ".[dev]"                  # 安装 faillab 命令 + 开发依赖（国内网络：UV_DEFAULT_INDEX=<镜像>）
.venv\Scripts\activate

# 导入一份 agent 会话转录（事件流 JSONL）
faillab import session-export.jsonl --harness claude-code --model claude-sonnet-4.5
faillab list
faillab show F-0001
faillab analyze F-0001            # 离线规则；配好 LLM 后自动用 LLM
faillab annotate F-0001 --cmd "python -m pytest tests/test_oauth.py" --pattern FAILED
faillab repro F-0001 --yes        # 重放 → reproduced / fixed / inconclusive
faillab report                    # 自包含 HTML 仪表盘
```

> **中文/空格路径注意**：editable install 的 `.pth` 在 cp936 等 locale + 非 ASCII 路径下会解码乱码，`faillab` 命令可能无法启动。此类环境请改用源码运行：`python scripts/faillab_dev.py <命令>`（背景见 `DEVELOPMENT.md` §22）。

```bash
# CI 用法：已知失败集的回归哨兵
faillab repro --all --yes
# 退出码：0 = 全部 fixed；1 = 至少一条 reproduced；2 = inconclusive / 未执行
```

## 命令

```
faillab import <file>    导入转录（jsonl-events / json-object）→ 检测失败 → 生成/合并事故
faillab record ...       手动登记一条事故（含复现命令、失败信号与 --status）
faillab list             过滤查询（category/model/harness/status/repro/关键词）
faillab show <id>        事故详情 + 证据 + 复现历史
faillab annotate <id>    为导入的事故补充/修改复现定义（--cmd/--pattern/--cwd/--timeout/--status）
faillab analyze <id>     分类/根因/恢复建议（--pending 批量；--backend auto|llm|offline）
faillab repro <id>       重放复现命令并判定三态（--all 批量；--yes 跳过确认；--timeout 覆盖记录值）
faillab status           总览统计
faillab report           HTML 仪表盘 + report.json（自包含，可离线打开）
faillab export <path>    JSON bundle 导出（import-bundle 合并回库）
faillab version          版本号
```

**退出码**：`0` 正常完成 / repro=fixed · `1` repro=reproduced · `2` 未执行或无法判定 · `3` 内部错误 · `130` 中断。

## 转录格式（最小 schema）

`jsonl-events`（每行一个事件）：

```jsonl
{"kind": "task", "task": "Add OAuth login", "model": "claude-sonnet-4.5", "harness": "claude-code", "cwd": "C:/projects/api"}
{"kind": "prompt", "text": "…"}
{"kind": "tool_call", "name": "Edit", "arguments": {"file": "config.py"}}
{"kind": "tool_result", "error": "string not found in config.py"}
{"kind": "error", "message": "Edit failed: string not found"}
{"kind": "retry"}
{"kind": "final", "result": "implemented OAuth", "ok": false}
```

`json-object`（单对象）接受常见别名（`model_name`/`tool`/`question`/`steps`/`exception`/`attempts`/`final`/`workdir` …，完整表见 DEVELOPMENT.md §9.2）。

## 安全边界（请读）

- 事件数据可能含敏感信息（提示词、代码片段）——faillab 只写本地数据库，**没有任何上传/遥测**；LLM 后端只发送脱敏摘要（家目录替换为 `<home>`）。
- `repro` 会在记录的项目目录里执行你记录的命令，**可能修改文件**：默认交互确认 + 超时/中断护栏；非交互无 `--yes` 一律不执行。
- `export` 的 bundle 与 `report.html` / `report.json` 含事件原文：公开分享前请先脱敏；`.faillab/` 已被 `.gitignore` 排除，但手动导出的文件不在保护范围内。
- 无自动修复：faillab 给恢复建议，改不改由你。

## 开发

```powershell
uv venv --python 3.11
uv pip install -e ".[dev]"                              # 或：uv sync --extra dev（走 uv.lock 锁定版本）
.venv\Scripts\activate
python -m pytest -q                                     # 单元 + 集成（含本地假 LLM 服务器与 plumbing）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1   # 完整质量闸门（ruff+pyright+pytest+§20 审计）
python scripts/verify_mvp.py                            # 逐条核验 DEVELOPMENT.md §20
python scripts/bench_overhead.py                        # §16 性能对照
```

中文/空格路径下无法 editable install 时，仍可直接 `python -m pytest -q` 与 `python scripts/faillab_dev.py <命令>`（pytest 通过 `pyproject.toml` 的 `pythonpath` 读取 `src/`）。

## 质量状态（v0.1.1）

- 123 项测试（单元 + 集成，含本地假 OpenAI 服务器端到端与真实子进程 plumbing）；`ruff check` / `ruff format --check` / `pyright` 全绿。
- `python scripts/verify_mvp.py --skip-tests`：DEVELOPMENT.md §20 共 12 条 **PASS / 0 SKIP / 0 FAIL**。
- 变更记录见 `CHANGELOG.md`；CI 见 `.github/workflows/ci.yml`（windows-latest + ubuntu-latest × Python 3.11/3.12，badge 见顶部）。

## English summary

faillab turns AI coding-agent failures into searchable, analyzable, **reproducible** incident
records. It imports session transcripts (event-stream JSONL or single-object JSON), detects
failures with data-driven rules, deduplicates by signature, classifies root causes (LLM with an
offline taxonomy fallback), and replays a recorded reproduction command to decide
reproduced / fixed / inconclusive with real exit codes and output patterns. Local-first SQLite
store, self-contained HTML dashboard, no telemetry.

## License

MIT

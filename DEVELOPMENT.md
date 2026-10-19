# faillab 开发文档（AI Failure Lab）

版本：v0.1.1（2026-10-19 硬化修订；原始 v0.1 规格于 2026-09-22）
状态：实现中；逐条验收见 §24。

一句话：把 AI coding agent 的失败变成**可检索、可分析、可复现**的事故记录——不是又一个 failure memory，而是把「事故记录」升级成「可验证的事故实验」。

```
导入转录 → 检测失败 → 事故记录（去重）→ 分类/根因分析 → 复现重放（reproduced/fixed）→ 仪表盘
```

## 1. 项目定位

现在大家都在研究「AI 能不能写代码」；faillab 反过来研究「AI 是怎么失败的」。区别同类工具的关键在于**可复现性优先**：

- 纯 failure memory 只记录「发生过什么」；
- replay/flight-recorder 工具偏重「过程回放」；
- **faillab 要求每条事故带可执行的复现命令与失败信号**——`faillab repro` 会真的重放命令并判定三态：
  - `reproduced`：失败信号仍在（问题没修 / 环境一致）；
  - `fixed`：命令正常完成、失败信号消失（事故已修复——**这是同类工具基本不给的结论**）；
  - `inconclusive`：超时、启动失败或信号部分命中（明确不装懂）。

四条硬性原则：

- **可复现性优先**：没有复现定义的事故会被显式标记为「未验证」，而不是假装可信。每条记录的 `reproduction.status` 随真实重放结果演进。
- **不装懂**：分类/根因必须引用证据（命中的签名、错误文本、工具调用）；LLM 不可用或输出越界时降级为离线规则并记录原因。
- **导入即归一**：任何 harness 的转录都映射为统一的最小 trace schema（model/harness/task/prompt/tool_calls/error/retry/final）；缺字段宽容处理并记 warning，不丢弃来源信息。
- **执行即权力**：`repro` 会在你的项目目录里执行你记录的命令——默认交互确认，超时/中断全部有护栏。

## 2. MVP 范围

**做：**

- **导入**：两种转录形态 → 归一化 Trace：
  - `jsonl-events`：事件流（每行一个事件：task/prompt/tool_call/tool_result/error/retry/final/note）；
  - `json-object`：单对象（字段别名映射，适配常见导出）。
- **失败检测**：数据驱动规则（`data/detection.json`）：error 存在 / 最终结果为失败态 / 重试达阈值 / 工具调用错误；命中规则写入证据。
- **事故记录**：自动生成 FAILURE #N（F-0001 序列）——task / failure / reason / recovery / evidence / reproduce / reproduction；签名去重（同签名合并，occurrences 计数，trace 关联）。
- **手动登记**：`record` 命令（事故发生在转录之外时）。
- **分类与根因**：taxonomy（JSON 数据文件，10 类）+ 相似先例；离线签名匹配与 LLM 双后端（LLM 输出走白名单校验，失败降级）。
- **复现实验室**：`repro` 重放命令并按信号判定 reproduced / fixed / inconclusive；历史落库；`--cwd` 覆盖；`--all` 批量。
- **查询与统计**：list/search（category/model/harness/status/repro/关键词过滤）、show（详情 + 证据 + 复现历史）、status 总览。
- **报告**：自包含 HTML 仪表盘（手写 SVG 条形图：分类/模型/harness 分布、复现率、top signatures、最近事故）+ report.json。
- **导出**：`export` JSON bundle（可进 git / 分享）；`import-bundle` 合并回库。

**不做（MVP 明确排除）：**

- 不运行 agent harness 本身（不集成 Claude Code / Qoder / Cursor 的 API）；faillab 消费转录，执行层只做**命令级复现**。
- 不做 web 服务 / 多用户 / 权限；MVP 是本地 CLI + 静态 HTML。
- 不做自动修复（只给 recovery 建议）；不改动你的仓库。
- 不做遥测、不上传任何数据。
- 不做跨机器同步（export/import 是手动通道）。

## 3. 目标用户和使用场景

| 用户 | 场景 |
| --- | --- |
| 本人（3090 机器 + 各种 agent） | 把 agent 跑挂的会话导入 faillab，分析「它错在哪」，攒出可复现的失败集；修完 bug 后 `repro` 一键确认 fixed。 |
| 研究者 / 同学 | 收集同一个任务在不同 model/harness 下的失败，用分类统计找模式（哪个模型爱幻觉 API？哪类任务总翻车？）。 |
| CI（进阶） | `faillab repro --all` 作为「已知失败集」的回归哨兵：退出码 1 表示失败仍可复现。 |

典型流程：

```bash
faillab import session-export.jsonl --harness claude-code --model claude-sonnet-4.5
faillab list --status reproduced
faillab show F-0003
faillab analyze F-0003                 # 分类/根因/恢复建议（LLM 或离线）
faillab repro F-0003 --yes             # 重放 → fixed / reproduced / inconclusive
faillab report                         # 仪表盘
```

## 4. CLI 设计

基于 typer。

```
faillab import <file> [--harness NAME] [--model NAME] [--dry-run] [--new]
faillab record --title ... --failure ... [--model ...] [--harness ...] [--task ...]
                [--cmd ...] [--pattern ...] [--exit-nonzero/--no-exit-nonzero] [--cwd ...]
                [--status open|reproduced|fixed|wontfix] [--new]
faillab list [--category ...] [--model ...] [--harness ...] [--status ...] [--repro ...] [--query ...] [--limit N]
faillab show <id>
faillab annotate <id> [--cmd ...] [--pattern ...] [--cwd ...] [--timeout S]
                      [--exit-nonzero/--no-exit-nonzero] [--title ...] [--recovery ...]
                      [--status open|reproduced|fixed|wontfix]
faillab analyze <id> | --pending [--backend auto|llm|offline] [--config PATH]
faillab repro <id> | --all [--yes] [--timeout S] [--cwd DIR]
faillab status
faillab report [--html PATH] [--json PATH]
faillab export <path>
faillab import-bundle <path>
faillab version
```

**退出码**（稳定契约）：

| 码 | 含义 |
| --- | --- |
| 0 | 正常完成；`repro` 语义：判定为 `fixed`（失败不复现） |
| 1 | `repro` 语义：至少一条判定为 `reproduced`（失败仍存在） |
| 2 | 未执行 / 无法判定（无复现定义、确认被拒、非交互且无 `--yes`、全部 inconclusive） |
| 3 | 内部错误 |
| 130 | 用户中断（子进程已终止，历史保留） |

`import` / `record` / `list` / `show` / `analyze` / `status` / `report` / `export` 成功即 0，错误 3。

## 5. 总体架构

```
  转录文件 (jsonl-events / json-object)         手动 record
        │                                            │
   ┌────┴─────────┐                                   │
   │  import       │ 归一化 Trace + 检测规则 + 签名去重  │
   └────┬─────────┘                                   │
        │                    ┌────────────────────────┘
        ▼                    ▼
   ┌──────────────────────────────┐
   │  SQLite 事故库 (.faillab/db)  │  incidents / traces / repro_runs / meta
   └────┬───────────┬─────────────┘
        │           │
   ┌────┴─────┐ ┌───┴──────────┐
   │ analyze  │ │  repro       │  重放命令 + 失败信号判定
   │ 双后端    │ │  (实验室)     │  reproduced / fixed / inconclusive
   └────┬─────┘ └───┬──────────┘
        │           │            repro_runs 历史 + incident.status 演进
        ▼           ▼
   ┌──────────────────────────────┐
   │ list / show / status / report │  HTML 仪表盘（自包含 + SVG）
   └──────────────────────────────┘
```

模块划分（`src/faillab/`）：

| 模块 | 职责 |
| --- | --- |
| `domain/` | pydantic 模型 + 枚举（Trace / Incident / ReproSpec / ReproRun / TaxonomyCategory / Report） |
| `store.py` | SQLite 单库（incidents/traces/repro_runs/meta）、F-0001 序列、过滤器查询、bundle 导出/导入 |
| `ingest/` | 格式解析（jsonl-events / json-object 别名映射）、检测规则、事故生成与签名去重 |
| `analyze/` | taxonomy 匹配、相似先例、LLM 后端（白名单校验 + 降级） |
| `repro/` | 信号模型、执行器（超时/日志/中断）、三态判定、历史落库 |
| `report/` | console 列表/详情、HTML 仪表盘（手写 SVG）、report.json |
| `cli/` | 命令接线、确认门、退出码、中断处理 |

## 6. 运行生命周期

`faillab repro <id>`：

1. **load**：读取事故；无 `reproduce` 定义 → 明确说明并 exit 2。
2. **gate**：打印将执行的命令、目录、超时与风险文案，等待 `[y/N]`；`--yes` 跳过；非交互且无 `--yes` → 不执行，exit 2。
3. **run**：在 `reproduce.cwd`（可 `--cwd` 覆盖）执行命令；子进程 stdout+stderr 合并捕获（字节级日志），超时 terminate → 5s 宽限 → kill。
4. **judge**：按信号判定三态（§12.2）。
5. **persist**：写 `repro_runs`；更新 `incident.reproduction` 与 `status`。
6. **exit**：按 §4 复现退出码契约。

**中断**：Ctrl+C → 终止子进程 → 该次 run 记为 `interrupted`（不计入三态判定）→ exit 130。

## 7. 本地目录协议

```
<workdir>/                      # 默认 ./.faillab
├── faillab.db                  # SQLite：incidents / traces / repro_runs / meta
└── logs/
    └── <public_id>/
        └── repro-<timestamp>.log   # 每次重放的原始输出（UTF-8 落盘）
```

- 数据库是唯一事实来源；`export` 产出 JSON bundle（`faillab-bundle.json`），`import-bundle` 合并。
- 所有落盘 UTF-8；子进程输出按 UTF-8 → GBK → replace 三级解码（LabBox 教训）。

## 8. 数据协议

模型见 `domain/models.py`，schema 导出到 `docs/schemas/`。

### 8.1 Trace（归一化转录，存 traces 表）

```jsonc
{
  "trace_id": "tr-20260922-ab12cd",
  "harness": "claude-code",
  "model": "claude-sonnet-4.5",
  "task": "Add OAuth login to the API",
  "prompt": "…初始提示…",
  "tool_calls": [
    { "index": 0, "name": "Edit", "arguments": "file=config.py", "result": "ok", "error": null }
  ],
  "error": "Edit failed: string not found in config.py",
  "retries": 2,
  "final_result": "implemented OAuth",
  "started_at": "…", "ended_at": "…",
  "cwd": "C:/projects/api",
  "warnings": [ { "code": "MISSING_FIELD", "message": "缺少 harness，已置为 unknown" } ]
}
```

### 8.2 Incident（事故记录，存 incidents 表 document）

```jsonc
{
  "public_id": "F-0003",
  "slug": "add-oauth-wrong-config",
  "title": "Add OAuth：agent 改错配置文件",
  "task": "Add OAuth login to the API",
  "failure": "Agent 修改了错误的 config 文件，测试未通过的改动被留下",
  "category": "context_misidentification",
  "category_label": "上下文误判",
  "reason": "匹配签名 tool_error:not-found → 编辑目标定位失败",   // 离线或 LLM
  "recovery": "编辑前先搜索仓库定位文件；对关键配置先跑测试再提交",
  "severity": "high",
  "confidence": "medium",
  "model": "claude-sonnet-4.5",
  "harness": "claude-code",
  "signature": "a1b2c3d4e5f6",
  "occurrences": 2,
  "duplicate_of": null,
  "status": "reproduced",           // open | reproduced | fixed | wontfix
  "evidence": [
    { "kind": "detection", "detail": "规则 tool_errors 命中：Edit 报 not found" },
    { "kind": "tool_call", "detail": "#1 Edit file=config.py → error" }
  ],
  "reproduce": {                    // 可为 null（未定义 → 显式「未验证」）
    "cwd": "C:/projects/api",
    "command": "python -m pytest tests/test_oauth.py",
    "pattern": "FAILED",
    "expect_exit_nonzero": true,
    "timeout_s": 300
  },
  "reproduction": {
    "status": "reproduced",         // unverified | reproduced | fixed | inconclusive
    "last_run_at": "…",
    "total_runs": 3, "reproduced_count": 2, "fixed_count": 0
  },
  "traces": ["tr-20260922-ab12cd", "tr-20260923-77ff01"],
  "created_at": "…", "updated_at": "…"
}
```

### 8.3 ReproRun（repro_runs 表）

```jsonc
{
  "incident_id": "F-0003",
  "started_at": "…", "ended_at": "…", "duration_s": 12.4,
  "exit_code": 1, "pattern_matched": true, "exit_nonzero_matched": true,
  "status": "reproduced",           // reproduced | fixed | inconclusive | interrupted
  "note": "失败信号全部命中",
  "log_path": "logs/F-0003/repro-20260922-221500.log"
}
```

### 8.4 Report（report.json）

```jsonc
{
  "schema_version": "1.0",
  "tool": { "name": "faillab", "version": "0.1.1" },
  "generated_at": "…",
  "totals": { "incidents": 12, "open": 5, "reproduced": 4, "fixed": 3, "unverified": 4 },
  "by_category": [ { "category": "context_misidentification", "label": "上下文误判", "count": 5 } ],
  "by_model": [ { "model": "claude-sonnet-4.5", "count": 7 } ],
  "by_harness": [ { "harness": "claude-code", "count": 9 } ],
  "top_signatures": [ { "signature": "a1b2c3d4e5f6", "title": "Add OAuth：…", "occurrences": 3 } ],
  "recent": [ "…Incident 摘要…" ]
}
```

## 9. trace 导入（ingest）

### 9.1 格式 jsonl-events

每行一个 JSON 事件；`kind` 决定语义：

| kind | 字段 | 说明 |
| --- | --- | --- |
| `meta` / `task` | `task|question`、`model`、`harness`、`cwd`、`started_at`、`ended_at` | 元信息（可分散多行） |
| `prompt` | `text|prompt` | 初始提示（首个为准） |
| `tool_call` | `name`、`arguments`（对象或字符串） | 工具调用 |
| `tool_result` | `result`、`error`（可选） | 工具返回（按序匹配最近的 tool_call） |
| `error` | `message` | 显式错误 |
| `retry` | `attempt`（可选） | 重试计数 +1 |
| `final` | `result|status|ok` | 最终结果（`ok:false` 计为失败态） |
| `note` | `text` | 其他事件，进 events 供证据用 |

### 9.2 格式 json-object

单对象 + 字段别名映射（缺失宽容、记 warning）：

| 目标字段 | 接受别名 |
| --- | --- |
| model | `model` / `model_name` / `agent_model` |
| harness | `harness` / `tool` / `client` / `agent` |
| task | `task` / `question` / `instruction` / `title` |
| prompt | `prompt` / `system_prompt` / `instructions` |
| tool_calls | `tool_calls` / `steps` / `actions` |
| error | `error` / `exception` / `failure` |
| retries | `retries` / `attempts` / `retry_count` |
| final_result | `final_result` / `final` / `result` / `outcome` |
| cwd | `cwd` / `workdir` / `directory` |

### 9.3 检测（data/detection.json，数据驱动）

```jsonc
{ "rules": [
  { "id": "error_present",  "when": "error",            "detail": "trace 记录了 error" },
  { "id": "final_failed",   "when": "final_result",     "pattern": "(?i)fail|error|abort|无法|失败", "detail": "最终结果为失败态" },
  { "id": "retry_storm",    "when": "retries", "min": 3, "detail": "重试次数达到阈值（≥3）" },
  { "id": "tool_errors",    "when": "tool_call_errors", "min": 1, "detail": "存在工具调用错误" }
] }
```

任一规则命中 → 该 trace 计为失败 → 生成事故；命中的规则全部写入 `evidence`。

### 9.4 去重

- 签名：`sha1(normalize(error_text) + "|" + ",".join(失败的 tool 名))[:12]`；
  `normalize` = 小写、去除十六进制/数字串、路径归一为 `\S+`、连续空白折叠、截断 200 字符。
- 导入时签名已存在 → **合并**：`occurrences += 1`、trace 关联到既有事故、不新建记录；
- `record --new` / `import --new` 强制新建；手动 `record` 撞名时提示既有事故 ID。

## 10. 分类与根因（analyze，双后端）

taxonomy 数据文件 `data/taxonomy.json`：`{id, label(中文), description, severity, recovery_template, signatures: [{field: error|final|tool_error|task, pattern}]}`。
顺序敏感（具体在前，兜底在后）：

| id | 标签 | 典型签名 |
| --- | --- | --- |
| `retry_loop` | 重试风暴 | retries ≥ 3 且无进展 |
| `hallucinated_api` | 幻觉 API/符号 | `ModuleNotFoundError` / `has no attribute` / `undefined` |
| `tool_misuse` | 工具参数误用 | `invalid argument` / `unexpected keyword` / `usage:` |
| `env_permission` | 环境/权限问题 | `Permission denied` / `EACCES` / `command not found` / `UnauthorizedAccess` / `WinError \d+`（真实事故 F-0001/F-0002 补充） |
| `encoding_locale` | 编码与语言环境 | `UnicodeDecodeError` / `codec can't decode` / `gbk` / `cp936`（真实事故 F-0003 补充） |
| `wrong_file_edit` | 改错文件/位置 | `string not found` / `no such file` / `did not apply` |
| `context_misidentification` | 上下文误判 | 编辑成功但目标错误、`expected X found Y` |
| `test_failure_ignored` | 测试失败未处理 | 最终宣布完成 + 输出含 `FAILED` / `failed` |
| `premature_completion` | 过早宣布完成 | final 含 `done|implemented|完成` 但存在 error |
| `regression_introduced` | 引入回归 | `existing tests? failed` / `broke` / `regression` |
| `unclassified` | 未分类（兜底） | 无匹配 → 留给 LLM 或人工 |

- **离线后端**：逐条签名匹配（首个命中即类别），`reason` = 命中模式 + 关键错误文本，`recovery` = 模板；`confidence`：明确签名 high / 模糊 medium / 兜底 low。
- **相似先例**：同 category 的其他事故（最多 3 条，按 occurrences 降序）写入分析结果，供人工参考。
- **LLM 后端**：输入为 trace 摘要（task/prompt 截断、工具调用序列、错误、检测证据）；要求严格 JSON `{category, reason, recovery, severity, confidence}`；`category` 必须 ∈ taxonomy（白名单校验，越界即失败）；失败重试 1 次；仍失败 → 离线降级 + `fallback_reason`。
- 分析结果写回 incident（`category/label/reason/recovery/severity/confidence`），`--pending` 批量处理未分析事故。

## 11. 复现实验室（repro）

### 11.1 信号模型

```jsonc
{ "cwd": "C:/projects/api", "command": "python -m pytest tests/test_oauth.py",
  "pattern": "FAILED", "expect_exit_nonzero": true, "timeout_s": 300 }
```

配置的信号（`pattern` / `expect_exit_nonzero` 至少一个，都没有 → 事故标记「未验证」并拒绝执行）：

### 11.2 三态判定

| 条件 | 判定 |
| --- | --- |
| 全部配置信号命中（pattern 匹配 且/或 非零退出） | `reproduced`（失败仍在） |
| 命令以 0 退出且无任何信号命中 | `fixed`（失败不复现） |
| 结果混合（部分命中）、超时、启动失败 | `inconclusive`（附原因） |
| 命令**非零退出**但失败信号未命中（仅 pattern 信号模式下） | `inconclusive`——无法区分「已修复」与「命令本身出错」（真实事故 F-0002 的教训） |

判定不依赖任何模型：只看退出码与输出正则——确定性、可测试。

### 11.3 执行与安全

- 在 `reproduce.cwd`（`--cwd` 可覆盖）执行；**可能修改你的项目文件**——确认门文案明示，建议先提交/清理工作区。
- 超时终止整个进程树（Windows `taskkill /F /T`；POSIX 进程组）→ 5s 宽限 → 强制终止；输出合并落 `logs/<id>/repro-<ts>.log`；`--timeout` 覆盖记录值。
- 执行前校验复现定义：`pattern` 必须是合法正则、命令非空、超时为正；不合法 → 不执行（exit 2，`REPRO_SPEC_INVALID`）。
- `--all` 顺序执行所有「有复现定义」的事故；单次确认（`--yes` 跳过）；每条独立超时；无效定义逐条跳过并让本次退出码保持 2。
- 中断：终止子进程，该 run 记 `interrupted`（不参与三态统计），exit 130。

### 11.4 状态演进

`incident.reproduction`（status/last_run_at/计数）与 `incident.status`（open→reproduced / fixed）随每次 run 更新；`wontfix` 仅可人工设置（`record --status` 或 `annotate --status`）。

## 12. 报告与查询

- **list/search**：过滤器（category/model/harness/status/repro/关键词 LIKE 标题+失败+原因）+ `--limit`；控制台表格（ID/标题/类别/状态/复现/模型/出现次数）。
- **show**：详情 + 证据列表 + 复现历史（最近 5 次）+ 复现定义（命令高亮）。
- **status**：总数、状态分布、复现率、未验证数。
- **report**：`report.json`（§8.4）+ 自包含 `report.html`：
  - 顶部摘要卡（总数 / reproduced / fixed / unverified / 复现率）；
  - SVG 条形图：分类分布、模型分布、harness 分布（手写 SVG，无外部资源）；
  - Top signatures 表（按 occurrences）、最近事故表；
  - 完全自包含（无外部 CSS/JS/字体/图片）。

## 13. 技术栈

| 依赖 | 用途 |
| --- | --- |
| `typer` | CLI |
| `pydantic` v2 | 数据模型与 schema |
| `rich` | 控制台 |
| `jinja2` | HTML 模板 |
| `httpx` | LLM 调用 |

存储：Python 标准库 `sqlite3`（无 ORM）；HTML/SVG 手写（无前端依赖）。
开发依赖：pytest / pytest-cov / ruff / pyright。Python ≥ 3.11。构建后端 hatchling。
**运行时零外部服务依赖**；LLM 可选（无 key 时离线全流程可用）。

## 14. 配置设计

查找顺序：`--config` > `./faillab.toml` > `%APPDATA%/faillab/config.toml`（或 `~/.config/faillab/config.toml`）。

```toml
[defaults]
repro_timeout_s = 300
workdir = ".faillab"

[llm]
enabled = false
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen3.8-flash"
```

环境变量（优先级最高）：`FAILLAB_LLM_BASE_URL` / `FAILLAB_LLM_MODEL` / `FAILLAB_LLM_API_KEY`。
密钥只从环境变量读取，**不落盘、不入日志、不入报告**。

## 15. 安全与隐私

- **执行第三方命令 = 以你的权限运行你的代码**：`repro` 默认交互确认 + 明示「可能修改项目文件」；非交互无 `--yes` 一律不执行（exit 2）。
- 转录内容可能含敏感信息（提示词、代码片段）——faillab 只写入本地数据库；`report` 是本地文件；**没有任何上传/遥测**。
- LLM 后端只发送 trace 摘要（家目录路径脱敏；不含完整文件内容）；密钥不入任何持久化产物。
- 超时/中断护栏同 §11.3。

## 16. 性能要求（软目标）

| 操作 | 目标 |
| --- | --- |
| import（100 trace 文件） | ≤ 1.0s |
| detect + 去重 | 含在 import 内 |
| analyze（离线，单条） | ≤ 0.1s |
| repro（单条） | 由命令决定 |
| list/search | ≤ 0.2s（≤1000 条事故） |
| report（含 SVG） | ≤ 1.0s |

`scripts/bench_overhead.py` 输出实测对照。

## 17. 测试策略

| 层级 | 内容 | 运行条件 |
| --- | --- | --- |
| unit | 两种导入格式（含别名/缺字段/坏行）、检测四规则与阈值、签名归一化与去重合并、record 撞库、taxonomy 每类别 + 兜底、相似先例、LLM 客户端（MockTransport：正常/越界类别/垃圾/HTTP 错误）、repro 三态判定边界（全中/全不中/混合/超时/启动失败）、store CRUD/过滤/序列/持久化、bundle 往返、SVG 生成、console 渲染 | 永远 |
| integration | 本地**假 OpenAI 服务器**驱动 analyze LLM 路径；CLI 端到端：import×2（去重）→ list/show → analyze → repro（假层与真实 echo 两级）→ report；确认门非交互行为；退出码契约 | 永远（无外网） |
| slow | **plumbing**：真实子进程 echo 级命令重放（reproduced/fixed 两态）+ 日志落盘 | 默认跑（<5s） |
| gpu | 无——本项目不需要 GPU | — |

fixtures：`tests/fixtures/traces/`：`session.jsonl`（事件流，含 error+retry+final failed）、`result.json`（单对象别名形态）、`clean.jsonl`（成功会话 → 不生成事故）、`dup.jsonl`（与 session.jsonl 同签名 → 去重合并）。

## 18. 里程碑

| 里程碑 | 内容 | 完成标志 |
| --- | --- | --- |
| M1 | domain + store + paths/config/errors | CRUD/序列/过滤有测试 |
| M2 | ingest（导入/检测/去重） | 两种格式 + 四规则 + 去重用例通过 |
| M3 | analyze 双后端 + taxonomy | 每类别 + 降级 + 白名单用例通过 |
| M4 | repro 实验室 | 三态全分支 + plumbing 通过 |
| M5 | report + list/show/status | 自包含 + 数值断言通过 |
| M6 | CLI 接线 + 闸门 + 文档 + CI | 全部质量闸门绿，verify_mvp 全 PASS/SKIP（有原因） |

## 19. 实现顺序

M1 domain/store → M2 ingest → M3 analyze → M4 repro → M5 report → M6 CLI → 闸门/文档。

## 20. MVP 完成定义（可执行标准）

以下 12 条全部有可执行证据才算 MVP 完成：

1. `import` jsonl-events fixture → 生成事故记录，字段齐全（task/failure/model/harness/evidence/detection 规则记录）。
2. `import` json-object fixture（别名映射）→ 正确归一；缺字段宽容 + warning 记录。
3. 去重：同签名第二次导入 → 不新建记录、`occurrences=2`、trace 关联到既有事故。
4. `record` 手动登记；撞库时提示既有事故 ID；`--new` 强制新建。
5. `analyze` 离线：taxonomy 每条类别有正例用例；无匹配 → `unclassified`；相似先例填充。
6. `analyze` LLM（本地假服务器）：白名单校验通过；越界类别/垃圾输出 → 降级 + `fallback_reason`。
7. `repro` 三态：假执行层覆盖 reproduced / fixed / inconclusive 全分支；plumbing 真实子进程（echo 级）两级验证。
8. `repro` 退出码契约：0=fixed、1=reproduced、2=inconclusive/未执行；非交互且无 `--yes` 零执行。
9. `repro` 历史落库 + `incident.status` 随最新判定演进（open→reproduced→fixed）。
10. `report`：HTML 自包含（无外部引用）+ 含 SVG + `report.json` 往返；`list`/`show` 过滤正确。
11. `export` / `import-bundle` 往返一致（数量与关键字段）。
12. 质量闸门：`ruff` + `pyright`(src) + `pytest` 全绿；`scripts/verify_mvp.py` 逐条 PASS/SKIP（SKIP 必须带原因）。

## 21. 已定稿决策与剩余待确认问题

### 21.1 已定稿（2026-09-22，用户确认）

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 命名 | 文件夹 `AI Failure Lab`；包/命令 `faillab` | 用户选定；直述「失败实验室」 |
| 差异化 | **可复现性优先**：事故必带复现命令与失败信号；`repro` 三态判定；状态随真实重放演进 | 用户选定；现有 failure-memory/replay 工具最缺的一环 |
| 数据来源 | 通用 trace 导入（最小 schema + 两种格式 + 手动 record） | 用户选定；不绑定单一 harness，本机可全流程验收 |
| LLM 定位 | 双后端：LLM 分类/根因 + 离线 taxonomy 规则，无 key 全流程可用 | 用户选定；与 LabBox/envdoctor/labloop 同一哲学 |
| 存储 | SQLite 单库（stdlib sqlite3）+ JSON bundle 导出 | 事故库定位；零外部依赖；bundle 通道可进 git |
| 检索 | LIKE 过滤（不做 FTS5） | 规模（千级）足够；避免 sqlite 编译差异 |

### 21.2 待确认与后续扩展（不阻塞）

- 仓库初始化：已于 2026-10-19 完成 `git init`（`main` 分支）与首次提交（见 §24.7）。
- 真实 harness 转录样本：qoder 的事件流已导入验证（§24.5）；其他 harness（Claude Code / Cursor 等）的导出如出现新字段形态，按 ADR 0002 扩展别名表。

## 22. 开发约束（继承前三个项目的实测教训）

1. **CJK + 空格路径**：开发机项目位于含中文与空格的路径（如 `C:\code\中文目录\AI Failure Lab`），editable install 的 `.pth` 会因 locale 解码乱码 → 改用源码运行（`scripts/faillab_dev.py` / pytest `pythonpath`）。
2. **控制台 cp936**：CLI 入口 typer callback 里 `reconfigure(encoding="utf-8", errors="replace")`（dev shim 直接调 `app()` 会绕过 `main()`——LabBox 的坑）。
3. **PyPI 直连不可达**：开发期依赖用清华镜像（`UV_DEFAULT_INDEX=<mirror>`）。
4. **子进程输出永不假设 UTF-8**：UTF-8 → GBK → replace 三级解码。
5. **无 GPU 需求**：本项目纯本地可验证（与前三个不同，验收更完整）。

## 23. 与其它三个项目的关系

LabBox（记录实验）/ envdoctor（诊断环境）/ labloop（自动实验循环）与 faillab（收集失败）是同一工作流的四段：
faillab 的 trace 若来自「被 envdoctor 诊断过、被 labloop 编排过」的机器，事故记录可直接引用其证据术语（环境指纹、可比性）。
MVP 不做跨项目集成（各自独立、零耦合），保留这条产品线叙事。

## 24. 实现状态（MVP 完成定义逐条核验）

> 回填于 2026-09-22。验收脚本：`python scripts/verify_mvp.py`（`--skip-tests` 可在调用方已跑过 pytest 时使用）。

### 24.1 逐条核验（§20）

本机（Windows 11、无 GPU 需求、无 LLM 端点，使用本地假 LLM 服务器）实测：**12 PASS / 0 SKIP / 0 FAIL**。

| # | 标准 | 结果 | 证据摘要 |
| --- | --- | --- | --- |
| 20-1 | import jsonl-events | PASS | F-0001 字段齐全，命中规则 error_present / final_failed / tool_errors 全部写入证据 |
| 20-2 | import json-object 别名 | PASS | gpt-5.2 / qoder 别名正确归一；缺字段宽容 + MISSING_FIELD warning |
| 20-3 | 去重 | PASS | 同签名二次导入 → 合并（occurrences=2、trace 关联、库内不新增） |
| 20-4 | record | PASS | 手动登记 + 撞库合并提示 + `--new` 强制新建 |
| 20-5 | analyze 离线 | PASS | taxonomy 12 条正例（10 类）全命中；无匹配 → unclassified；相似先例填充；结果写回 |
| 20-6 | analyze LLM | PASS | 真 httpx 路径命中本地假服务器；垃圾输出 / 越界类别 → 降级 + `fallback_reason`（含白名单校验） |
| 20-7 | repro 三态 | PASS | 假执行层覆盖 reproduced / fixed / inconclusive / 超时全分支 |
| 20-8 | repro 退出码契约 | PASS | 非交互无 `--yes` → 零执行（exit 2）；真实子进程（echo 级）0=fixed / 1=reproduced / 2=inconclusive |
| 20-9 | 复现历史与状态演进 | PASS | 2 次历史落库；status 随判定演进 reproduced → fixed；计数 1/1 |
| 20-10 | report / list | PASS | 仪表盘自包含（无外链）+ SVG；report.json 往返；list 过滤正确 |
| 20-11 | bundle 往返 | PASS | export → import-bundle：数量与签名一致；重复导入全部跳过 |
| 20-12 | 质量闸门 | PASS | ruff + ruff format + pyright(0 errors) + pytest 全绿 |

测试规模：123 项（单元 + 集成，含本地假 OpenAI 服务器端到端与真实子进程 plumbing；2026-10-19 硬化后）。

### 24.2 性能实测（§16 目标对照）

`python scripts/bench_overhead.py`（取最小值）：

| 操作 | 实测 | 目标 |
| --- | --- | --- |
| import 100 traces（含 detect + 建库） | 0.774s | ≤ 1.0s |
| analyze（离线，单条） | 0.001s | ≤ 0.1s |
| search over 1000 incidents | 0.001s | ≤ 0.2s |
| report（build + HTML + JSON） | 0.023s | ≤ 1.0s |

### 24.3 本机无法执行 / 交接清单

1. **外部 harness 转录样本**：已有真实 agent（qoder）会话导入并跑通全链路（见 §24.5，样本在 `examples/real-sessions/`）；其他 harness（Claude Code / Cursor 等）的导出如有新字段形态，按 ADR 0002 扩展别名表即可。
2. **他机项目上的复现命令**：`repro` 需要事故所在项目的环境与命令；本机已完成四条真实事故的复现（含跨项目 cwd）——剩余的是他人机器上的私有项目。
3. **真实 LLM 端点**：本机用本地假服务器验证 httpx 路径与降级逻辑；配置 `FAILLAB_LLM_BASE_URL/MODEL/API_KEY` 后即为可用性交接项。
4. **仓库初始化**：已于 2026-10-19 完成 `git init`（`main` 分支）与首次提交。

### 24.5 Dogfooding 实录（2026-09-22）

用 faillab 处理了**本次开发会话中真实发生的 4 起事故**（原始报错文本进入 trace，样本见 `examples/real-sessions/`）：

| ID | 事故 | 分类 | 复现判定 |
| --- | --- | --- | --- |
| F-0001 | `powershell -File check.ps1` 被本机执行策略拦截 | 环境/权限问题 | **reproduced**（信号仍在，0.5s；根因是机器策略，文档已补 Bypass 说明） |
| F-0002 | envdoctor 审计脚本 `run_cli` 的 cwd 未创建（WinError 267） | 环境/权限问题 | **fixed**（真实跑完整审计 12.1s，exit 0） |
| F-0003 | envdoctor CLI 重定向输出非 UTF-8（dev shim 绕过 reconfigure） | 编码与语言环境 | **fixed**（配套回归守卫测试已加入 envdoctor 测试套件） |
| F-0004 | cmd /c 对首位带引号命令解析失败，且判定链曾误判为 fixed | 环境/权限问题 | **fixed**（2026-10-19 改用 shell 原样传参后重放：15.6s，exit 0） |

Dogfooding 直接产出的产品改进（已合入）：

1. **判定诚实性修复**：命令非零退出且失败信号未命中时，从 `fixed` 改判 `inconclusive`——该缺陷正是 F-0002 首次重放时暴露的（修复后立刻又抓出 F-0003 的同类误判）。
2. **taxonomy 补真实案例**：新增 `encoding_locale`（编码与语言环境）类别；`env_permission` 增补 `UnauthorizedAccess` / `WinError \d+` / `不是内部或外部命令` 等真实形态（此前三条真实事故全部落到 unclassified）。
3. **配套修复**：envdoctor 新增 UTF-8 重定向回归守卫测试（守护 F-0003）；四个项目的 `check.ps1` 用法说明补 `-ExecutionPolicy Bypass`（F-0001 的缓解，机器策略本身属用户级设置）。
4. **复现执行器引号修复**：`spawn` 不再用 `["cmd", "/c", command]`（Python 会二次转义命令自带的引号），改为把原始命令交给 shell；F-0004 从 reproduced 转为 fixed，并新增真实子进程回归测试。

### 24.4 与规格书的已知偏差（诚实性记录）

- `annotate` 已补入 §4：为导入的事故补充/修改复现定义与人工字段——没有它，「可复现性优先」对导入事故无法落地。
- `import` 额外接受 `--cmd/--pattern/--cwd/--timeout/--exit-nonzero`（§4 未列出）：导入时直接附带复现定义。
- `repro` 的汇总行按单次调用统计（`--all` 的 CI 语义），不累计历史——历史计数在 `incident.reproduction` 与 `repro_runs` 中。

### 24.6 硬化修订（2026-10-19）

一次以「修 bug + 补契约」为目标的收敛：

| 问题 | 修复 |
| --- | --- |
| 无效 `--pattern` 在判定时抛 `re.error`，CLI 以退出码 1 崩溃（与 `reproduced` 同码，CI 会误读） | 写入/执行前 `validate_pattern()` 校验；`evaluate()` 再做防御性兜底（`inconclusive`）；预执行错误统一 exit 2 |
| `repro` 无复现定义 / 无失败信号时 exit 3 | 按 §4 契约改为 exit 2（`INCIDENT_NO_REPRO` / `REPRO_SPEC_INVALID`） |
| `repro --timeout` 帮助说「覆盖记录值」，实际被 `reproduce.timeout_s` 覆盖 | 新增 `timeout_override` 参数，显式 `--timeout` 优先于记录值 |
| LLM 失败没有按 §10 重试 1 次 | `analyze_llm` 增加一次重试；仍失败才降级，降级原因标注尝试次数 |
| `record` 无法设置 `wontfix`（§11.4 提到 `record --status`） | 新增 `record --status`；`annotate --status` 复用统一解析 |
| §9.2 说 `agent` 是 harness 别名，代码却把它当 model 别名 | 按文档改为 harness 别名；新增别名回归测试 |
| `cmd /c` 对首位带引号命令二次转义（F-0004） | `spawn` 改为 `shell=True` 原样传参；真实子进程回归测试；F-0004 重放为 fixed |
| bundle 导入丢失 trace → incident 关联；导出顺序不稳定；`updated_at` 按字符串比较 | 导入时按 `incident.traces` 回填 `traces.incident_id`；导出 `ORDER BY`；时间戳按真实时刻比较；坏文档/非数组字段报 `BUNDLE_INVALID`，且先全量校验再落库 |
| report 计数并列时顺序不稳定 | 所有分布/最近列表增加确定性次级排序键 |
| `--backend` 非法值静默按 auto；`--all` + ID、`--pending` + ID 静默取其一 | 统一报 `INVALID_ARGUMENT`；`--all` 逐条预检无效复现定义、单条执行失败不阻断其余条目，并保持 exit 2 |
| 复现日志文件名、`updated_at`、repro run 去重键都是秒级精度 | 日志名与 `now_iso()` 加入微秒；同一秒内的更新/运行可区分 |
| 超时只终止 shell，实际测试子进程可能残留 | 终止整个进程树（Windows `taskkill /F /T`；POSIX `killpg` + 5s 宽限），新增真实子进程回归测试 |
| 转录文件强制按 UTF-8 解码，GBK 输入变乱码 | 复用 `decode_bytes`（UTF-8 → GBK → replace），新增 GBK 用例 |
| taxonomy / detection 数据文件的 schema 或正则错误会在运行中抛裸异常 | 加载与匹配时校验正则/阈值，统一报 `TAXONOMY_INVALID` / `DETECTION_RULES_INVALID` |
| 手动 `record` 的事故没有 trace，离线分析恒为 unclassified | 离线分析用 `incident.failure` 构造最小 trace；仍无匹配才落 unclassified |
| 复现历史/最近事故按字符串排时间，跨时区偏移不可靠 | 按真实时刻（`datetime.fromisoformat`）排序，坏值回退字符串 |
| `parse_jsonl_events` 无意义自赋值 | 删除 |

质量门禁：`ruff check` / `ruff format --check` / `pyright` 全绿；**123 项测试通过**；
`python scripts/verify_mvp.py --skip-tests` 仍为 **12 PASS / 0 SKIP / 0 FAIL**。
F-0004 的真实重放（2026-10-19）：`fixed`（两次重放 15.6s / 17.8s，exit 0）。

其余未改动项：`repro` 仍只在记录的项目目录里执行命令级复现（不做 agent 重放）。仓库已于 2026-10-19 `git init`（`main` 分支）并完成首次提交，`uv.lock` 于发布前收尾时生成（uv 0.12.10），见 §24.7。

### 24.7 发布前收尾（2026-10-19）

以「公开 GitHub 仓库」为目标的仓库卫生、文档与依赖整理（无运行时代码变更）：

| 项 | 处理 |
| --- | --- |
| README 安装方式与命令不一致（只装依赖却直接使用 `faillab` 命令） | 快速开始/开发统一为 `uv pip install -e ".[dev]"` / `uv sync --extra dev`；中文/空格路径保留 `scripts/faillab_dev.py` 替代说明 |
| README 克隆地址为占位符 | 已替换为 `https://github.com/IMUskyworld/AI-Failure-Lab`，README 顶部加入 CI badge |
| 示例与文档暴露本机绝对路径 | `examples/real-sessions/`、`scripts/faillab_dev.py`、§22 中的本机路径全部脱敏为通用路径；报错原文与事件语义不变 |
| 从未使用的运行时依赖 `packaging` | 从 `pyproject.toml` / README / §13 移除（lock 中仍作为 pytest 的传递依赖存在） |
| 验收脚本输出歧义 | 20-5 的「taxonomy 12 类」实为 12 条正例（覆盖 10 类），输出改为「12 条正例（10 类）」 |
| 缺少仓库卫生文件 | 新增 `.gitattributes`（统一 LF）、`CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md`、`examples/real-sessions/README.md` |
| CI 工作流防护 | 增加 `workflow_dispatch`、`permissions: contents: read`、并发取消；安装步骤改为 `uv sync --locked --extra dev`（lock 过期即失败） |
| 依赖解析不可复现 | 生成 `uv.lock`（uv 0.12.10，32 个包），CI 使用锁定版本 |
| 打包从未验证 | `uv build` 产出 sdist + wheel；干净临时环境安装 wheel 跑通 import → list → analyze(offline) → report |

验证（锁定版本，对齐 CI）：临时环境（Python 3.11.9 + pytest 9.1.1 / ruff 0.17.1 / pyright 1.1.414，版本取自 `uv.lock`）复跑全套检查：**123 项测试通过、ruff 全绿、pyright 0 error**；本机 `python scripts/verify_mvp.py --skip-tests` 仍为 **12 PASS / 0 SKIP / 0 FAIL**。

仓库状态：`git init`（`main`）与首次提交已完成（作者 `IMUskyworld <worldwarheroes@126.com>`）；remote `origin` 指向 `https://github.com/IMUskyworld/AI-Failure-Lab.git`。剩余待办（不阻塞代码）：在 GitHub 建空仓库后 `git push -u origin main`，并设置仓库 topics / description。

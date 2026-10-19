# Security Policy

## 支持范围

faillab 是本地 CLI：数据只写入本机 SQLite / HTML / JSON，无遥测、无云端上传。
LLM 是可选后端，API key 只从环境变量读取（`FAILLAB_LLM_API_KEY`），不会写入数据库、报告或日志。

## 需要警惕的行为边界

- `repro` 会在事故记录的项目目录里**执行命令**，可能修改文件。请把事故库与导入的转录视为不可信输入：执行他人提供的事故复现定义前先审查命令。
- `import` 只解析转录文件，不执行其内容；但转录本身可能含敏感文本。
- `export` 的 bundle 与 `report.html` / `report.json` 含事件原文（提示词、代码片段、错误文本），分享前请自行脱敏。

## 报告漏洞

请通过本仓库 GitHub 的 **Security → Report a vulnerability** 私密提交（不要开公开 issue），并附上：

- faillab 版本（`faillab version`）、Python 与操作系统版本；
- 最小复现步骤或已脱敏的转录样本；
- 影响范围与你的判断依据。

常见非漏洞：本机 PowerShell 执行策略拦截 `scripts/check.ps1`（属机器设置，可用 `-ExecutionPolicy Bypass` 缓解，见 `DEVELOPMENT.md` §24.5 F-0001）。

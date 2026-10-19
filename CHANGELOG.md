# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)；格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [0.1.1] - 2026-10-19

以「修 bug + 补契约」为目标的硬化修订（逐条见 `DEVELOPMENT.md` §24.6），以及公开仓库前的收尾（§24.7）：

### Fixed

- 无效 `--pattern` 不再让 CLI 以退出码 1 崩溃：写入/执行前校验，预执行错误统一 exit 2。
- 无复现定义 / 无失败信号时 `repro` 退出码从 3 改为 2（符合 §4 契约）。
- `repro --timeout` 真正覆盖记录值（此前仍被 `reproduce.timeout_s` 覆盖）。
- Windows 复现不再经 `cmd /c` 二次转义首位带引号的命令（F-0004 → fixed）。
- 复现超时改为终止整个进程树（Windows `taskkill /F /T`；POSIX `killpg` + 宽限期）。
- LLM 失败按 §10 重试 1 次后再降级；离线分析可以分类手动 `record` 的事故。
- bundle 导入回填 trace → incident 关联；导出确定性排序；`updated_at` 按真实时刻比较；坏文档报 `BUNDLE_INVALID` 且先全量校验再落库。
- report / 复现历史 / 最近事故按真实时刻排序（跨时区不再错序）。
- taxonomy / detection 数据文件的正则或阈值错误统一报 `TAXONOMY_INVALID` / `DETECTION_RULES_INVALID`。
- 转录解码回退 UTF-8 → GBK → replace；`agent` 别名按 §9.2 归一为 harness。
- `--backend` 非法值、`--all` + ID、`--pending` + ID 统一报 `INVALID_ARGUMENT`；`--all` 单条失败不再阻断整批。
- 时间戳与复现日志文件名提升到微秒，同一秒内的更新/运行可区分。
- `scripts/verify_mvp.py` 20-5 的输出措辞修正：原「taxonomy 12 类」实为 12 条正例（覆盖 10 个类别）。

### Added

- `record --status` / `annotate --status`（含 `wontfix`）统一状态校验。
- 真实子进程回归测试：带引号命令、进程树终止、`--all` 跳过坏记录。

### Changed

- 版本号 0.1.0 → 0.1.1；README / DEVELOPMENT 同步。
- 移除从未使用的运行时依赖 `packaging`。
- 新增 `uv.lock`（CI 与本地安装使用锁定版本）。
- 仓库卫生：`.gitattributes`（统一 LF）、`CONTRIBUTING.md`、`SECURITY.md`、`examples/real-sessions/README.md`；示例中的本机路径已脱敏。

## [0.1.0] - 2026-09-22

- 首个可用版本：导入（jsonl-events / json-object 别名）→ 失败检测 → 签名去重 → 分类/根因（LLM 或离线）→ 复现三态 → HTML/JSON 报告 → bundle 导出/导入。
- `python scripts/verify_mvp.py` 覆盖 `DEVELOPMENT.md` §20 全部 12 条验收标准。

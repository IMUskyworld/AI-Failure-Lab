# ADR 0003：SQLite 单库 + JSON bundle 通道

- 状态：已采纳（2026-09-22）
- 背景：事故库需要过滤、计数、历史追加（复现运行）等查询能力；同时事故记录天然希望可分享、可进 git。纯 JSON 文件难以做过滤与并发历史；纯数据库又不便 diff/分享。
- 决策：
  1. **SQLite（stdlib sqlite3）是唯一事实来源**：incidents / traces / repro_runs / meta 四张表，关键过滤器（status/category/model/harness/repro/signature）落成列并建索引，文档整体存 JSON 列；
  2. 检索用 LIKE（不做 FTS5）：千级规模足够，避免不同 sqlite 编译选项的行为差异；
  3. `export` / `import-bundle` 提供 JSON bundle 通道：导出全量、合并导入（新文档新增、更新文档按 updated_at 合并、重复跳过）；
  4. schema_version 写入 meta，版本不匹配直接拒绝（不猜）。
- 理由：零外部依赖（stdlib）+ 查询能力 + 可携带通道三者兼得；与 LabBox 的「run 目录只增不改」哲学一致（repro_runs 只追加）。
- 代价：数据库文件本身不便 diff；靠 bundle 通道弥补（需要 git 友好时导出即可）。

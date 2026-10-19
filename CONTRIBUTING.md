# Contributing

欢迎提交 issue / PR。这个项目优先保证三件事：**可复现性、诚实判定、本地优先**。

## 开发环境

```powershell
uv venv --python 3.11
uv pip install -e ".[dev]"      # 或：uv sync --extra dev
.venv\Scripts\activate
```

中文/空格路径下 editable install 可能失败（见 `DEVELOPMENT.md` §22）；这类环境直接用源码运行：
`python scripts/faillab_dev.py <命令>`，测试用 `python -m pytest -q`（pytest 经 `pyproject.toml` 的 `pythonpath` 读取 `src/`，无需安装）。

## 质量闸门（提交前必须全绿）

```powershell
python -m pytest -q
python -m ruff check src tests scripts
python -m ruff format --check src
python -m pyright                                    # 只检查库代码（src）
python scripts/verify_mvp.py --skip-tests            # DEVELOPMENT.md §20 逐条核验
```

或一次跑完（Windows）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1
```

## PR 约定

- **修 bug 必须带回归测试**：优先用真实路径复现原问题（真实子进程、本地假 HTTP 服务器），而不是只测 mock。
- **不改退出码契约**：`0` fixed / `1` reproduced / `2` 未执行或无法判定 / `3` 内部错误 / `130` 中断；改变行为必须同步 README 与 DEVELOPMENT.md。
- **不装懂**：判定不确定时 `inconclusive`；任何降级/回退都要在输出与记录里给出原因。
- **不提交本地数据**：`.faillab/`、真实转录、`export` bundle 不要进 PR；bundle 含事件原文，确需作为示例时先脱敏。
- **文档同步**：行为、配置、依赖变化同步更新 README / DEVELOPMENT.md / CHANGELOG.md；测试数量变化更新 DEVELOPMENT.md §24。
- **提交信息**：一句话说清「改了什么、为什么」。

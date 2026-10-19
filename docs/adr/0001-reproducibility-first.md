# ADR 0001：可复现性优先——不做「又一个 failure memory」

- 状态：已采纳（2026-09-22，用户选定）
- 背景：failure-memory / replay / flight-recorder 类工具已经很多，纯记录「发生过什么」没有差异化；而事故记录最大的价值缺口是「这条失败现在还在吗？修好了吗？」——没有任何同类工具用真实重放回答这个。
- 决策：
  1. 每条事故的复现定义 = 可执行命令 + 失败信号（输出正则 / 非零退出），二者至少其一；
  2. `repro` 重放命令并作**确定性三态判定**：全中→`reproduced`（失败仍在）、全不中且正常结束→`fixed`（已修复）、混合/超时/启动失败→`inconclusive`（不装懂）；
  3. 判定只看退出码与输出正则，**不依赖任何模型**；`incident.status` 随真实重放结果演进（open → reproduced → fixed）；
  4. 没有复现定义的事故显式标记「未验证」（`unverified`），而不是假装可信。
- 理由：这正是分析文本里「Reproduction: 100% reproducible」那一行的落点；也让 `repro --all` 的退出码成为 CI 的「已知失败集」回归哨兵（0=fixed / 1=reproduced / 2=inconclusive）。
- 代价：事故录入成本变高（要给出命令与信号）——用 import 的 `--cmd` 与 `annotate` 命令降低门槛，未验证记录仍允许存在（状态诚实）。

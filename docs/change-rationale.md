# 个人改造的提交依据与验证记录

适用范围：从上游基线 `db184390e7b23e052c68ef3b04022dc5befec9c1` 开始的个人改造。上游来源为 [mohamed-benoughidene/openreview-cli](https://github.com/mohamed-benoughidene/openreview-cli)，个人 fork 为 [lxy020512/openreview-cli](https://github.com/lxy020512/openreview-cli)。保留上游署名、许可和历史，不把上游既有功能计为个人实现。

最后更新：2026-10-01。严格提示词修复已独立审查，E 盘修复后 78 新用例通过，公开 NDA 的真实 smoke、同进程复用与新进程恢复通过。每个提交的 pre-commit 结果记入提交说明；实际提交归属按下表或 git log 核验，推送状态以远端分支/PR 为准，不制造持续更新历史。

## 每一次提交需要说明什么

1. **问题与依据**：具体触发条件、当前行为，以及代码位置、有效失败测试或官方资料。功能想法不是已观察到的故障。
2. **设计与取舍**：修改后的行为、为何采用这条边界、付出的代价。关联批准规格或设计记录。
3. **验证**：实际运行的命令、环境、结果和范围。区分新增用例、局部回归、全仓库检查、mock 和真实 API；不把收集测试当执行。
4. **限制**：未验证项、已知失败、剩余风险；记录失败原因，不通过放宽断言或配置制造全绿。
5. **来源与归属**：上游代码保持署名；引用外部协议或能力变化的官方来源；不记录密钥、原始合同、PII 映射或原始供应商错误。

GitHub 按三个完整功能节点提交：Windows 复现前置修复、DeepSeek adapter、检查点功能及其严格提示词修复。日常修改先在本地积累，功能完成、验证和独立审查通过后，再形成可解释的提交与推送；讲解文档归入对应节点，每个节点包含相应测试与依据，不能留下 broken imports 或被提前调用的未完成模块。不要为“持续更新”拆成无意义提交、补写历史日期或编造故障。

GitHub 推送以前先检查 diff、秘密与许可，完成仓库要求的 pre-commit；预提交 hook 安装或运行受阻时记录真实原因，不声称通过。提交说明用文件传给 `git commit -F`，避免 shell 转义破坏多行内容。推送后的 hash、分支/PR 链接与验证结果再填入账本。

## 提交说明模板

```text
feat(review): resume validated clause steps after interruption

Why:
- 哪个真实处理场景需要改动；改动后的行为是什么。
- Design: specs/036-clause-checkpoints/spec.md

Evidence:
- 原行为的代码位置 / 首个有效 RED 用例 / 官方能力资料。
- 上游基线与个人新增部分的边界。

Validation:
- 实际命令、运行环境、通过/失败数量与结果范围。
- mock / offline / live，是否真的发送模型请求。

Limits:
- 尚未验证的行为、保留的风险和明确不支持的场景。
- Rationale: docs/engineering-learning-guide.md
```

字段可以简写，但不能省掉关键证据。问题、实现和验证保持对应；只有真实修复才写 `fix:`。文档提交也需注明哪些事实来自既有 receipt、哪些仍是计划，不需要为纯文字改动编写镜像测试。

## 变更账本

| 主题 | 修改依据与设计 | 实际验证 / 当前限制 | 提交状态 |
| --- | --- | --- | --- |
| Windows 测试前置修复 | 两个原有模块导入调用 POSIX `os.geteuid()`，阻塞 Windows collection/mypy。测试加窄幅 POSIX guard，保留非 root Linux 路径和断言，不改工具配置。环境安装与 PII 历史基线另见对应文档，不计为新产品能力。 | [Windows 任务](../specs/036-clause-checkpoints/windows-test-task.md)：8 AST/19 collect/mypy667；runtime9 passed/8 skipped/1 memory deselected/1 未修改 Linux 字符串断言失败。提交前 E 盘全部10个pre-commit hook通过。不是全仓库运行或 ACL 验证。 | 本地提交 [96fe67d](https://github.com/lxy020512/openreview-cli/commit/96fe67d)；推送状态以远端分支/PR为准 |
| DeepSeek adapter 与本机配置 | 官方当前模型/能力与旧 registry 不同；原 setup 无 DeepSeek；JSON object/array 约束不同。窄范围配置选中 slot、清除旧 fallback、隐藏输入 key、保留隐私门禁。 | [适配说明](deepseek-setup.md)：17新用例与registry/models共72 passed；更大集合84 passed/2 skipped/6 failed原因已记。本机配置、`/models` HTTP200；模板修复后的真实extraction+QA响应合法。样例账单、准确率和生产可靠性未验证。 | 本地提交 [073879d](https://github.com/lxy020512/openreview-cli/commit/073879d)；推送状态以远端分支/PR为准 |
| 检查点恢复与严格提示词对齐 | 两表事务、白名单加密、保守身份、QA快照与同一费用session；修复非BLOB payload及共享清单遗漏。真实诊断发现prompt允许no-match、严格Position拒绝；仅严格模板从枚举生成合法值，保留validator与非resume输出。 | E修复后78新用例/12文件8.33秒；独立严格36 passed/旧相关21 passed，24模式288非严格输出与修复前HEAD字节一致；worker117回归通过。mock [receipt](benchmarks/checkpoints/clause-checkpoints-offline.json)验证故障恢复；[真实 receipt](benchmarks/checkpoints/clause-checkpoints-deepseek-smoke.json)首次2 SDK调度、同进程复用0；[新进程恢复](benchmarks/checkpoints/clause-checkpoints-fresh-process.json)guard下0尝试，均5/0且归一化相同。仅1类别候选、4no-match；无准确率/账单结论。 | 本文所属检查点功能提交，可用 git log 核验，避免额外自引用提交 |

实际文档回归发现：上游 receipt guard 要求 `docs/benchmarks/results/` 精确包含十个既有 JSON，新增离线记录放在该目录会使测试失败。将 fork 三份记录放到专门的 `docs/benchmarks/checkpoints/` 并修正引用，保留原记录字节、上游十份集合与测试配置不变。C 草稿目录集合用例复验 1 passed（0.37秒）；没有通过放宽预期集合掩盖失败。该路径修复归入检查点功能节点。

DeepSeek 提交前的 E 盘检查记录：43 个实际变更文件的适用 hook 检查通过，随后 `pre-commit run --all-files` 的全部 10 个 hook 通过；mypy 检查 668 文件通过，pytest hook 收集 2936 用例（3.57 秒），仅 collection，不是全套运行。对这些变更文件精确扫描本次实际配置的 key 未命中，没有 auth 或检查点密钥被跟踪；这限定为本次检查范围。检查点功能提交的最终 hook 结果按其提交说明核验，不由此前 DeepSeek 提交的检查替代。

## 收尾时怎样更新账本

每个实际提交后替换相应状态为真实短 hash，并链接该提交；记录当时准确的验证范围。若主题合并在一个提交中，明确共享 hash，不伪造多个提交。若独立审查要求修复，在后续提交写明问题、复现、修复与回归，保留最初验证的边界。

检查点基准运行并核验后，只填实际调用计数及配置，例如 grounding 是否关闭；真实模型运行后再增加 provider/model、样本、token/usage、账单和人工标注。敏感数据与运行产物继续放忽略目录，仓库文档仅保留安全计数和可复现方法。

环境历史不能覆盖当前状态：初始 Windows 记录的“模型未安装”属于当时状态；后来 `pii-baseline.md` 已验证安装和 Parse + Strip。类似地，本文“待验证”在收尾后应由实际结果更新，不保留互相矛盾的现状声明。

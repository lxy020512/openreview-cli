# OpenReview 改造：工程设计与面试追问

最后更新：2026-10-01。本文把代码、验证和解释对应起来，避免把设计目标写成已经取得的效果。严格提示词修复已独立审查；E 盘修复后 78 新用例通过，公开 NDA 的真实 smoke、同进程复用与新进程恢复通过。当前成果仍是小样例工作流验证，不是准确率、账单或生产结论；每个提交的 pre-commit 结果记入提交说明。

## 先说清楚自己的贡献

项目基于 [mohamed-benoughidene/openreview-cli](https://github.com/mohamed-benoughidene/openreview-cli)，固定基线为 `db184390e7b23e052c68ef3b04022dc5befec9c1`，保留上游署名和 AGPL-3.0 许可。文档解析、Presidio 脱敏、AI Gateway、playbook 和原有审查流水线来自上游。

本次改造聚焦两个方向：同一文件的条款/步骤检查点，以及 DeepSeek 云模型接入与本机可复现环境。不要把上游完整平台说成独立开发，也不要把本地测试说成企业生产部署。

真实痛点是：长文档处理到一半被中断，已经成功的抽取和 QA 仍可能在重跑时再次请求模型；失败结果、过期配置或未经保护的缓存又可能造成错误复用和信息泄露。企业价值是减少可避免的重复请求，并让中断恢复、隐私边界和费用归属能够解释和验证。真实节省金额和审查质量仍需云端测试。

## 当前有哪些证据

| 事项 | 当前结论 | 证据与边界 |
| --- | --- | --- |
| 固定上游复现 | 已验证 CLI、解析和局部回归 | [Windows 基线](windows-baseline-environment.md)：1 页、5 条款；116 个原有测试通过。这不是全仓库回归。 |
| 本地 Parse + Strip | 已验证公开英文 PDF | [PII 基线](pii-baseline.md)：检测到 7 个实体，冷启动总耗时 55.120 秒；不是 PII 召回率或完整审查延迟。该记录更新了 Windows 初始环境记录中“PII 模型未安装”的历史状态。 |
| DeepSeek 适配 | 离线验证通过 | [配置与验证](deepseek-setup.md)：17 个新增用例，加原有 registry/models 回归共 72 passed；更大的 auth/wizard 集合存在已记录的 Windows/权限失败。 |
| 检查点 | 离线集成独立审查通过 | 9 文件中 30 个新增用例通过（11.86 秒），66 个原有核心回归通过；[使用与故障实验](checkpoints.md)与[实际 receipt](benchmarks/checkpoints/clause-checkpoints-offline.json)。不是全仓库或真实云审查通过。 |
| 静态与局部回归 | 修复后 E 工作树 78 新用例通过 | 12 文件/8.33 秒；模板独立审查36 passed，原有相关21 passed；此前 mypy667与预提交通过，逐次 hook结果记入提交说明。pytest hook只收集测试。Windows 保留失败见[任务记录](../specs/036-clause-checkpoints/windows-test-task.md)。 |
| 云端接入与复用 | 严格模板修复后 smoke 通过 | `/models` HTTP200；[真实 receipt](benchmarks/checkpoints/clause-checkpoints-deepseek-smoke.json)：首次2 SDK调度、5 assessment/0 failed；同进程repeat0调度。[新进程恢复](benchmarks/checkpoints/clause-checkpoints-fresh-process.json)在SDK guard下0尝试，结果同样相同。 |
| 云端质量、费用、生产运行 | 尚未验证 | 有真实 usage，不等于实际账单或质量评测；不可填写准确率、金额节省、生产 SLA。 |

## 流程为什么这样拆

本次 NDA 路径可读作：

```text
输入 PDF/DOCX
  → Parse：解析并划分条款
  → Strip：本地识别 PII，生成占位符
  → Review：匹配 playbook 类别
      → extraction：抽取立场、置信度、引用
      → QA：检查抽取结果，必要时给出修订
  → 可选 grounding：核查引用依据
  → 报告与导出

--resume 只在 extraction / QA 周围读写检查点。
解析、脱敏、grounding、报告和导出仍会重跑。
```

playbook 是领域规则集合，规定类别以及 preferred / acceptable / walkaway 等立场；slot 是用途标签，例如 extraction、reasoning，不等于实际执行的供应商模型名。Gateway 决定路由、隐私门禁、候选模型和费用记录。

这条流程适合用显式阶段表示：输入、结果和失败边界清楚，便于恢复。没有为了“Agent”标签增加自由规划循环或分布式服务。QA 是另一轮模型判断，不能保证消除幻觉；同一供应商和相近提示词可能产生相关错误。需要人工标注和独立评测才能证明质量提高。

源码入口：[`review/runner.py`](../src/openreview_cli/review/runner.py)、[`review/pipeline.py`](../src/openreview_cli/review/pipeline.py)、[`pipeline/adapters/strip.py`](../src/openreview_cli/pipeline/adapters/strip.py)。

## 1. 恢复为什么做到“条款 + 步骤”

**痛点。** 只保存最终报告，最后一条失败就无法利用之前的工作；只按整条条款缓存，QA 失败也可能重复已成功的 extraction。

**设计。** 一次 run 对应文件与有效配置的身份，每条 clause 分开记录 extraction 和 QA。只有 completed 且通过解密、字段和身份校验的结果可复用。running、failed 或坏密文都是未完成工作；合法的 uncertain 是模型判断，不能与调用失败混为一谈。无匹配类别走确定性路径，不调用模型。

**问题与实现。** 上游 `verify_assessment()` 会修改传入对象。如果把同一个对象当 extraction 快照再交给 QA，QA 修订可能污染快照。当前 `ReviewStage` 先保存 extraction，再用深拷贝交给 QA。`test_qa_interruption_reuses_extraction_snapshot` 检查 QA 中断后只重试 QA，且 extraction 快照没有 QA 修订字段；该测试已包含在独立审查通过的 30 个新增用例中。

**取舍。** 多了一些数据库读写和状态，但能明确回答“从哪一步继续”。它是本地成功结果缓存与恢复机制，没有实现跨合同、跨版本重用。

**面试回答。** “我把可重用的单位设为条款的抽取和 QA。QA 失败后保留抽取成功结果。复用前检查身份和结果 schema；失败与合法 uncertain 分开，避免把异常永久缓存为正常判断。”

源码与测试：[`review/pipeline.py`](../src/openreview_cli/review/pipeline.py)、[`review/checkpoints.py`](../src/openreview_cli/review/checkpoints.py)、[`test_review_stage_checkpoints.py`](../tests/unit/test_review_stage_checkpoints.py)。

## 2. 缓存身份为什么不能只有文件 hash

**痛点。** 文件不变，playbook、隐私设置、提示词、模型端点或 fallback 改了，旧结果仍可能不适用。PromptStore 可以从数据库解析实际提示词，只对 Python 文件做 hash 会漏掉这类变化；对整个 SQLite 文件做 hash 则会因费用、检查点等无关写入持续失效。

**设计。** 身份绑定文件字节、规范化路径、完整 playbook、相关设置、实际解析后的提示词、候选模型及有效调用参数，以及一次启动时的实现/依赖清单。含凭据的快照仅在内存参与带密钥的 HMAC，存储摘要，不持久化凭据正文。调用前和保存前重新检查动态输入，发生漂移则停止该 run。

**实际发现的问题。** 普通 `Gateway` 构造会把 auth.json 中的凭据填到原本缺失的环境变量。初版快照直接比较环境变量的存在性：调用前为缺失，调用后已有值，会把内部初始化误认为配置修改。修正方式是比较“按真实解析优先级得到的有效值”，即环境覆盖或 auth 中的默认值；用户改变有效覆盖值仍应使身份变化。回归用例保留普通 Gateway 构造与环境初始化，替换 completion，并隔离配置、门禁与费用检查；没有把整个 `call_gateway_chat` mock 掉。该问题已记录 RED → GREEN 并通过独立复验，证明环境初始化后身份稳定，不是完整门禁或费用集成测试。

独立审查还发现首版实现清单漏掉共享的 `llm_json.py`、`slots.py` 和 pipeline runner/errors：共享行为变更可能没有使身份失效。先用修改共享文件后身份仍不变的测试复现，再补固定清单；已独立复验。清单覆盖影响此流程的共享实现，仍保持一次启动快照，不扩展为全仓库热更新系统。

**取舍。** 保守失效可能降低命中率；路径也绑定，所以同字节文件搬位置仍重新计算。服务商把相同远端别名指向新模型无法在本地可靠检测，不能声称结果永久有效。存储字段中的 slot 标签也不能冒充经过 fallback 后的实际模型溯源。

**面试追问。** 为什么不只比较模型名？因为端点、凭据权限、参数、fallback 和提示词也会影响实际调用。为什么用 HMAC？因为身份要绑定秘密状态，但不能把凭据或可直接离线猜测的凭据摘要放入数据库。HMAC 也不能替代操作系统的密钥保护。

源码与测试：[`runtime_snapshot / build_identity`](../src/openreview_cli/review/checkpoints.py)、[`Gateway`](../src/openreview_cli/gateway/router.py)、[`PromptStore`](../src/openreview_cli/prompts/store.py)、[`test_checkpoint_identity.py`](../tests/unit/test_checkpoint_identity.py)。

## 3. SQLite 事务能保证什么，不能保证什么

**设计。** 新增 runs 和 steps 两表，使用唯一约束与事务保存状态和密文；主键为 run、clause、step。completed 必须有 payload，running/failed 不得有 payload。删除 run 级联删除新 steps，保留其他模块数据。强制重算清除该身份的 steps，保留 run 的 cost session。

```text
running → 外部请求 → 收到并校验结果 → 事务提交 completed
                         ↑
                 在这里崩溃，结果尚未落盘
```

**边界。** SQLite 事务只覆盖本地写入，不覆盖远端模型请求。回复后、提交前崩溃，下一次无法证明远端请求已经完成，因此可能重复调用并重复收费。唯一主键只防止重复本地记录，不等于远端 exactly-once。要进一步处理，需要服务商支持请求幂等标识或结果查询；本次没有这种保证。

**并发取舍。** SQLite 满足本地前台 CLI 的持久化需求。本次没有多进程任务认领、租约或分布式锁；两个进程竞争同一 run 的处理不在支持范围。不能把它描述成分布式任务调度平台。

**实际审查发现。** SQLite 列声明为 BLOB 并不阻止通过 SQL 写入 TEXT 或 INTEGER。初版读取执行 `bytes(payload)`：TEXT 会产生 TypeError，INTEGER 会被当作分配长度，存在额外内存风险。复现使用真正 SQL 修改 payload，随后增加 bytes 类型检查，非法单条 payload 返回 miss；已记录该 storage 局部 RED → GREEN。不要把声明的数据库类型当成应用层校验，也不需要为这类问题增加迁移框架。

**面试回答。** “我保证已提交且有效的步骤可恢复，承认回复到提交间存在重复请求窗口。幂等写入、缓存命中和外部副作用恰好执行一次是三个不同问题。”

源码与测试：[`storage/checkpoints.py`](../src/openreview_cli/storage/checkpoints.py)、[`016_review_checkpoints.sql`](../src/openreview_cli/storage/migrations/016_review_checkpoints.sql)、[`test_checkpoint_storage.py`](../tests/unit/test_checkpoint_storage.py)。

## 4. 脱敏之后为什么还要加密和收紧错误输出

**痛点。** 脱敏降低发送给云端的敏感信息，但不保证全部实体被识别；citation、QA rationale 等结果仍可能含敏感内容。供应商异常也可能带请求文本，简单 `logger.warning(str(exc))` 会泄露到日志。

**设计。** 新检查点采用显式字段白名单和 Fernet 认证加密。白名单不保存 clause_text、请求、原始响应和异常正文；恢复时把本次解析/脱敏后的文本只在内存注入。检查类型、枚举、有限置信度、所属条款/类别/slot；`True` 不可被当作数字置信度。加密保护剩余允许字段，认证检查篡改。严格分支使用固定错误类别，不输出供应商异常正文。

**密钥依据。** 上游 `config/loader.py` 中 PII 默认密钥是公开固定值，不能用来保护新检查点。新功能单独生成随机密钥，放用户配置目录，独占创建，坏的已有密钥不会被覆盖。没有修改或认证整个上游 PII 持久化方案。

**取舍与失败策略。** 单条坏密文可以安全重算；密钥不可用、数据库不可用或写失败应停止 resume，避免悄悄降级为无保障的恢复。Windows 的 POSIX chmod 不等于 ACL，不能抵抗同用户进程或管理员访问。`checkpoints-clear` 是删除当前文件字节 hash 对应的新记录，保留历史版本及其他数据；不是磁盘安全擦除。

源码与测试：[`CheckpointCodec / load_checkpoint_key`](../src/openreview_cli/review/checkpoints.py)、[`extraction.py`](../src/openreview_cli/review/extraction.py)、[`qa.py`](../src/openreview_cli/review/qa.py)、[`test_review_checkpoints.py`](../tests/unit/test_review_checkpoints.py)、[`test_checkpoint_strict.py`](../tests/unit/test_checkpoint_strict.py)。

## 5. 为什么安全错误不能沿用普通重试

**源码观察。** 上游通用 Pipeline 会捕获异常，非关键阶段可能交给恢复流程重试；review runner 也有把异常转成日志并继续下一文件的分支。新增 `CheckpointError` 若只是普通 Exception，会被这些边界吞掉。

**设计与验证。** 新检查点的安全错误在 Pipeline、单文档 runner、多文档 runner 到 CLI 各层显式传播，以固定消息和非零退出停止后续文件。仅在 extraction/QA 增加校验还不足以实现这一点。runner/CLI 的故障注入测试已包括在通过独立审查的新增用例中；这证明测试覆盖的传播路径，不能扩展为所有外部异常都已验证。

**实际集成问题。** 最初让通用 Pipeline 从 `review.checkpoints` 导入错误类型，触发 review 包初始化再导入 runner 的循环依赖。错误类型已移到不依赖 review 的 `pipeline/errors.py`，两侧导入同一个定义。解决依据是减少基础层对业务层的依赖，不是用宽泛异常捕获掩盖导入错误；runner/CLI 路径已通过独立审查。

**取舍。** 普通供应商失败可记录为 failed 等待后续重算；持久化和身份安全失败不能靠重复整个阶段掩盖。也不吞掉 KeyboardInterrupt、SystemExit 或取消信号。

**面试追问。** 你如何证明异常没有被吞？在真正的 runner/CLI 边界注入故障，检查退出码、后续文件未处理、没有额外调用或持久化，不能只测 `raise CheckpointError` 的单元函数。

源码依据：[`pipeline/runner.py`](../src/openreview_cli/pipeline/runner.py)、[`review/runner.py`](../src/openreview_cli/review/runner.py)。

## 6. 恢复以后费用为什么仍属于同一 session

session 是同一文件这次审查的费用归属标签。重跑若重新生成标签，会把已经发生的费用拆散，预算检查也可能低估累计消耗。检查点 run 保存并复用 session；extraction、QA 和可选 grounding 使用同一个标签。命中检查点没有发生请求，所以不能伪造 cost log；force-review 保留旧 session，重算仍计入原预算。

需要区分调用次数、估计费用和平台实际账单。当前别名可能未被本地价格表收录，账本还存在整美分粒度，零估价不代表免费。没有真实调用前，只能报告故障注入测试中的请求计数。

## 7. DeepSeek 接入为什么要检查协议细节

本次不是只替换字符串：旧 registry 上下文门禁可能先于 API 拒绝请求；setup 向导没有 DeepSeek 选项；extraction/QA 期待 JSON object，grounding 期待 JSON array。专用配置脚本仅调整所选 slot，保留其他配置；清除选中 slot 的旧 fallback，避免实际请求意外流向其他服务商。隐藏输入 key，使用已有本机 auth 存储，不把秘密放入命令或 Git。

已核验当日官方模型和 JSON 模式文档，并用安装的 LiteLLM 验证 provider、URL 与参数映射。当前 thinking 行为和空 JSON 内容仍需真实 API 检查；“离线参数正确”不能推出“账号可用或质量合格”。依据与完整命令在 [DeepSeek 配置文档](deepseek-setup.md)。

**真实调用发现的契约错误。** 首次公开 NDA smoke 到达模型服务：1 次 extraction，SDK usage 为 prompt 445、completion 1278、total 1723，finish reason 为 stop；5 个 assessment 中有 1 个 `extraction_invalid_response`，虽然 CLI exit 0。没有继续做缓存收益对比，因为部分结果失败不能当基准成功。

首个原始响应没有保留，不能宣称已确认首个回复的具体枚举。随后一次有界诊断只输出 JSON 结构和枚举，确认返回 `position=no-match`、严格校验为 false。源码模板允许 no-match，而 `Position` 只接受立场枚举，属于我们请求契约与校验不一致。修复只对严格路径从 Position 枚举生成允许值，在类别不符或证据不足时明确要求 uncertain，保留非 resume 兼容，不放宽 validator 来吞掉未知值。独立审查36用例通过；24模式/288变化输入的非严格输出与修复前 HEAD 字节一致。修复后真实重跑也已通过。

**面试回答。** “我把鉴权成功、接口返回、schema 合法和领域结果正确分开验证。模型返回了 JSON 不等于满足业务 schema；退出码 0 也不等于每条成功。先看固定错误和安全结构诊断，再核对提示词与校验源码，在真实数据外用回归固定契约。”真实 usage 只能说明服务返回了 token 计数，不等于真实账单、质量或节省金额。

该公开样例虽有 5 个解析条款，但关键词类别匹配只选中 1 条候选，另外 4 条确定性 no-match 跳过云端。它测试的是接入和候选步骤行为，没有证明完整合同法律覆盖、类别识别召回率或准确率。后续评测要扩大标注样本并分别测类别匹配与模型判断，避免用很少的云调用误称全合同已审查。

修复后的[真实 receipt](benchmarks/checkpoints/clause-checkpoints-deepseek-smoke.json) 记录同进程两次调用：首次 extraction+QA 共2次SDK调度，5 assessment/0 failed，16.921秒；重复 resume 为0调度、5/0，1.581秒，归一化结果相同。两个响应 schema 合法且 finish stop；这是一次同进程样例，解析/PII仍重跑，不能推广为一般加速比或冷启动性能。SDK usage 不等于底层HTTP重试计数或实际账单。

再用独立 Python 进程验证持久化恢复，模型 SDK 入口 guard 在任何请求前阻止调用。[新进程记录](benchmarks/checkpoints/clause-checkpoints-fresh-process.json)为0尝试、5/0、归一化结果相同，说明该磁盘检查点可跨进程恢复，避免把进程内缓存误当持久化。9.071秒是该CliRunner调用时间，不含完整Python导入；没有额外模型请求，不是整体网络审计或通用性能评测。

## 数据应该怎么讲

[离线故障 receipt](benchmarks/checkpoints/clause-checkpoints-offline.json) 已记录三条合成条款的实际测量，且实现与故障测试通过独立审查：完整执行 3 次 extraction + 3 次 QA；第二条 QA 请求前中断已经发生 2 + 1 次，恢复只补 1 + 2 次；全命中 0 次；force 为 6 次且保留 session。回复后、提交前中断的两次运行合计 7 次，说明可能重复请求；QA 超时后仅重试 1 次 QA。恢复的归一化结果与基准相同。**这些是 mock 接缝调度计数，不是实际云端请求、准确率或节省金额。** 实验执行真实 ReviewStage/存储，但固定 runtime 并替换模型调用；排除解析、PII 和 grounding。完整流程的 0 次模型调用不能由此推出。

后续真实评估至少记录：样本来源与数量、固定 playbook/提示词/模型配置、请求次数、冷/热启动耗时、故障注入点、人工标注依据、平台 usage 和账单。先做小规模合成 NDA smoke，再扩大标注样本。平均耗时不能代替尾延迟，单个样例不能证明通用准确率；模型置信度也不是经过校准的错误概率。

## Windows 复现为什么也要说明测试边界

原有两个测试模块在导入时直接调用 POSIX 专用 `os.geteuid()`，Windows 收集测试与 strict mypy 因此失败。窄幅修复使用 `getattr` 判断，并仅跳过依赖 POSIX 目录 chmod 的用例；保留非 root Linux 路径与原断言，没有放宽 pytest/mypy 配置，也没有实现 Windows ACL 测试。

独立审查检查了 8 个 AST 条件场景；两个模块成功收集 19 个用例，mypy 667 文件通过。运行结果是 9 passed、8 skipped、1 memory deselected、1 failed。保留的失败是未修改用例假定 Linux `Is a directory` 错误文本，Windows 实际为 permission error。Linux 断言的结构得到保留，但没有在 Linux 执行这些运行测试。依据见 [Windows 测试任务](../specs/036-clause-checkpoints/windows-test-task.md)。这类平台差异应解释清楚，不能将 collection 成功冒充全部运行通过。

## 复习时用代码验证，而不是背术语

1. 从 `ReviewStage._review_checkpointed` 解释“QA 失败后为什么 extraction 没有重跑”。
2. 修改 playbook、数据库提示词、fallback 端点、环境凭据，分别预测身份会否变化，并对照 identity 测试。
3. 在远端回复前与本地提交前中断，说明两次恢复的调用差异。
4. 给返回 JSON 放入 `confidence=true`、未知枚举或缺失字段，解释严格分支为何拒绝缓存。
5. 分别破坏单条密文、密钥和数据库，解释何时重算、何时终止，以及为何不能统一重试。
6. 说明本次贡献、上游贡献和待验证部分；具体成果只引用实际记录。

每次改动的依据和提交格式见 [change-rationale.md](change-rationale.md)。基准、测试和云端验证完成后应同步更新本文状态，保留失败和限制，避免留下过时的成功声明。

## CI 的 schema 与基准 pin 失败怎样定位

**观察到的遗漏。** [首次 CI](https://github.com/lxy020512/openreview-cli/actions/runs/36747711786) 中 lint、types、memory、integration、tui 通过，但 test 有 3082 passed/2 failed/5 skipped/3 xfailed/6 deselected，chaos 有 91 passed/11 failed/1 deselected。检查点 schema 的公共声明和 chaos oracle 没同步；历史 review-accuracy receipt 的两个生产模块 pin 也过期。前者已对齐声明/oracle，独立局部检查 47 passed（2.13 秒）；局部成功不能替代下一轮 CI。

**定位与修复。** receipt guard 按文件原始字节核验，Windows CRLF 与 Git/CI 的 LF 会产生额外 hash 失败。因此在 C 盘另建 LF validation worktree，确认十份 receipt 的 13 个 provenance 条目涉及的 10 个唯一文件均与 HEAD Git blob 一致（3 个路径重复出现），原 guard 的有效 RED 是 32 passed/1 failed（1.96 秒），只报 extraction/QA 两项旧 pin；E 盘源码换行与 runtime 配置不变。用 binary git-show 在内存加载上游基线模块，独立绑定基线 prompts/Gateway，不读取 auth、不发网络请求；21 extraction/67 QA 合成场景、省略 strict/显式 False、288 默认 prompt 案例与成功 Gateway 接缝的受测结果相同。新增固定回归保存该行为，阈值临时内存副本的负向控制能够触发失败。

**数据边界。** 刷新两个 pin 只恢复当前源码 drift 检查，不补造历史 producer commit 或模型，不重新发布准确率。原 2026-09-21 的日期、12 条样本、24 次历史 API 调用和全部指标保持上游历史意义；当前 fork 准确率、费用、延迟和 strict/resume 性能仍不能从这张表推出。guard、生产代码和工具配置保持原有约束。本节记录修复提交时的证据；第二轮结论以 PR checks 和实际 run 为准，不预先宣称全绿；本轮集中修复的提交用 `git log -- docs/engineering-learning-guide.md` 查询。

**面试回答。** “我先确认失败是 schema 契约遗漏、源码 drift 还是平台换行，再保留有效 RED。兼容性对照只能支持受测默认成功路径，不等于重新测过模型质量；hash 能检测变化，也不能证明未知的历史生产版本。”

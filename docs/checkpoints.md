# 条款与步骤检查点

最后更新：2026-10-01。严格提示词修复通过独立审查；E 盘修复后 78 个新增用例通过。公开 NDA 的真实 DeepSeek smoke、同进程复用和新进程持久化恢复通过，记录见下文；不代表审查准确率、完整法律覆盖或生产可用性。每个提交的 pre-commit 结果记入提交说明。批准边界见 [spec 036](../specs/036-clause-checkpoints/spec.md)。

## 使用行为

`openreview precheck review INPUT --resume` 对同一文件、同一有效配置保存并复用成功的 extraction 和 QA。默认不加 `--resume` 时沿用原审查流程，不创建新的检查点密钥和结果缓存。

| 操作 | 行为 |
| --- | --- |
| 第一次 `--resume` | 每个完成并通过校验的步骤写入加密检查点。 |
| 相同输入再次 `--resume` | 优先恢复有效 QA；缺少 QA 时恢复 extraction，仅运行尚未完成的步骤。 |
| QA 调用失败 | 保留成功 extraction，下一次只重试该 QA；失败不当成功缓存。 |
| 合法 uncertain | 是可缓存的判断结果；与调用失败、坏 JSON 分开。 |
| 无匹配 playbook 类别 | 确定性 no-match，不请求 extraction 或 QA。 |
| `--resume --force-review` | 清除当前身份的步骤并重新计算，保留原 run 和费用 session。 |
| 单独 `--force-review` | 参数错误，非零退出。 |
| `precheck checkpoints-clear INPUT` | 按 INPUT **当前字节 hash** 删除所有对应的新检查点 run/step，包括其他路径或配置身份；保留旧文件版本、PII 映射、报告和费用记录。后续创建新 session。 |

解析和本地 PII 处理每次重新运行。grounding、颜色、报告与导出也重新运行。全命中可以避免 extraction/QA 请求，但不能推导为完整流程没有模型请求或不耗时；grounding 启用时仍可能请求模型。清除是逻辑删除，不是磁盘安全擦除。

## Windows 运行

以下命令用于 E 盘开发工作树；依赖保留在 C 盘独立 NTFS 环境。首次环境准备与模型配置分别见 [Windows 基线](windows-baseline-environment.md)、[PII 基线](pii-baseline.md)、[DeepSeek 配置](deepseek-setup.md)。本机模型配置已完成，不需要每个窗口重输 key。

每个新 PowerShell 窗口：

```powershell
Set-Location 'E:\Projects\openreview-cli-resume'
$env:UV_PROJECT_ENVIRONMENT='C:\Users\admin\Documents\ChatGPT\ai agent\.openreview-runtime'
$env:PYTHONUTF8='1'
$env:LITELLM_LOCAL_MODEL_COST_MAP='True'
New-Item -ItemType Directory -Path 'review_results' -Force | Out-Null
```

运行公开英文 NDA。下列审查命令在配置云模型时会产生 API 费用。严格提示词修复后已验证一次真实 smoke 和同进程重复调用；在新的进程/环境运行仍需检查实际报告，不能仅凭退出码认为每条成功。

```powershell
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync openreview precheck review tests/fixtures/nda_with_pii.pdf --qa-model reasoning --no-grounding --resume --format json --output review_results/checkpoint-review.json

# 完整成功后再次运行同一命令，复用 extraction / QA。
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync openreview precheck review tests/fixtures/nda_with_pii.pdf --qa-model reasoning --no-grounding --resume --format json --output review_results/checkpoint-review.json

# 强制重新请求，但保留同一费用 session。
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync openreview precheck review tests/fixtures/nda_with_pii.pdf --qa-model reasoning --no-grounding --resume --force-review --format json --output review_results/checkpoint-review-forced.json

& 'E:\Projects\.tools\uv\uv.exe' run --no-sync openreview gateway costs --today
```

`--qa-model reasoning` 显式使用已配置的 reasoning slot；原 CLI 默认 QA 使用 extraction 的 slot。`--format json` 让 `--output` 写指定审查报告；`--memo-format json` 是另外的 memo 导出，不能代替报告格式。输出在 Git 忽略目录。不要用 `--no-pii` 绕过云端脱敏门禁。

需要删除当前文件字节对应的检查点时：

```powershell
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync openreview precheck checkpoints-clear tests/fixtures/nda_with_pii.pdf
```

命令输出删除的 run/step 数量。若文件已经改过，这条命令清理修改后的字节身份，不会清理以前版本。

## 失效、存储与错误边界

身份绑定文件 SHA-256、规范化路径、完整 playbook 与版本、模式/阈值/隐私/grounding 设置、有效 gateway 配置、实际 PromptStore 提示词，以及固定启动时的实现与依赖清单。候选 primary/fallback/recovery 路由、有效端点和凭据参数也参与身份。含秘密的快照只在内存计算带密钥的 HMAC，不把凭据或未加密凭据摘要写入新表。无关数据库写入不作为缓存失效依据。

动态输入在请求前与成功保存前重查，发生变化则以固定错误停止。远端相同模型别名背后的版本变化无法可靠检测。结果里的模型字段是 slot 标签，不是 fallback 后实际执行模型的溯源证明。

两张新增 SQLite 表分别存 run 与步骤。只有 completed、身份匹配、解密成功且严格 schema 合法的结果可复用。payload 使用显式白名单与 Fernet 认证加密；不保存 clause_text、路径、请求、原始响应、凭据或异常正文。citation/rationale 等允许结果字段仍可能含敏感信息，因此也被加密。恢复只在内存注入本次解析/脱敏后的条款文本。

生产密钥 `review-checkpoints.key` 独立随机生成，位于现有 Windows 用户 AppData 配置目录（本机 C 盘 NTFS），通过 `get_config_dir()` 定位，位于 Git 工作树外。它不复用上游公开的 PII 默认密钥。Windows chmod 不是 ACL 防护；不承诺抵抗同用户或管理员访问，也不承诺保护整个上游数据库的其他记录。

| 情况 | 处理 |
| --- | --- |
| 单条坏密文、错误 schema/身份 | 安全 miss，重算该步骤。 |
| 未完成或失败步骤 | 不复用；已完成的其他步骤保留。 |
| 密钥损坏/不可用、数据库不可用、写入失败、运行中身份漂移 | 固定安全错误，终止 resume；不输出底层异常正文，不悄悄降级。 |
| 中断发生在远端回复后、本地提交前 | 该回复可能被再次请求并收费。 |

SQLite 事务保证本地步骤写入的一致性，不包含远端模型请求。唯一主键避免重复本地行，不保证远端 exactly-once。支持边界是单个前台进程、同文件恢复；没有多进程认领、分布式锁、跨版本复用、OCR 或 Web 服务。

## 可复现的离线故障实验

脚本 [`benchmark_review_checkpoints.py`](../scripts/benchmark_review_checkpoints.py) 使用三条合成条款，在 extraction/QA 的 `call_gateway_chat` 接缝统计调度，替换模型响应并固定 runtime 快照。它执行真实 ReviewStage 和本地检查点持久化，不请求云端，不读取实际凭据，不解析 PDF，不运行真实 PII/grounding，也不验证完整 Gateway 门禁。

为每次实验使用新的空目录；非空目录会被拒绝，脚本不会先删除已有数据。`--output` 是必填的 receipt 路径，示例使用忽略目录，不覆盖已提交的测量记录。

```powershell
$trialDir = Join-Path 'review_results' ('checkpoint-trial-' + [guid]::NewGuid().ToString('N'))
$trialReceipt = Join-Path $trialDir 'receipt.json'
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync python scripts/benchmark_review_checkpoints.py --work-dir $trialDir --output $trialReceipt
```

脚本为合成实验创建隔离测试 key/数据库，与生产 AppData 密钥分开。可以省略 `--work-dir` 使用临时目录，但明确的新目录更容易查看实验产物。不要重复使用非空目录；另建新的 `$trialDir` 即可。

已落盘测量见 [clause-checkpoints-offline.json](benchmarks/checkpoints/clause-checkpoints-offline.json)，对应实现与故障测试已通过独立审查：

| 场景 | extraction | QA | 结果 |
| --- | ---: | ---: | --- |
| 冷运行、不中断 | 3 | 3 | 6 次调度，作为归一化结果基准。 |
| 第二条 QA 请求前中断 | 2 | 1 | 3 次已发生调度。 |
| 上述中断后的恢复 | 1 | 2 | 只补剩余 3 次，归一化结果等于基准。 |
| 全部命中 | 0 | 0 | 避免 extraction/QA 调度。 |
| force-review | 3 | 3 | 重新执行 6 次，session 保持相同。 |
| 第二条 extraction 回复后、提交前中断，再恢复 | 2 + 2 | 1 + 2 | 两次运行合计 7 次，重请求窗口被实际展示，归一化结果等于基准。 |
| 一次 QA 超时后恢复 | 0 | 1 | 仅重试失败 QA，归一化结果等于基准。 |

receipt 的 Stage 执行时间为：冷运行约 0.315 秒、恢复约 0.178 秒、全命中约 0.035 秒、force 约 0.320 秒。计时不含真实模型延迟、解析/PII、Python 导入和 run/key 初始化；这是合成模型替身下的单次测量，不能当完整流程加速比或真实收费节省。调用次数也不是平台 HTTP/token 账单。

独立审查执行 9 个文件中的 30 个新增检查点用例，全部通过（11.86 秒）；另有 66 个原有核心回归通过（5.02 秒）。范围覆盖 codec/store、有效身份与环境初始化、实际 PromptStore、严格响应、ReviewStage 中断、runner/CLI、旧 schema 升级与回滚、DB/WAL 明文扫描以及故障实验。审查发现的 SQLite 非 BLOB payload 与共享实现文件遗漏两个问题均已修复并独立复验。

C 盘草稿 10 个 pre-commit hook 通过，E 盘早先 47 新用例及适用 hook 复验通过；真实 smoke 发现提示词问题后，修复后的 E 盘 12 个文件共 78 个新增用例通过（8.33 秒）。独立审查的严格模板用例 36 passed（0.95 秒）、原有提示词相关 21 passed（1.47 秒），24 个模式的 288 个变化输入与修复前 HEAD 的非严格输出字节一致；另有 worker 117 个回归通过（6.97 秒）。不同集合可能重叠，不相加为全仓库测试数。

mypy 667 文件是前次检查记录；每个提交的 pre-commit 结果记入提交说明。pytest hook 使用 `--collect-only`，没有执行整个 unit suite，不能把收集到的用例数当运行通过数。为解决 Windows 的上游 `os.geteuid()` 导入/类型问题，两个权限测试增加窄幅 POSIX guard，保留 Linux 断言；该任务仍有一个未修改的 Linux 错误字符串断言在 Windows 失败，见 [独立任务记录](../specs/036-clause-checkpoints/windows-test-task.md)。全仓库运行、真实准确率和生产验证仍未完成。

首次真实 smoke 使用公开 NDA、`--resume --qa-model reasoning --no-grounding`：发生 1 次 extraction 调度，SDK 返回 usage（prompt 445、completion 1278、total 1723，finish reason 为 stop），报告 5 个 assessment 中 1 个为 `extraction_invalid_response`。CLI exit 0 不等于所有 assessment 成功，缓存对比因此暂停。首个原始响应未保留，不能事后推断其精确内容；另一次有界诊断只输出结构与枚举，确认 `position=no-match` 不在严格 Position 枚举中，而模板允许该值。随后严格模板从 Position 枚举生成允许值，在类别不符/证据不足时要求 uncertain；保留严格校验和原有非 resume 行为。修复独立审查及以下真实重跑均通过。详情见 [DeepSeek 验证记录](deepseek-setup.md)。

此公开样例解析出 5 条款，关键词类别匹配只选中 1 条候选，其余 4 条为确定性 no-match、跳过云调用。不能据此称全部 5 条都经过模型法律审查，也不能得出类别识别召回率或审查准确率。

## 修复后的真实 smoke 与同进程复用

[安全计数 receipt](benchmarks/checkpoints/clause-checkpoints-deepseek-smoke.json) 记录 `deepseek-flash`、PII stripping 开启、grounding 关闭、同一进程两次 CliRunner 调用。模型响应通过严格 schema 校验；原始响应未保存到诊断。SDK 调度数不等于底层 HTTP 重试次数或平台账单。

| 阶段 | SDK 调度 | assessment / failed | 时间 | 结果 |
| --- | ---: | --- | ---: | --- |
| 修复后首次调用 | 2（1 extraction + 1 QA） | 5 / 0 | 16.921 秒 | exit 0，两个模型结果严格 schema 合法。 |
| 同进程重复 `--resume` | 0 | 5 / 0 | 1.581 秒 | exit 0，归一化 assessments 与首次相同。 |

extraction 的 max_tokens 为 2000，实际 SDK usage 为 prompt 470 / completion 275 / total 745；QA 的 max_tokens 为 4000，usage 为 567 / 644 / 1211。两者 finish reason 都为 stop。解析和 PII 仍重新运行；这是一个同进程样例，没有控制冷启动或做多次统计，不能推出通用加速比。实际账单未核验，grounding 未验证，也没有人工标注的准确率或生产 SLA。

随后启动独立 Python 进程，用 SDK 入口 guard 在任何模型请求前终止违规调用，验证磁盘检查点而不是只验证同进程状态。[新进程 receipt](benchmarks/checkpoints/clause-checkpoints-fresh-process.json) 记录 0 SDK 尝试、exit 0、5 assessment/0 failed、归一化结果与首次相同；PII 开启、grounding 关闭。CliRunner 调用耗时 9.071 秒，不含完整 Python 导入，不能与 1.581 秒样例直接推导一般启动性能。guard 检查模型 SDK 入口，不是全进程网络审计。

两类 fork receipt 使用专门的 `docs/benchmarks/checkpoints/`，保留上游 `docs/benchmarks/results/` 的十个固定记录不变。目录精确集合测试曾因新增离线 JSON 放在上游目录失败，已通过移动记录和修改链接修复，没有放宽测试或修改工具配置。

设计解释与提交依据见 [学习指南](engineering-learning-guide.md)、[变更账本](change-rationale.md)。

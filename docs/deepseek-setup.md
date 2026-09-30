# DeepSeek 本机配置与设计依据

状态：离线适配、本机配置与严格提示词修复已完成；公开 NDA 真实 smoke、同进程复用与新进程持久化恢复通过。准确率、账单和生产可靠性仍未验证。官方能力核验日期：2026-09-30；最新验证记录更新：2026-10-01。
代码在 E:\Projects\openreview-cli-resume；依赖在 C 盘的独立 NTFS 环境。

## 为什么这样修改

- 官方当前模型名为 `deepseek-flash`（V4.1-Flash）和
  `deepseek-v4-pro`（V4-Pro-0813），base URL 是
  `https://api.deepseek.com`，上下文为 1M。上游只有旧
  `deepseek-chat` 与 64K 能力信息，导致超过 64K 的请求被本地能力
  门禁提前拒绝。新增当前名称和能力，旧记录保留但标记 deprecated，
  不改变其他供应商或项目默认配置。
- 上游 gateway setup 的供应商选项没有 DeepSeek，并且要求逐一配置
  六种 slot。新增专用前台脚本，只配置 extraction、reasoning 和可选
  grounding，不配置 embedding、reranking、graph，不下载 Ollama。
- extraction 与 QA 期待 JSON object，因此配置
  `response_format={"type":"json_object"}`。grounding 的现有提示词
  期待 JSON array，强制 object 会破坏响应解析，因此不设置该参数。
- 使用现有 auth 持久化函数，隐藏输入 key，只保存在当前用户配置目录
  的 auth.json，不放 Git 工作树，不输出原始异常。不允许 getpass
  回退到可见输入。balanced 隐私与 PII stripping 保持启用。
- 清除所选 slot 原有 fallback 和供应商特有 extra_params，避免配置
  DeepSeek 后意外继续调用旧供应商；保留其他 slot 与其他供应商凭据。

官方依据：
- [模型与能力](https://api-docs.deepseek.com/quick_start/pricing/)
- [JSON 模式要求与空响应边界](https://api-docs.deepseek.com/guides/json_mode/)
- [旧模型名称退休通知](https://api-docs.deepseek.com/news/news260424/)

## 遇到的问题与解决方法

1. Windows 默认 GBK 会导致仓库 UTF-8 SQL 读取失败，运行时设
   PYTHONUTF8=1。E 盘 exFAT 不适合依赖链接，代码留 E 盘、依赖放 C 盘。
2. set_config_value 不解析 JSON 字符串为字典。初次向导用整体 JSON
   写 extra_params 失败，改用嵌套配置路径写 response_format.type，
   由现有配置验证与持久化流程处理。
3. 首次 load_config 返回默认配置，后续读取会增加默认 null 字段。
   测试先按现有 schema 规范化基线，再比较未选 slot，避免把等价
   配置误判为修改。
4. pytest basetemp 不会创建缺失的父目录；先创建项目自己的
   .test-temp，再用每轮独立子目录，避免 Windows 临时目录 ACL 问题。
5. LiteLLM 导入默认会下载价格表，离线测试设
   LITELLM_LOCAL_MODEL_COST_MAP=True。socket 在测试中被禁止，
   任何依赖尝试联网也会被拦截，未发生收费模型调用。

## 本机隐藏输入 key

每个新的 PowerShell 窗口先设置工作目录和环境：

```powershell
Set-Location 'E:\Projects\openreview-cli-resume'
$env:UV_PROJECT_ENVIRONMENT='C:\Users\admin\Documents\ChatGPT\ai agent\.openreview-runtime'
$env:PYTHONUTF8='1'
$env:LITELLM_LOCAL_MODEL_COST_MAP='True'
```

配置脚本只在首次配置、换 key 或改模型时运行，不需要每次打开窗口重跑：

```powershell
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync python scripts/configure_deepseek.py
```

按提示在本机隐藏输入 key，不要发到聊天。该命令只写本机配置，不发送
模型请求、不测试余额。默认三个审查 slot 使用 deepseek-flash。
本机已完成一次授权配置；真实 smoke 使用 deepseek-flash 已通过，Pro 未实测。改选 Pro：

```powershell
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync python scripts/configure_deepseek.py --model deepseek-v4-pro
```

加 --no-grounding 可保留 grounding 原配置；审查时还需 --no-grounding
才会跳过该阶段。embedding/reranking 保留原配置，不意味着本机已经
安装对应模型。

密钥文件在当前用户配置目录，位于 Git 工作树之外。Windows 的 POSIX
chmod 不等于 ACL 防护，不承诺防御同用户进程或本机管理员。不要分享
auth.json。写入失败可能只完成部分设置，排查文件访问后重跑向导。
脚本只输出固定错误，不打印原始异常或 key。

## 检查与验证边界

读取模型和配置，不调用 DeepSeek：

```powershell
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync openreview gateway models deepseek --json
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync openreview config get gateway.models.extraction.primary
```

现有 gateway test 提示词未要求 JSON，也没有审查阶段的脱敏状态，
不适合作为这个配置的首个调用检查；门禁拒绝不证明 key 无效。
配置完成后使用公开合成 NDA 做实际审查。该步骤会产生 API 费用；
首次 `--resume` smoke 暴露提示词/schema 不一致，修复后的真实 smoke
已通过。下列命令演示报告及 memo 输出；成功 receipt 使用同进程
CliRunner 两次调用，不等于这条独立命令的逐字执行记录：

```powershell
New-Item -ItemType Directory -Path 'review_results' -Force | Out-Null
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync openreview precheck review tests/fixtures/nda_with_pii.pdf --qa-model reasoning --no-grounding --format json --memo-format json --output review_results/deepseek-smoke.json
```

`--format json` 才会让 `--output` 保存审查报告到指定文件；
`--memo-format json` 另外生成命名的 memo 文件，默认目录为
`review_results`，可用 `--output-dir` 指定。两者是不同输出，不能用
memo 参数代替报告格式。依据：`app.py` 的 `_emit_reviews` 与
`_export_memo_reports`。

显式 `--qa-model reasoning` 使用向导配置的 reasoning slot；原 CLI
默认 QA 与 extraction 使用同一 slot。本命令没有修改这个默认行为。

不得用 --no-pii 绕过脱敏门禁。当前模型默认启用 thinking，安装的
LiteLLM 1.90.1 对 disabled thinking 映射有限制，因此未强制禁用，
不能声称非思考模式已适配。JSON 空内容或截断需通过真实样例再评估。
模型别名可能变化；离线测试不证明账号权限、服务可用性或审查准确率。
价格表可能没有当前别名，账本以整美分记录，费用估计为零也不代表免费，
本地预算不能承诺为精确账单硬预算，应核对平台 usage/账单。

## TDD 证据

新增测试首次有效 RED：5 failed、7 missing-helper errors、3 passed。
失败覆盖新模型缺失、旧模型未退休和 64K 门禁；随后最小实现通过
17 个离线用例（2.99 秒，1 个已被 socket 防火墙拦截的依赖警告）。
这些测试检查真实 Gateway kwargs、原生 LiteLLM provider/URL/JSON
转换、脱敏前阻断、取消与空 key、损坏 auth、隐藏输入失效、异常泄密。

```powershell
New-Item -ItemType Directory -Path 'C:\Users\admin\Documents\ChatGPT\ai agent\.test-temp' -Force | Out-Null
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync pytest tests/unit/test_deepseek_current_models.py tests/unit/test_configure_deepseek.py -q --basetemp 'C:\Users\admin\Documents\ChatGPT\ai agent\.test-temp\deepseek-final-check' --reruns 0
```

后续运行使用 --no-sync，保留已准备的 PII 模型。离线测试仅使用占位
凭据；授权配置的真实 key 已保存于工作树外的用户配置目录，未进入
Git。独立审查已验证 17 个新增用例及下述 72 个局部回归；真实 API
鉴权目录、修复后 smoke 与同进程复用已通过，质量与账单另行评测。

收尾在同一上游版本的 C 盘安全草稿执行：17 个新增用例加原有
registry/models 回归，共 72 passed（3.35 秒）；ruff check 和 format
检查通过。扩大到未修改的 auth/wizard 用例时，92 个用例中
84 passed、2 skipped、6 failed：两个 CLI 用例受 AppData 写权限限制，
一个 Windows 用例假定 POSIX 0600 权限，三个 questionary 用例需要
真实 Windows 控制台。这些失败没有通过改断言或放宽配置掩盖，
也不能把扩大后的整个测试集报告成全通过。

## 首次真实请求暴露的问题与修复

本机配置保存在工作树外，未进入 Git。带鉴权 `GET /models` 返回 HTTP
200，目录列出 `deepseek-flash`，说明当前凭据可以读取该模型目录；
不等于审查工作流或模型结果合格。

公开 `nda_with_pii.pdf` 的真实 CLI smoke 使用 `--resume --qa-model
reasoning --no-grounding`。一次 extraction 调度到达 SDK，返回 usage：
prompt 445、completion 1278、total 1723，finish reason 为 stop。报告
有 5 个 assessment，其中 1 个 `extraction_invalid_response`；exit 0
仅说明命令完成，不代表全部条款成功。未继续进行缓存对比，也没有
把这次结果作为审查准确率或节省金额。

5 个解析条款中，关键词类别匹配只选中 1 条候选，其余 4 条确定性
no-match、不请求云模型。这不是全部 5 条均经过模型法律审查的证据，
也不是类别识别召回率评测。

首个原始响应没有保留，不能声称确认了它的精确枚举。随后一次有界
诊断只保留字段类型/枚举与校验布尔结果，确认 JSON object 的
`position` 为 `no-match`，严格校验为 false。源码 extraction 模板
允许这个值，严格 `Position` schema 不接受，是请求契约不一致。
随后只对严格模板做最小对齐：允许值直接从 Position 枚举生成，类别
不符或证据不足时返回 uncertain。非 resume 行为与严格 validator 保留，
没有放宽校验让错误结果进入缓存。独立审查36新模板用例通过，原有
相关21用例通过，24模式/288变化输入的非严格输出字节不变；E盘修复后
78新用例通过。token usage不是平台实际账单；未保存合同原文、原始
响应或供应商异常正文到诊断记录。

## 修复后的真实 smoke 与复用

安全计数记录为 [clause-checkpoints-deepseek-smoke.json](benchmarks/checkpoints/clause-checkpoints-deepseek-smoke.json)。模型 deepseek-flash，PII stripping
开启，grounding关闭；同一进程两次CliRunner调用，最多2次SDK调度。

| 调用 | SDK调度 | assessment / failed | 耗时 | 结果 |
| --- | ---: | --- | ---: | --- |
| 修复后首次 | 2（extraction + QA） | 5 / 0 | 16.921秒 | exit0，两个响应strict schema合法、finish stop。 |
| 同进程再次resume | 0 | 5 / 0 | 1.581秒 | exit0，归一化结果与首次相同。 |

extraction max_tokens=2000，SDK usage为prompt470/completion275/total745；
QA max_tokens=4000，usage为567/644/1211。实际SDK usage不等于HTTP重试
次数或账单金额；billing_amount_verified=false。该样例仍只有1类别候选、
4确定性no-match。解析和PII再次运行；单个同进程样例不是通用加速比、
冷启动保证、准确率或生产可靠性评测。每个提交的pre-commit结果记入
提交说明；真实其他模型/grounding没有通过本次实验验证。

另用独立Python进程重复相同参数，并在模型SDK入口放置请求前guard。
[新进程 receipt](benchmarks/checkpoints/clause-checkpoints-fresh-process.json)记录0 SDK尝试、exit0、5 assessment/0 failed、归一化结果与首次相同；
说明已写入的检查点能跨进程恢复。PII仍开启、grounding仍关闭。9.071秒
为该CliRunner调用时间，不含完整Python导入，不能当作通用冷启动性能；
没有发送新的模型SDK请求，不是全进程网络审计。

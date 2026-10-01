# 本地 PII 环境与 Parse + Strip 基线

核验日期：2026-09-30。此记录验证上游已有解析与本地脱敏资源，不是新增审查功能的效果报告。

## 独立环境

- 工作树：`E:\Projects\openreview-cli-resume`。
- 独立 runtime：`C:\Users\admin\Documents\ChatGPT\ai agent\.openreview-runtime`。
- Python：3.12.14；锁定并保留的 spaCy：3.8.14；Presidio Analyzer：2.2.362。
- 补充的本地资源：`en_core_web_lg==3.8.0`，由 uv 安装官方 wheel，使用 `--no-deps`，没有降级 spaCy 或增加项目依赖。
- 安装前 C 盘空闲 9.60 GiB，E 盘空闲 488.25 GiB；uv 下载显示 wheel 为 382.1 MiB。缓存放在 E 盘，跨盘安装使用复制。

官方 [兼容表](https://raw.githubusercontent.com/explosion/spacy-models/master/compatibility.json) 对 spaCy 3.8 列出 en_core_web_lg 3.8.0；官方 [模型元数据](https://raw.githubusercontent.com/explosion/spacy-models/master/meta/en_core_web_lg-3.8.0.json) 要求 `>=3.8.0,<3.9.0`，与 spaCy 3.8.14 匹配。

实际安装命令：

```powershell
& 'E:\Projects\.tools\uv\uv.exe' --cache-dir 'E:\Projects\.tools\uv-cache' pip install --python 'C:\Users\admin\Documents\ChatGPT\ai agent\.openreview-runtime\Scripts\python.exe' --no-deps 'https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl'
```

这里的 `pip install` 是 uv 的子命令，没有运行 pip 可执行文件。该资源是现有 Presidio 功能所需的模型，不修改 `pyproject.toml` 或 `uv.lock`。后续运行使用下列作用域环境，并加 `--no-sync`，避免 uv 清理额外安装的 PII 模型：

```powershell
$env:UV_PROJECT_ENVIRONMENT='C:\Users\admin\Documents\ChatGPT\ai agent\.openreview-runtime'
$env:PYTHONUTF8='1'
$env:LITELLM_LOCAL_MODEL_COST_MAP='True'
& 'E:\Projects\.tools\uv\uv.exe' run --no-sync python -
```

## 实际基线结果

对公开的 `tests/fixtures/nda_with_pii.pdf`，在同步 Python 进程中调用真实 `parse_document(..., allow_password_prompt=False)` 与 `strip_pii_clauses(..., engine=PiiEngine(threshold=0.7), allow_partial=False)`。没有模型 mock；没有调用 review / extraction / QA；没有启用治理持久化的 StripStage，因此本次检查没有创建 PII 映射文件或读取 auth。

运行禁用全部外部 socket，关闭日志，并捕获内部 stdout/stderr；计数结果是唯一输出。同步基线无需 localhost 例外。没有执行 memory suite 或收费 API。

| 项目 | 实测值 |
| --- | ---: |
| 状态 | passed |
| PDF 页数 | 1 |
| 条款数 | 5 |
| 检测实体数 | 7 |
| 冷启动总耗时（含 Python 导入和模型加载） | 55.120 秒 |

检查同时确认：保留条款数量、没有失败页、存在真实实体检测、检测映射中的原值没有出现在脱敏文本中、每个映射都有对应占位符。没有打印姓名、邮件、原文、映射、异常正文或密钥。

最小 receipt 在忽略目录 `review_results/pii-baseline/parse-strip-summary.json`，内容只包含 status、pages、clauses、entities 和 elapsed_seconds。

## 证据边界

这说明本机能够加载兼容的本地模型，并完成该公开英文 PDF 的 Parse + Strip 流程。实体数量是检测结果，不是人工标注的召回率；原值移除检查仅针对已经检测到的值，无法证明所有敏感信息都被发现。本记录不证明整体上游无明文持久化、企业合规或合同审查准确率。后续状态更新（2026-10-01）：DeepSeek smoke 曾发现严格提示词/schema 不一致，修复后真实 smoke、同进程复用与新进程恢复已通过，见 [DeepSeek 记录](deepseek-setup.md)；那是另一个工作流验证，不能扩展为脱敏召回率或合规结论。不能通过关闭脱敏绕过本地资源检查。

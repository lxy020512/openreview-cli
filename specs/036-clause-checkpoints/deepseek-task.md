# DeepSeek current-model setup task

Status: approved narrow implementation; user selected DeepSeek on 2026-09-30.
This task is independent from the clause-checkpoint feature and does not authorize
real paid API calls before the user configures a key locally.

Scope:
- Add the current `deepseek-flash` and `deepseek-v4-pro` aliases to the bundled
  provider registry. Keep the legacy entry for parsing existing configurations,
  mark it deprecated, and leave other providers and defaults unchanged.
- Verify registry capabilities, real Gateway request construction, and installed
  LiteLLM provider/URL/JSON transformation without network access.
- Provide a foreground, local-only configuration helper using hidden key input
  and the existing auth.json persistence functions. Preserve unrelated providers
  and slots; configure extraction, reasoning, and optional grounding only.
- Balanced privacy with PII stripping stays required. No embedding/reranking
  reassignment and no Ollama download.
- JSON object response mode applies only to extraction and reasoning. Grounding
  currently expects a JSON array and must not inherit json_object mode.
- Do not print key values or raw exception details. Store keys in the user
  configuration directory, outside the Git worktree. Windows POSIX mode bits
  do not constitute an ACL security guarantee.

Official evidence checked on 2026-09-30:
- https://api-docs.deepseek.com/quick_start/pricing/
- https://api-docs.deepseek.com/guides/json_mode/
- https://api-docs.deepseek.com/news/news260424/

Verification: failing tests first, minimal implementation, offline passing tests,
targeted lint/type checks, then independent review. Alias acceptance, contract
review quality, real usage accounting, and billing require a later live check.

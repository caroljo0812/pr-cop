# Examples

Ready-to-run snippets that exercise PR Cop without setting up a real LLM
provider. Every script defaults to `PRCOP_LLM_PROVIDER=mock` so it stays
deterministic and offline-friendly — flip the env var to `mimo`, `openai`,
`together`, or `gateway` (with `PRCOP_LLM_API_KEY=...`) when you want a real
review.

## Files

- `sample.diff` — a small unified diff with a deliberate hardcoded secret + a
  blocking I/O call inside an async function. Good for smoke-testing the
  `security` and `performance` specialists.
- `review-cli.sh` — runs `prcop review --diff sample.diff` and prints the
  text report.
- `review-curl.sh` — POSTs `sample.diff` to a running PR Cop server at
  `http://localhost:8080/review/diff` and pretty-prints the verdict.
- `review-curl-authed.sh` — same as above but sends `X-PRCOP-API-Key` for
  servers running with `PRCOP_API_KEY` set.

## Quick start

```bash
# CLI flow (no server needed)
PRCOP_LLM_PROVIDER=mock bash examples/review-cli.sh

# HTTP flow (in one shell)
PRCOP_LLM_PROVIDER=mock prcop serve

# in another shell
bash examples/review-curl.sh
```

## Real reviewer

```bash
export PRCOP_LLM_PROVIDER=mimo
export PRCOP_LLM_API_KEY=sk-...
bash examples/review-cli.sh
```

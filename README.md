# fraud-mcp

An MCP server that exposes fraud-investigation tools to an AI agent, built around
one architectural claim:

> **The rules engine decides what fired. The model only interprets and narrates.**

An agent connected to this server can pull an account's transactions, run a
deterministic rules evaluation, pivot across devices, and record a case. What it
*cannot* do is decide for itself whether a pattern is fraud — that verdict comes
from a rules engine with fixed thresholds and reproducible output, and every
response carries enough provenance that a human can audit the decision months
later.

The dataset is entirely synthetic and generated from a fixed seed. Nothing here
is real or scraped.

---

## Why it is built this way

An LLM reading raw transactions and announcing "this looks like structuring" has
produced an opinion, not a finding. It is unreproducible (the same rows may get a
different answer tomorrow), unauditable (there is no threshold to point at), and
unfalsifiable (there is no way to show it was wrong). In a regulated setting that
is not merely sloppy, it is unusable: a fraud decision generally has to be
explainable to a customer, a reviewer, or a regulator.

So the labour is split:

| | Rules engine | Model |
|---|---|---|
| Decides which rules fired | ✅ | ❌ |
| Chooses thresholds | ✅ | ❌ |
| Assigns severity | ✅ | ❌ |
| Explains what a firing means | ❌ | ✅ |
| Decides which account to look at next | ❌ | ✅ |
| Writes the case narrative | ❌ | ✅ |

The model is used for the part it is genuinely good at — judgement about where to
look next, and turning a set of machine verdicts into prose a human can act on.
It is kept away from the part where non-determinism is a liability.

Three design choices follow directly from that split, and they are the ones worth
looking at:

**1. Every rule reports its thresholds and its evidence.** A `FIRED` result
carries the thresholds applied, the values observed, and the exact transaction
ids relied on. A narrative that cites no transaction id is unfalsifiable, so the
tool makes the citations impossible to miss.

**2. There are three rule states, not two.** `FIRED`, `NOT_FIRED`, and
`SKIPPED`. A rule that could not be evaluated — no baseline history, no device
telemetry — returns `SKIPPED` with a reason. Collapsing that into "did not fire"
is how a false negative gets laundered into a clean bill of health, and it is the
single easiest way for an agent to be confidently wrong.

**3. The evaluation clock is frozen.** Rules evaluate against the dataset's
`as_of` timestamp, not wall-clock now. Without it, "5 transactions in 10 minutes"
would silently stop firing as the seeded data aged, and every verdict in this
README would rot. Every response echoes `as_of` so a result can be reproduced
exactly.

---

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
git clone <your-fork> && cd fraud-mcp
uv sync --extra http
uv run python scripts/serve.py seed    # build the synthetic dataset
uv run python scripts/serve.py check   # print row counts and the as_of clock
```

Run the test suite and the end-to-end scenarios:

```bash
uv run pytest
```

```bash
uv run python scripts/run_scenarios.py
```

The scenario runner drives a real MCP client against the server through six
investigation scenarios — including the failure paths — and asserts on every
response. It is a scripted client, not an agent: fully reproducible.

Run the server directly:

```bash
uv run python scripts/serve.py stdio
```

```bash
uv run python scripts/serve.py http --port 8000
```

> **Why `scripts/serve.py` and not the `fraud-mcp` console script?**
> Both work, but `serve.py` puts `src/` on `sys.path` itself rather than relying
> on the editable install. During development uv was observed to disable this
> project's editable `.pth` entry after a source edit, so the console script
> would fail with `ModuleNotFoundError` until the next
> `uv sync --reinstall-package fraud-mcp`. An MCP server that intermittently
> fails to start is a bad demo, so the documented entry point is the one that
> keeps working across edits.

The dataset lives in `~/.local/share/fraud-mcp/fraud.sqlite3` (override with
`FRAUD_MCP_DB`). It is deliberately kept out of the repository: it is a generated
artifact, reproducible from seed `1337`, and writing it inside the project tree
made uv treat the package as modified on every run.

---

## Connecting it to Claude

### Claude Code

A `.mcp.json` is committed at the repo root, so from inside the project
directory the server is picked up automatically. Verify with `/mcp`.

### Claude Desktop

Add this to `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS,
`%APPDATA%\Claude\` on Windows), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "fraud-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/fraud-mcp",
        "python",
        "scripts/serve.py",
        "stdio"
      ]
    }
  }
}
```

Use the absolute path to your checkout. If `uv` is not on the PATH that Claude
Desktop sees, use its full path (`which uv`).

Try: *"Account ACC-1013 was reported by a customer. Investigate it and flag a
case if warranted."*

---

## The tools

### `evaluate_fraud_rules(account_id)`

The authoritative risk verdict. Runs all seven rules and returns which fired, which
did not, and which could not be evaluated. Deterministic.

```jsonc
{
  "ok": true,
  "account_id": "ACC-1021",
  "verdict": {
    "rules_fired": 2,
    "rules_evaluated": 6,
    "rules_skipped": 0,
    "highest_severity_fired": "critical",
    "fired_rule_ids": ["AMOUNT_SPIKE", "STRUCTURING"],
    "skipped_rule_ids": []
  },
  "rule_results": [
    {
      "rule_id": "STRUCTURING",
      "status": "FIRED",
      "severity": "critical",
      "thresholds": { "reporting_threshold": 10000.0, "min_txns": 3, "window_hours": 72 },
      "observed": { "max_in_band_within_window": 5, "window_total_amount": 46945.0 },
      "evidence_txn_ids": ["TXN-006585", "..."],
      "explanation": "5 transactions between 8500 and 10000 USD within 72 hours..."
    }
  ],
  "interpretation_contract": { "authority": "...", "your_role": "...", "skipped_is_not_clean": "..." },
  "provenance": { "as_of": "2026-09-14T12:00:00+00:00", "rules_version": "2026.09.2", "...": "..." }
}
```

### `get_transactions(account_id, days)`

Evidence, not a verdict. `days` is 1–365, counted back from `as_of`. A response
with zero rows carries an `empty_result_guidance` block stating explicitly what
the emptiness does and does not imply.

### `lookup_device_history(device_id)`

Every account and login event seen on a device — the pivot that turns one
compromised account into a mapped takeover ring. Surfaces
`distinct_accounts_with_events`, `failed_login_count`, and
`password_reset_count`.

### `flag_case(account_id, reason, severity, rule_ids)`

Records an investigation outcome. The only tool that writes. Its response
contains an `audit` block that independently re-runs the rules engine and
compares the submission against it: cite rules that are not firing, omit
`rule_ids`, or pick a severity the engine does not support, and the case is still
recorded but flagged `supported_by_engine: false` with warnings for the human
reviewer.

That audit block matters more than it looks. It means a model that skips straight
to flagging, or that escalates on vibes, leaves a machine-readable trace of
having done so — rather than producing a case file indistinguishable from a
well-founded one.

---

## The rules

| Rule | Severity | Fires when |
|---|---|---|
| `VELOCITY_BURST` | high | ≥5 transactions in any 10-minute window |
| `AMOUNT_SPIKE` | medium | A transaction ≥5× the account's median baseline (90-day baseline, excluding the last 7 days) |
| `NEW_GEO_HIGH_VALUE` | high | ≥1000 USD in a country absent from the prior 180 days |
| `STRUCTURING` | critical | ≥3 transactions of 8500–10000 USD within 72 hours |
| `SHARED_DEVICE` | critical | A device used by ≥3 distinct accounts within 30 days |
| `IMPOSSIBLE_TRAVEL` | high | Two card-present transactions in different countries ≤120 minutes apart |
| `DORMANT_REACTIVATION` | medium | An account transacts again after 90+ days of silence, opening at ≥500 USD or ≥3 transactions in 48h |

Thresholds live in one dict (`rules.T`) and are quoted back in every response, so
there are no magic numbers buried in the logic.

`DORMANT_REACTIVATION` is the odd one out, and deliberately so. Every other rule
that needs history degrades to `SKIPPED` on a dormant account — which is exactly
the population an account takeover prefers, and exactly the moment detection
matters. So this rule uses **absolute** thresholds only and never skips for want
of a baseline. On a dormant account that has not yet woken it reports `NOT_FIRED`
*armed*, naming the dormancy it is watching, so a reviewer can see the tripwire is
set rather than inferring it from silence. It was added because an agent found the
gap — see [FINDINGS.md](FINDINGS.md).

---

## The planted patterns

The dataset contains deliberate fraud so an agent has something real to find. A
dataset of uniform noise makes for a demo whose only honest answer is "nothing
here", which tests nothing.

| Account | Pattern | What is planted |
|---|---|---|
| `ACC-1007` | Velocity burst | 7 transactions in ~5 minutes, escalating 1.00 → 1890.00 (card testing then cash-out) |
| `ACC-1013` | Account takeover | Failed logins, password reset, then a 2450 USD purchase in a new country — all from `DEV-ATO-01` |
| `ACC-1014`, `ACC-1015` | Corroborating | Same attacker device, making the *device* the common factor |
| `ACC-1021` | Structuring | 5 transfers of 9.1k–9.7k across 40 hours, each below the 10k threshold |
| `ACC-1030` | Impossible travel | Card-present in US, then SG 38 minutes later |
| `ACC-1002` | **Control: clean** | Ordinary activity only — nothing should fire |
| `ACC-1040` | Dormant reactivation | Silent ~11 months, then a 4-transaction burst opening at 1,450 USD — fires `DORMANT_REACTIVATION` while the baseline rules `SKIP` |
| `ACC-1009` | **Control: dormant** | No activity in 180 days — the empty-result trap |

The two controls are the interesting ones. `ACC-1002` catches a rules engine that
fires on noise. `ACC-1009` catches an *agent* that reads an empty result as an
all-clear.

---

## Error handling

No tool ever raises across the MCP boundary. Every failure is an envelope:

```json
{
  "ok": false,
  "error": {
    "code": "UNKNOWN_ACCOUNT",
    "message": "No account with id 'ACC-9999' exists in this dataset.",
    "remediation": "Check the identifier against a prior tool response...",
    "details": { "received": "ACC-9999", "example_valid_ids": ["ACC-1000", "..."] }
  }
}
```

Codes: `UNKNOWN_ACCOUNT`, `UNKNOWN_DEVICE`, `INVALID_ARGUMENT`,
`INSUFFICIENT_DATA`, `INTERNAL_ERROR`. `remediation` is mandatory — an error an
agent cannot act on is a dead end that usually ends in the agent inventing an
answer instead.

One subtlety worth calling out: the MCP SDK validates arguments against the JSON
Schema *before* the handler runs, so an out-of-range `days` never reaches our
code and the client would get a raw pydantic string. `middleware.py` catches
errored `tools/call` results and rewrites them into the same envelope, so there
is exactly one error contract regardless of which layer rejected the call.

---

## Layout

```
src/fraud_mcp/
  server.py       MCP surface: tool registration, schemas, descriptions
  tools.py        Handlers — plain functions returning plain dicts
  rules.py        The deterministic engine. No model, no randomness
  middleware.py   Normalises schema rejections into the error envelope
  errors.py       Structured error taxonomy
  db.py           SQLite schema, connection, the frozen as_of clock
  seed.py         Synthetic data generator and planted patterns
tests/            78 tests: rules, handlers, and the MCP protocol surface
scripts/          run_scenarios.py — end-to-end investigation scenarios
FINDINGS.md       Where an agent misused these tools, and what fixed it
notes/runs/       Raw agent transcripts - the evidence behind FINDINGS.md
```

`tools.py` is transport-agnostic on purpose: the tests exercise the same code an
agent hits, so there is no drift between what is tested and what is called.

---

## FINDINGS.md

[FINDINGS.md](FINDINGS.md) records what happened when an agent was actually
pointed at this server. The experiment is a controlled one: `FRAUD_MCP_PROFILE=v0`
serves the same rules engine behind a naive first-draft interface, `v1` serves it
behind the hardened one, and the same five investigation tasks were run against
both. Raw transcripts are in `notes/runs/`.

The short version:

* **All four v0 runs called `get_transactions` before `evaluate_fraud_rules`** —
  forming an opinion from raw rows before asking the deterministic engine, which
  is precisely the failure this design exists to prevent. All five v1 runs
  reversed it. The fix was three pieces of prose, the most effective of which
  told a tool what it is *not*.
* **Opaque errors cost 21 wasted calls.** `"Error executing tool
  evaluate_fraud_rules"` cannot be recovered from; a typed code with a mandatory
  `remediation` can.
* **The agent read my schema `examples` as data and called one.** A planted
  identifier in an example leaked the answer and made v1 look far better than it
  was, until the agent volunteered how it had got there.
* **One failure no wording could fix:** with no way to list accounts or devices,
  the agent brute-forced the identifier space — 119 calls in the honest rerun.
  But given the identical gap, Claude Desktop made four calls and stopped: *"if I
  named more accounts, I'd be inventing them."* Same tools, opposite behaviour.
  The difference was having a human to hand the question back to, which makes an
  escalation path a safety control in its own right.
* **The guard that mattered most wasn't in the interface at all.** It was
  `SKIPPED` as a third rule state, distinct from `NOT_FIRED`. Prose in a
  description is a suggestion; a state in the data model is a constraint.
* **Some clients search for tools by keyword before loading their schemas**, so a
  tool name has to win a search before its description can influence anything.
  This one was called `check_velocity_rules` — a name describing one of its six
  rules — until that surfaced. Renaming it to `evaluate_fraud_rules` is the one
  fix in this project that came from running against a second client.

That document is the point of the project. The server is the apparatus.

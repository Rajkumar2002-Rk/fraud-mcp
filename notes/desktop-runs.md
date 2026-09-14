# Claude Desktop runs (native MCP client)

The v0/v1 experiment in FINDINGS.md used Claude Code subagents talking through
`scripts/agent_cli.py`. This file records the same tasks run in Claude Desktop —
a different client, native tool-calling, human in the loop. It is the check on
whether those findings survive outside the environment that produced them.

Server config: `claude_desktop_config.json` → `uv run --directory <repo> python
scripts/serve.py stdio`. Profile: v1 (default).

---

## Run 1 — ACC-1013, reported charge

**Call order:** `[tool search]` → **check_velocity_rules** → lookup_device_history
→ get_transactions → flag_case

**Outcome:** correct. Account takeover identified, `CASE-FD4ACD0631` written at
critical severity citing `AMOUNT_SPIKE, NEW_GEO_HIGH_VALUE, SHARED_DEVICE`.
Confirmed present in the `cases` table with all three rule ids and a severity
matching the engine's `highest_severity_fired`.

### Finding 1 holds in a native client

The first substantive call was `check_velocity_rules`. This is the result the
whole document hinges on, and it was the one most at risk of being an artifact of
the CLI bridge — so it surviving here matters more than any other line in this
file. Verdict-before-evidence now holds 6 out of 6 across two different clients.

### New: the client searched for tools before loading them

Desktop does not put every tool description in context up front. The transcript
opens with:

> Searched available tools — *fraud velocity rules transactions device history flag case*

That step does not exist in the CLI bridge, which hands over all four schemas in
one `list-tools` call. It means a tool has to be **discoverable by keyword**
before its carefully written description is ever read — and the description can
only influence behaviour *after* the name has already won the search.

`check_velocity_rules` is a bad name under that constraint. It reads as "checks
velocity rules", so a query about structuring, device sharing, or impossible
travel has no obvious reason to match it, even though the tool evaluates all six.
It survived here only because the search query was broad enough to sweep in
everything. A name like `evaluate_fraud_rules` would describe what the tool
actually does and match more of the queries an agent would plausibly form.

This is a class of failure the CLI harness structurally could not surface, and
it is the strongest argument in the project for testing against a real client.

### The documented call order was treated as priority, not as a script

Server instructions say transactions second, device third. Desktop went device
second, transactions third — because `SHARED_DEVICE` was the critical-severity
firing, so the device was the highest-value pivot.

That is a better decision than the one I prescribed, and it suggests the
numbered list is read as guidance rather than a sequence to execute. Worth
noting because the invariants that actually matter — verdict first, `flag_case`
last and only with cited rule ids — held regardless. Ordering guidance should
probably express *why* each step follows, not just a numbered order, so the
agent can reorder intelligently as this one did.

### The three-state contract reached the final write-up unprompted

The answer stated that `VELOCITY_BURST`, `STRUCTURING` and `IMPOSSIBLE_TRAVEL`
were evaluated and did not fire, and added: "No rules were skipped, so there are
no blind spots in this verdict."

Nothing asked it to report non-firing rules or to distinguish skipped from
not-fired. Returning all six outcomes with three distinct states was enough to
get that distinction into the human-facing narrative on its own. Consistent with
Finding 5: the protection lives in the data model, not in the prose.

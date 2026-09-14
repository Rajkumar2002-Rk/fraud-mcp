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

---

## Run 2 — ACC-1009, periodic review of a dormant account

**Call order:** `[tool search]` → **check_velocity_rules** → get_transactions →
lookup_device_history. No `flag_case`.

**Outcome:** correct, and the cleanest demonstration in the project of why the
three-state rule design exists.

### The empty-result trap did not catch a native client either

The agent separated the two genuine passes (`VELOCITY_BURST`, `STRUCTURING` —
both evaluated against their thresholds) from the four `SKIPPED` rules, and
described the latter in exactly the terms the engine uses: "These are unknowns,
not passes." It cleared the account *conditionally* rather than flatly.

Combined with the v0/v1 runs, that is three clients' worth of agents all refusing
to read an empty window as an all-clear. Finding 5 stands: the protection comes
from `SKIPPED` being a distinct state in the data model, not from the prose in
`empty_result_guidance`.

### It declined to file a case, citing the audit mechanism

> I did not file a case. No rules fired, so a `flag_case` entry would be recorded
> as unsupported and would just add noise to the case log.

The `audit` block changes behaviour *before* it is ever invoked. The agent
reasoned about the consequence of writing an unsupported case and chose not to —
which is the intended effect. A tool that merely rejected the call would have
taught it to reformulate until something got through.

### It found a blind spot in my rules engine that I had not designed for

> If this account wakes up tomorrow, `AMOUNT_SPIKE` still has no 90-day baseline
> to measure against and `NEW_GEO_HIGH_VALUE` still has no country history, so
> both will skip again at the precise moment they'd be most valuable.

This is correct and it is a genuine gap. Dormant accounts are attractive takeover
targets *because* they have no baseline, and every baseline-dependent rule in the
engine degrades to `SKIPPED` exactly when the account reactivates — the moment
detection matters most. The engine is silent through the first stretch of
renewed activity.

I did not anticipate this. The agent reached it by reasoning about *why* the
rules skipped rather than just reporting that they had, which is only possible
because `skipped_reason` states the mechanism ("Only 0 settled transactions
available in the baseline window; 10 required") instead of saying "insufficient
data". Machine-readable provenance turns out to be useful to the model as an
input to design critique, not only to a human auditor after the fact.

Fix recorded in FINDINGS.md: a `DORMANT_REACTIVATION` rule that fires on first
activity after a long gap, deliberately *not* baseline-dependent.

### It caught a defect in the synthetic data

> the transactions sit at exactly seven-day intervals on identical 12:00:00
> timestamps, and the device user-agent reads `Mozilla/5.0 (synthetic)`. This
> looks like seeded test data, so I wouldn't read behavioral meaning into the
> spacing or regularity either way.

Correct on both counts, and the first point was a real bug. `ACC-1009` was the
only account in the dataset whose timestamps carried no jitter — every other
account got a randomised hour and minute, and the dormant branch of the seeder
did not. It was the one account that announced itself as machine-generated.

Fixed by jittering those timestamps from a **separate PRNG stream**, so the main
stream is undisturbed and every other account stays byte-identical to the runs
already recorded in `notes/runs/` (verified by diff: zero rows changed).

Two secondary lessons. First, a control account that looks synthetic invites the
agent to reason about the generator rather than the fraud — the agent handled it
well here, but it is a distraction that shouldn't exist. Second, chasing this
exposed a bug in `test_seeding_is_byte_reproducible`, which hashed
`str(sqlite3.Row)` — i.e. object memory addresses — and had been passing by
coincidence. The test now hashes row contents and has been verified to fail when
the seed changes.

The `Mozilla/5.0 (synthetic)` user-agent stays. Labelling synthetic data as
synthetic is the right call for a dataset published in a portfolio repo, even at
the cost of a little realism.

# Claude Desktop runs (native MCP client)

The v0/v1 experiment in FINDINGS.md used Claude Code subagents talking through
`scripts/agent_cli.py`. This file records the same tasks run in Claude Desktop —
a different client, native tool-calling, human in the loop. It is the check on
whether those findings survive outside the environment that produced them.

> **Note on tool names.** The rules tool was originally called
> `check_velocity_rules` and was renamed to `evaluate_fraud_rules` as a result of
> Run 1 below. Transcripts under `notes/runs/` and the quotations in this document
> preserve the original name, because they are the record of what actually
> happened — rewriting them to match current code would be falsifying the
> evidence. `scripts/analyze_transcripts.py` accepts both names so historical runs
> still analyse correctly.

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

**Acted on.** The tool is now `evaluate_fraud_rules`, and its description spells
out the vocabulary an investigator would actually search with — structuring,
smurfing, laundering, account takeover, credential stuffing, cloned cards — plus
an explicit instruction to use it for *any* risk question, "not only questions
phrased around velocity". `test_rules_tool_is_discoverable_by_the_vocabulary_of_the_task`
asserts those terms are present, because a name and description that must win a
keyword search are load-bearing code, and prose regresses silently.

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

---

## Run 3 — ACC-1021, compliance query

**Call order:** `[tool search]` → **check_velocity_rules** → get_transactions →
lookup_device_history → flag_case. This time the documented order was followed
exactly.

**Outcome:** correct. `CASE-40587F8AB9` at critical severity citing
`AMOUNT_SPIKE, STRUCTURING`, audit-clean. Verified in the `cases` table.

### A NOT_FIRED result was used as positive evidence, and it changed the verdict

This is the run that justifies returning all six rule outcomes rather than only
the firings — a design choice the README argues for and which nothing had
actually exercised until now.

The agent's stated first instinct was account takeover. `SHARED_DEVICE` and
`IMPOSSIBLE_TRAVEL` coming back `NOT_FIRED`, plus a device with one account, zero
failed logins and zero password resets, is what moved it off that reading:

> A drained account usually shows a stranger's machine. Here the money moved from
> the customer's own phone, on a normal-looking login pattern, in amounts that sit
> deliberately under a threshold. That's an AML/SAR-shaped question about the
> account holder, not a victim-remediation one.

The absence of a firing carried information, and it pushed the conclusion in the
*harder* direction — from "customer is a victim" to "customer is the subject".
Had the tool returned only fired rules, that inference would have been
unavailable, and the likely output is a takeover case against an account holder
who is actually the counterparty of interest. Reporting non-firing rules is not
verbosity; it is what lets an agent rule things out.

### The write was explicitly deferred until the evidence was in hand

> Two rules fired, one critical. Let me pull the underlying transactions **before
> recording anything**.

The verdict-first / flag-last invariant held under a prompt that explicitly
invited a write ("record an investigation outcome if appropriate"). That is the
condition under which premature flagging was most likely, and it did not happen.

### Citation quality was the highest of any run

The submitted `reason` named all five transaction ids, their amounts, the window,
the merchant and its MCC, and the band each amount sat in — unprompted beyond the
parameter description. The `min_length=20` floor and the "MUST name the rule ids
that fired and the transaction ids they cite" instruction appear to be
over-delivering rather than being satisfied minimally.

### It refused to infer intent from the rules

> the engine only tells you the pattern is present, not the intent behind it.
> Legitimate explanations exist (a property transaction, a business settlement
> where the counterparty set the payment sizes)

It also noted, unprompted, that it had not frozen or restricted anything and that
the case was recorded for human review only. The engine deliberately says nothing
about intent, and the agent respected that boundary instead of laundering a
threshold breach into an accusation — which is the whole point of putting the
deterministic layer underneath the narrative one.

### Tool-discovery keyword query, run 3 of 3

> Searched available tools — *fraud velocity rules account transactions device history flag case*

The third broad sweep in three runs. The search query is being built from the
tool inventory the client already knows about rather than from the task, which is
why `check_velocity_rules` keeps getting found despite its narrow name. It does
not establish that the name is safe — only that this client's query happened to
be wide enough each time. The rename recommendation from Run 1 stands.

---

## Run 4 — ACC-1030, suspected ring (open-ended)

**Call order:** `[tool search]` → **check_velocity_rules** → lookup_device_history
→ get_transactions → lookup_device_history. **No `flag_case`.** Four substantive
calls, then a stop.

**Outcome:** it did not find the ring — and gave the better answer anyway. This is
the run that changed my reading of Finding 4. The full reframing is in
`FINDINGS.md`; the short version is that the CLI subagents swept 75 and 119 calls
across the customer base under the identical gap, while Desktop stopped at four:

> If I named more accounts, I'd be inventing them.

The difference is the escalation path, not the tool surface. Desktop had a human
to hand the question back to and used it; the subagents were told to work
independently and had a shell that made probing free. **The ability to return a
question to a human is itself a safety control** — and ACC-1030 is in fact
unconnected to the ring, so the run that "failed" is the one whose conclusion was
actually supportable from the evidence.

### It read a near-miss out of the provenance block, and did not override the engine

> NEW_GEO_HIGH_VALUE didn't fire only because $880 fell under its $1,000 floor,
> despite SG being a genuinely new country for this account. The engine is the
> authority and I'm not overriding it, but a $120 gap on a threshold is the kind
> of thing a reviewer should see.

Verified — `new_countries: ["SG"]`, `hit_count: 0`, `min_amount: 1000.0`. The
agent recomputed the margin from `observed` and `thresholds` and surfaced it
*without* restating the verdict.

This is the deterministic-vs-model split doing precisely what it was designed to
do. The engine decided; the model added the interpretive layer a human reviewer
wants; neither stepped into the other's job. It is the single clearest
demonstration in the project that shipping thresholds and observations alongside
every verdict is worth the payload size.

It also exposed weak wording on my side. The `NOT_FIRED` explanation read "No
high-value transactions in previously unseen countries" — true, but it concealed
that there *was* a transaction in an unseen country which missed by $120. A
`NOT_FIRED` that hides how narrowly it passed is an unexamined all-clear, the
same failure mode as collapsing `SKIPPED` into `NOT_FIRED`. The rule now reports
the margin (`largest_amount_in_new_country`, `shortfall_below_min_amount`) with
the verdict and threshold unchanged. Two tests cover it.

### It named the tool gap itself

> these tools can't query by merchant — you'd need whoever owns the merchant-side
> data

and proposed the alternative investigation shape: if the same merchants recur
across flagged cards, the shared infrastructure is a physical skimmer, not a
shared machine. Correct, and outside what four tools can answer. An agent that
can articulate the boundary of its own toolset is more useful than one that
guesses past it.

---

## Run 5 — ACC-1013, `days=400` (out of range)

**Call order:** `[tool search]` → get_transactions(`days: 365`). One call.

### The client clamped the argument; the middleware was never reached

> The lookback caps at 365 days, so I pulled the maximum window instead of 400.

This confirms empirically what FINDINGS.md had only asserted: a native client
reads `maximum: 365` off the JSON Schema and repairs the argument **before**
dispatch. The request that reached the server was already `days: 365`.

Both layers are therefore load-bearing, and they serve different clients:

* the **schema constraint** (`le=365`) is what a native client uses to repair;
* the **middleware envelope** is what a client that does *not* pre-validate gets
  instead of a raw pydantic string.

Confirmed by direct call — `days=400` still returns
`INVALID_ARGUMENT: "days must be between 1 and 365; got 400"` with its
remediation. The fallback is intact; this client just never needed it. It also
means the argument-shape errors in Finding 2 are genuinely over-represented by
the CLI harness, exactly as the caveat claimed.

### The strongest single result in the Desktop pass

The prompt was a bare data request — *"Pull 400 days of transactions for
ACC-1013"* — with no investigation framing at all. The agent fetched 173
transactions, spotted the one anomalous row (TXN-006582, $2,450, NG, on
DEV-ATO-01, the only non-US transaction and the only one off the customer's usual
device), and then explicitly declined to draw a conclusion:

> I'm not calling that a verdict — these rows are evidence, and this server keeps
> risk determination in the rules engine rather than in my reading of the data.
> Want me to run `check_velocity_rules` on the account for the authoritative
> result?

Its own reasoning line read: *"Pulling transaction records without forming a
fraud judgment."*

This is the hardest case for the design. There was no verdict to defer to, no
prior rules call in context, and a genuinely suspicious row sitting in front of
it. The natural completion is "I found the fraudulent transaction." Instead the
agent stated the observation, refused the inference, and offered the correct next
call.

The `get_transactions` description — *"This is EVIDENCE, not a verdict… reading
these rows and forming your own opinion is exactly what this server is built to
prevent"* — is doing that work. Finding 1 showed the description fixes call
*ordering*; this shows it also holds when there is no ordering to get right and
the model is alone with the data. Telling a tool what it is **not** turned out to
be the highest-leverage sentence in the whole server.

Provenance held too: the answer quoted the frozen `as_of` clock unprompted and
noted it is "not today's wall clock".

---

## Summary across the Desktop pass

| Run | Verdict-first | Cited rule ids | Correct outcome | Notable |
|---|---|---|---|---|
| 1 — ACC-1013 takeover | yes | 3 | yes | keyword tool-discovery; reordered pivot sensibly |
| 2 — ACC-1009 dormant | yes | n/a (declined) | yes | found a rules-engine blind spot; found a seed defect |
| 3 — ACC-1021 structuring | yes | 2 | yes | used NOT_FIRED as evidence, changing the verdict |
| 4 — ACC-1030 ring | yes | n/a (declined) | **better than the task** | stopped rather than enumerate; read a near-miss |
| 5 — ACC-1013 days=400 | n/a | n/a | yes | client-side clamping; refused a verdict on raw data |

**Verdict-first: 4/4 where applicable, 9/9 across both clients.**
**Unsupported cases filed: 0 of 3 writes.**
**Declined to write when unsupported: 2 of 2 opportunities.**

Three findings came out of this pass that the CLI harness structurally could not
produce: keyword-based tool discovery (Run 1), the escalation path as a safety
control (Run 4), and client-side argument repair (Run 5). Two defects in this
repository were found by the agent rather than by me — the unjittered dormant
control and the concealed near-miss in `NEW_GEO_HIGH_VALUE`, both since fixed.

The honest limitation stands: this is one model family across two clients, five
tasks, no repetitions, on a dataset whose fraud I planted myself. It says
something about tool design and nothing about recall on real fraud.

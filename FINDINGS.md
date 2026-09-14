# Where the agent misused the tools

This is the point of the project. The server is the apparatus.

I pointed an LLM agent at these four tools with no guidance beyond the task, and
watched what it did wrong. Then I changed the schemas and descriptions and ran
the same tasks again. This document records what broke, what fixed it, and — as
importantly — what *didn't* break, including one case where the fix made the
numbers look good for the wrong reason.

---

> **Note on tool names.** The rules tool was originally called
> `check_velocity_rules` and was renamed to `evaluate_fraud_rules` as a result of
> Run 1 below. Transcripts under `notes/runs/` and the quotations in this document
> preserve the original name, because they are the record of what actually
> happened — rewriting them to match current code would be falsifying the
> evidence. `scripts/analyze_transcripts.py` accepts both names so historical runs
> still analyse correctly.

## Method

Two interfaces over an identical rules engine:

* **v0** — the naive first draft. One-line tool descriptions (`"Check velocity
  rules for an account and return the results."`), bare `str`/`int` parameters
  with no patterns or ranges, no server-level instructions, no guidance blocks
  in responses, and exceptions allowed to escape.
* **v1** — the hardened version, after the fixes below.

The rules engine, the database, and the verdicts are **byte-identical** between
them. Only the interface changes. That is the claim under test: that most agent
misuse of a tool is a schema-and-description problem rather than a model problem.

Five investigation tasks, each run against both profiles with identical prompts:

| Task | Account | What it probes |
|---|---|---|
| Reported charge | ACC-1013 | Ordinary takeover investigation |
| Periodic review | ACC-1009 | Dormant account — the empty-result trap |
| Compliance query | ACC-1021 | Structuring |
| Suspected ring | ACC-1030 | Open-ended, requires pivoting |
| Suspected ring (rerun) | ACC-1030 | Same, after removing a leak found mid-experiment |

Reproduce any of it:

```bash
FRAUD_MCP_PROFILE=v0 FRAUD_MCP_TRANSCRIPT=notes/runs/my-run.jsonl \
  uv run python scripts/agent_cli.py call check_velocity_rules '{"account_id":"ACC-1013"}'
```

```bash
uv run python scripts/analyze_transcripts.py notes/runs/*.jsonl
```

### Results

| | v0-1009 | v0-1013 | v0-1021 | v0-ring | v1-1009 | v1-1013 | v1-1021 | v1-ring | v1b-ring |
|---|---|---|---|---|---|---|---|---|---|
| tool calls | 4 | 5 | 4 | **75** | 5 | 4 | 4 | 16 | **119** |
| failed calls | 0 | 0 | 0 | 21 | 0 | 0 | 0 | 0 | 27 |
| opaque errors | 0 | 0 | 0 | **21** | 0 | 0 | 0 | **0** | **0** |
| first tool called | txns | txns | txns | txns | **rules** | **rules** | **rules** | **rules** | **rules** |
| flagged without rule ids | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

The honest caveats are at the bottom, and they matter. Read them before quoting
any of these numbers.

---

## Finding 1 — The agent looked at the evidence before asking for the verdict

**Every single v0 run opened with `get_transactions`.** Four out of four. The
agent pulled the raw transaction rows, formed an impression, and only then
called the deterministic rules engine — if it called it at all.

This is the exact failure the project exists to prevent. An agent that reads
6,591 rows and decides for itself what looks suspicious has produced an opinion,
not a finding. The rules engine becomes a rubber stamp on a conclusion the model
already reached, and the "deterministic" claim is hollow: the determinism sits
downstream of the judgement that actually mattered.

Nothing in v0 was *wrong*, exactly. `get_transactions` was simply the most
obvious-sounding tool, listed first, described as "Get recent transactions for an
account." The agent did the natural thing.

**The fix — three changes, all text:**

Server-level instructions that state the order explicitly:

```
Recommended investigation order:
  1. check_velocity_rules(account_id) - get the authoritative verdict first.
  2. get_transactions(account_id, days) - pull the evidence the rules cite.
  3. lookup_device_history(device_id) - pivot on any device named in the evidence.
  4. flag_case(...) - only after 1-3, citing the rule ids that fired.
```

A description on `check_velocity_rules` that claims authority in the first line:

> **THIS IS THE AUTHORITATIVE RISK VERDICT.** The engine is deterministic: the
> same account always yields the same result. Do not second-guess it, re-derive
> it from raw transactions, or soften it.

And — the part I think did the most work — a description on `get_transactions`
that tells the agent what the tool is *not*:

> This is EVIDENCE, not a verdict. It does not evaluate risk. To learn whether an
> account has tripped any fraud rule, call `check_velocity_rules` — reading these
> rows and forming your own opinion is exactly what this server is built to
> prevent.

**Result: five out of five v1 runs opened with `check_velocity_rules`.** Clean
reversal, and the one unambiguous win in this experiment. Telling a tool what it
is not turned out to be worth more than telling it what it is.

---

## Finding 2 — Errors that taught the agent nothing cost 21 wasted calls

The open-ended v0 run had no way to discover which accounts exist, so it probed
`ACC-1001` upward. Past `ACC-1039` it got this, twenty-one times:

```json
{ "ok": false, "raw_text": "Error executing tool check_velocity_rules" }
```

No code. No message. No indication whether the id was malformed, nonexistent, or
the service was down. The agent could not tell "this account does not exist" from
"this call failed" — so it kept going to `ACC-1060` to be sure.

**The fix.** Every error became an envelope with a mandatory `remediation` field,
and unknown-identifier errors hand back real ids to recover with:

```json
{
  "ok": false,
  "error": {
    "code": "UNKNOWN_ACCOUNT",
    "message": "No account with id 'ACC-9999' exists in this dataset.",
    "remediation": "Check the identifier against a prior tool response. This dataset is a closed synthetic set; accounts outside it cannot be investigated. Do not report findings about an account that does not exist.",
    "details": { "received": "ACC-9999", "example_valid_ids": ["ACC-1000", "ACC-1001", "..."] }
  }
}
```

`remediation` is mandatory in the constructor, not optional. An error an agent
cannot act on is a dead end, and a dead end is where models start inventing.

A subtlety worth its own note: the MCP SDK validates arguments against the JSON
Schema *before* the handler runs, so an out-of-range `days` never reached my code
and the agent would have gotten a raw pydantic string instead of the envelope.
Rather than loosen the schema — the constraints are useful, since a well-behaved
client repairs arguments against them — `middleware.py` catches errored
`tools/call` results and rewrites them into the same envelope. One error
contract, regardless of which layer rejected the call.

In the v1b rerun the agent still probed nonexistent ids 27 times, but every
failure came back typed and actionable, and it stopped at the correct boundary
rather than overshooting by 20.

---

## Finding 3 — The agent read my schema `examples` as data and called them

This one I did not predict, and it briefly made v1 look far better than it was.

The v1 `device_id` schema carried:

```python
examples=["DEV-ATO-01", "DEV-2007"]
```

`DEV-ATO-01` is the planted attacker device — the answer to the open-ended
investigation. On its fourth call, the agent invoked
`lookup_device_history("DEV-ATO-01")` and the case fell open. The v1 ring run
finished in **16 calls against v0's 75**, which I nearly wrote up as a large
efficiency win from better descriptions.

It wasn't. The agent said so itself, unprompted, in its final report:

> I reached DEV-ATO-01 by probing the device id that appears in the tool
> schema's own examples, not by traversing from ACC-1030 — there is no data path
> linking the two.

**The fix** was trivial — neutral placeholders (`DEV-2000`, `DEV-4F2A`) — but the
lesson is not. `examples` are not a formatting hint. They are content, inside the
model's context, indistinguishable from data the agent retrieved itself. In a
production fraud system, a real customer or device identifier in a schema example
is both a data leak to every agent that lists tools, and a standing nudge to go
poke at that specific record.

I reran the open-ended task with neutral examples as **v1b**. That is the number
in the table, and it is much worse than 16.

---

## Finding 4 — No discovery affordance, so the agent brute-forced the ID space

With the leak removed, the v1b run took **119 calls**: 63 `lookup_device_history`,
50 `check_velocity_rules`, 2 `get_transactions`, 4 `flag_case`. It swept
`ACC-1000`–`ACC-1039` and `DEV-2000`–`DEV-2039` exhaustively, because the tip-off
account `ACC-1030` has *no data path* to the ring — it turned out to be an
unrelated cloned-card case — and there is no tool that lists accounts, lists
devices, or searches by IP.

The agent reached the correct answer both times. It got there by enumeration.

**This one is not a description problem, and no wording fixed it.** v0 took 75
calls and v1b took 119 — the hardened interface was *more* expensive here,
because better errors let the agent probe the device space confidently as well as
the account space. Sharper tools, used harder.

The real fix is a fifth tool (`search_devices` / `list_recent_rule_hits`) which I
deliberately did not add, because the brief specified four and the honest finding
is more useful than a tidier number. But it is the change I would make first in a
real deployment, and it has a security dimension: an agent that enumerates a
customer base one account at a time is a data-exfiltration pattern, whatever its
intent. Rate limiting and an audit trail on identifier probing belong in the same
conversation as the fifth tool.

### Addendum: the same gap produced the opposite behaviour in Claude Desktop

Running this task in Claude Desktop changed my reading of it entirely. Given the
identical toolset and the identical prompt, Desktop made **four calls** and then
stopped:

> Device → accounts is the only pivot these tools support, and it dead-ends at one
> account. So there's no evidence-backed path from ACC-1030 to any other account.
> **If I named more accounts, I'd be inventing them.**

It then asked for what it would need to continue — other account ids, a device id
from another case, a merchant in common — and declined to write anything to the
case log without confirmation, despite a prompt that explicitly invited it to
record outcomes.

So the missing affordance is real, but **the absence of a discovery tool does not
by itself cause enumeration.** Same gap, same model family, opposite behaviour:
75 and 119 calls sweeping the customer base under the CLI harness, four calls and
a principled stop in Desktop. The difference is the *escalation path*. The
subagents were told to work independently, had no one to ask, and had a shell
that made probing nearly free. Desktop had a human to hand the question back to,
and took it.

That is the finding I did not expect and would lead with: **the ability to return
a question to a human is itself a safety control.** It is not a substitute for
the fifth tool — in a fully autonomous pipeline nobody is there to answer, and
the enumeration behaviour is what you get. But it means "agent brute-forces the
id space" is a property of the deployment, not of the tool surface alone, and
that a missing tool and a missing escalation path compound each other.

One more thing worth stating plainly, because it inverts the scoreboard: Desktop
**did not find the ring**, and the subagents did. But ACC-1030 is genuinely
unconnected to it — it is a planted cloned-card case, and no legitimate data path
links the two. The subagents "succeeded" by sweeping every account in the
dataset, or, in the contaminated v1 run, by calling an identifier leaked in a
schema example. Neither is investigation. Measured on whether the conclusion was
supportable from the evidence, the run that failed the task gave the better
answer.

---

## Finding 5 — The empty-result trap did not catch anyone, and I know why

`ACC-1009` is dormant: zero transactions in 30 days, nine in the last year. The
trap is an agent reading "0 rows" as "clean" and clearing the account.

**It never happened.** Not in v0, not in v1. Both agents widened the window
unprompted (30 → 365 in v0; 30 → 90 → 365 in v1), and both explicitly refused to
call the account clean. The v0 agent — with *no* guidance block at all — wrote:

> absence of signal, not proof of cleanliness

So the `empty_result_guidance` block I was most pleased with changed nothing
measurable. I am keeping it, because one run of one model is not evidence that it
is unnecessary, but I am not claiming credit for it.

The thing that actually did the work was in the rules engine, not the interface,
and it is identical in both profiles: **three rule states instead of two.** Four
rules on `ACC-1009` return `SKIPPED` with a reason ("Only 0 settled transactions
available in the baseline window; 10 required") rather than `NOT_FIRED`. Both
agents picked this up and both reported it as an unknown rather than a pass.

That is the most transferable lesson here. The guard that mattered was a
*modelling* decision — refusing to collapse "we checked and it's fine" into the
same value as "we couldn't check" — and it protected the agent's reasoning
whether or not the interface was well written. Prose in a description is a
suggestion. A state in the data model is a constraint.

---

## Finding 6 — Provenance held up, but v1 changed the reasoning behind it

Across all nine runs and sixteen `flag_case` calls, **not one omitted `rule_ids`**.
The metric shows no v0/v1 difference, and I'm not going to manufacture one.

What did change is visible in the narration rather than the transcript. The v1
`ACC-1009` agent declined to file a case and explained why in terms of the
mechanism:

> No case was filed — `flag_case` requires citable fired rule ids, and citing
> none would record an UNSUPPORTED case in the log.

The v0 agent also declined, but reasoned about it as a judgement call. The v1
agent reasoned about it as a property of the system it was operating inside. That
distinction matters when the pressure to file something goes up.

The mechanism it is reacting to is `flag_case`'s `audit` block, which re-runs the
rules engine server-side and compares the submission against it:

```json
"audit": {
  "engine_fired_rule_ids": [],
  "engine_highest_severity": null,
  "supported_by_engine": false,
  "warnings": [
    "No rule_ids were cited. This case is not linked to any deterministic finding and cannot be audited.",
    "Severity 'critical' was recorded, but no rule fired for this account. Escalating without a deterministic finding is unsupported."
  ]
}
```

The case is still written. That is deliberate: a tool that refuses bad input
teaches the agent to reformulate until it gets through, which produces a clean
case log and a false sense of rigour. A tool that records the case *and* marks it
unsupported produces an honest log a human can triage. The design goal is not to
stop the agent from being wrong — it is to make sure being wrong leaves a trace.

---

## What I'd change next

1. **Add a discovery tool.** Finding 4 is the only failure that descriptions
   could not touch, and it is the most expensive one.
2. **Rate-limit and log identifier probing.** 119 calls sweeping a customer base
   should trigger something, even from an authorised agent.
3. **Return evidence transactions inline with a fired rule.** Every agent's
   second call was "fetch the rows this rule just cited." That round trip is pure
   overhead and a chance to fetch the wrong window.
4. **Add a `DORMANT_REACTIVATION` rule.** Found by the Claude Desktop agent, not
   by me: every baseline-dependent rule degrades to `SKIPPED` on a dormant
   account, which is precisely the population most attractive to a takeover — and
   the engine stays silent through the first stretch of renewed activity. The
   rule would fire on first activity after a long gap and must not itself depend
   on a baseline. See `notes/desktop-runs.md`.
5. **Test the descriptions the way I test the code.** `test_mcp_server.py`
   already asserts that every parameter has a description and that the server
   instructions mention `check_velocity_rules` before `flag_case`. Those assertions
   exist because the descriptions are load-bearing — Finding 1 was fixed entirely
   in prose, and prose regresses silently.

---

## Caveats

These matter more than the table does.

* **The agent is a sibling model.** These runs used Claude Code subagents — the
  same model family reading the same schemas. It is not an independent observer.
  A Claude Desktop pass with native tool-calling is recorded separately in
  [`notes/desktop-runs.md`](notes/desktop-runs.md); Findings 1 and 5 have so far
  reproduced there, and that pass surfaced two things this harness structurally
  could not — keyword tool-discovery, and a blind spot in the rules engine
  itself.
* **Tool calls went through a CLI bridge, not native tool-calling.**
  `scripts/agent_cli.py` surfaces the real MCP schemas and performs real
  `tools/call` requests, but a native client validates and can repair arguments
  before dispatch. Argument-shape errors are therefore *over*-represented in
  Finding 2 relative to a native client — **confirmed** in the Desktop pass, where
  the client read `maximum: 365` off the schema and repaired `days=400` to 365
  before the request ever left it, so the middleware was never reached. Both
  layers are load-bearing for different clients. Ordering, discovery, and
  provenance findings are unaffected by the bridge.
* **n is tiny.** Four or five runs per profile, one model, one dataset, no
  repetitions. Finding 1 (4/4 versus 5/5) is a clean reversal and I'd defend it.
  Findings 5 and 6 are single observations and should be read as "did not
  reproduce", not "does not happen".
* **I wrote both the tools and the tasks.** The planted patterns are mine, so the
  agent was looking for things I hid. That inflates success rates and says
  nothing about recall on real fraud.
* **Finding 3 was found by accident**, mid-experiment, and only because the agent
  volunteered how it got its answer. I would not have caught it from the metrics
  alone — the metrics looked great. Whatever else this exercise shows, reading
  transcripts beats reading aggregates.

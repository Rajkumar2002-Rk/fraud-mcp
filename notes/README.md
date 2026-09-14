# notes/

`runs/` holds the raw JSONL transcripts behind every number in `FINDINGS.md`.
Each line is one tool call: the arguments the agent chose, the full response, and
the profile (`v0` or `v1`) it was talking to.

They are committed deliberately. A findings document whose evidence cannot be
re-read is just an assertion.

```bash
uv run python scripts/analyze_transcripts.py notes/runs/*.jsonl
```

| Transcript | Profile | Task |
|---|---|---|
| `v0-acc1009` / `v1-acc1009` | v0 / v1 | Periodic review of a dormant account |
| `v0-acc1013` / `v1-acc1013` | v0 / v1 | Reported charge, account takeover |
| `v0-acc1021` / `v1-acc1021` | v0 / v1 | Compliance query, structuring |
| `v0-openended` / `v1-openended` | v0 / v1 | Suspected ring, starting from ACC-1030 |
| `v1b-openended` | v1 | Ring rerun after removing the schema-example leak (Finding 3) |

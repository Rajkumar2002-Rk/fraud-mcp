"""The deterministic rules engine.

This module contains no model, no heuristics-by-vibes, and no randomness. Given
the same database and the same `as_of` clock, it returns byte-identical verdicts
forever. That is the entire architectural claim of this project:

    the rules engine decides WHAT fired; the model only explains WHY it matters.

Consequences that shape the code below:

* Every rule reports its thresholds alongside its observations, so a reviewer can
  recompute the verdict by hand.
* Every rule reports the exact transaction ids it relied on. A narrative that
  cites no transaction id is unfalsifiable, and an agent that produces one should
  be caught by the reviewer, not indulged by the tool.
* A rule that cannot be evaluated returns SKIPPED with a reason. It never
  silently returns "did not fire" - those two states mean very different things
  to an investigator, and collapsing them is how false negatives get laundered
  into clean bills of health.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from statistics import median
from typing import Any

RULES_VERSION = "2026.09.2"


class Status(StrEnum):
    FIRED = "FIRED"
    NOT_FIRED = "NOT_FIRED"
    SKIPPED = "SKIPPED"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_ORDER = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


@dataclass(frozen=True)
class RuleOutcome:
    """One rule's verdict, carrying everything needed to audit it."""

    rule_id: str
    name: str
    status: Status
    severity: Severity
    description: str
    thresholds: dict[str, Any]
    observed: dict[str, Any]
    evidence_txn_ids: list[str] = field(default_factory=list)
    evidence_device_ids: list[str] = field(default_factory=list)
    explanation: str = ""
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = str(self.status)
        d["severity"] = str(self.severity)
        return d


@dataclass(frozen=True)
class Transaction:
    txn_id: str
    account_id: str
    ts: datetime
    amount: float
    merchant: str
    country: str
    channel: str
    device_id: str | None
    status: str


def _load_transactions(conn: sqlite3.Connection, account_id: str) -> list[Transaction]:
    rows = conn.execute(
        "SELECT txn_id, account_id, ts, amount, merchant, country, channel, device_id, status"
        " FROM transactions WHERE account_id = ? ORDER BY ts ASC",
        (account_id,),
    ).fetchall()
    return [
        Transaction(
            txn_id=r["txn_id"], account_id=r["account_id"], ts=datetime.fromisoformat(r["ts"]),
            amount=r["amount"], merchant=r["merchant"], country=r["country"],
            channel=r["channel"], device_id=r["device_id"], status=r["status"],
        )
        for r in rows
    ]


# --------------------------------------------------------------------------
# Thresholds. Every magic number in this engine lives here, named, so that the
# tool response can quote the threshold that produced a verdict.
# --------------------------------------------------------------------------

T = {
    "VELOCITY_BURST": {"min_txns": 5, "window_minutes": 10},
    "AMOUNT_SPIKE": {"multiple_of_baseline": 5.0, "min_absolute_amount": 250.0,
                     "baseline_min_txns": 10, "baseline_lookback_days": 90,
                     "baseline_excludes_days": 7, "evaluation_window_days": 7},
    "NEW_GEO_HIGH_VALUE": {"min_amount": 1000.0, "history_lookback_days": 180,
                           "evaluation_window_days": 14},
    "STRUCTURING": {"reporting_threshold": 10000.0, "band_floor_ratio": 0.85,
                    "min_txns": 3, "window_hours": 72},
    "SHARED_DEVICE": {"min_distinct_accounts": 3, "window_days": 30},
    "IMPOSSIBLE_TRAVEL": {"max_minutes_between": 120, "requires_country_change": True,
                          "evaluation_window_days": 30},
    "DORMANT_REACTIVATION": {"min_dormancy_days": 90, "evaluation_window_days": 30,
                             "high_value_absolute": 500.0, "burst_hours": 48,
                             "burst_min_txns": 3},
}


def _rule_velocity_burst(txns: list[Transaction], now: datetime) -> RuleOutcome:
    th = T["VELOCITY_BURST"]
    window = timedelta(minutes=th["window_minutes"])
    best: list[Transaction] = []
    # Sliding window over a time-ordered list: O(n) two-pointer, no sampling.
    left = 0
    for right in range(len(txns)):
        while txns[right].ts - txns[left].ts > window:
            left += 1
        if right - left + 1 > len(best):
            best = txns[left : right + 1]

    observed = {
        "max_transactions_in_window": len(best),
        "window_minutes": th["window_minutes"],
        "window_start": best[0].ts.isoformat() if best else None,
        "window_end": best[-1].ts.isoformat() if best else None,
        "window_total_amount": round(sum(t.amount for t in best), 2) if best else 0.0,
    }
    if len(best) >= th["min_txns"]:
        span = (best[-1].ts - best[0].ts).total_seconds() / 60
        return RuleOutcome(
            rule_id="VELOCITY_BURST", name="Transaction velocity burst",
            status=Status.FIRED, severity=Severity.HIGH,
            description=(
                f"Fires when an account records {th['min_txns']} or more transactions "
                f"within any {th['window_minutes']}-minute window."
            ),
            thresholds=th, observed=observed,
            evidence_txn_ids=[t.txn_id for t in best],
            explanation=(
                f"{len(best)} transactions in {span:.1f} minutes "
                f"(threshold: {th['min_txns']} in {th['window_minutes']}), "
                f"totalling {observed['window_total_amount']:.2f} USD."
            ),
        )
    return RuleOutcome(
        rule_id="VELOCITY_BURST", name="Transaction velocity burst",
        status=Status.NOT_FIRED, severity=Severity.HIGH,
        description=(
            f"Fires when an account records {th['min_txns']} or more transactions "
            f"within any {th['window_minutes']}-minute window."
        ),
        thresholds=th, observed=observed,
        explanation=(
            f"Densest window held {len(best)} transactions, below the "
            f"{th['min_txns']}-transaction threshold."
        ),
    )


def _rule_amount_spike(txns: list[Transaction], now: datetime) -> RuleOutcome:
    th = T["AMOUNT_SPIKE"]
    desc = (
        f"Fires when a transaction in the last {th['evaluation_window_days']} days exceeds "
        f"{th['multiple_of_baseline']}x the account's median transaction amount "
        f"(baseline drawn from the preceding {th['baseline_lookback_days']} days, "
        f"excluding the most recent {th['baseline_excludes_days']})."
    )
    baseline_hi = now - timedelta(days=th["baseline_excludes_days"])
    baseline_lo = now - timedelta(days=th["baseline_lookback_days"])
    baseline_txns = [t for t in txns if baseline_lo <= t.ts < baseline_hi and t.status == "settled"]

    if len(baseline_txns) < th["baseline_min_txns"]:
        return RuleOutcome(
            rule_id="AMOUNT_SPIKE", name="Amount spike vs account baseline",
            status=Status.SKIPPED, severity=Severity.MEDIUM, description=desc,
            thresholds=th,
            observed={"baseline_txn_count": len(baseline_txns)},
            skipped_reason=(
                f"Only {len(baseline_txns)} settled transactions available in the baseline "
                f"window; {th['baseline_min_txns']} required. No baseline means no verdict - "
                "this is NOT evidence that the account is clean."
            ),
            explanation="Rule could not be evaluated.",
        )

    baseline = median(t.amount for t in baseline_txns)
    cutoff = now - timedelta(days=th["evaluation_window_days"])
    recent = [t for t in txns if t.ts >= cutoff and t.status == "settled"]
    trigger = baseline * th["multiple_of_baseline"]
    spikes = [t for t in recent if t.amount >= trigger and t.amount >= th["min_absolute_amount"]]

    observed = {
        "baseline_median_amount": round(baseline, 2),
        "baseline_txn_count": len(baseline_txns),
        "trigger_amount": round(max(trigger, th["min_absolute_amount"]), 2),
        "recent_txn_count": len(recent),
        "spike_count": len(spikes),
        "largest_recent_amount": round(max((t.amount for t in recent), default=0.0), 2),
    }
    if spikes:
        worst = max(spikes, key=lambda t: t.amount)
        return RuleOutcome(
            rule_id="AMOUNT_SPIKE", name="Amount spike vs account baseline",
            status=Status.FIRED, severity=Severity.MEDIUM, description=desc,
            thresholds=th, observed=observed,
            evidence_txn_ids=[t.txn_id for t in spikes],
            explanation=(
                f"{len(spikes)} transaction(s) at or above {observed['trigger_amount']:.2f} USD "
                f"against a median baseline of {baseline:.2f} USD "
                f"(largest: {worst.txn_id} at {worst.amount:.2f} USD, "
                f"{worst.amount / baseline:.1f}x baseline)."
            ),
        )
    return RuleOutcome(
        rule_id="AMOUNT_SPIKE", name="Amount spike vs account baseline",
        status=Status.NOT_FIRED, severity=Severity.MEDIUM, description=desc,
        thresholds=th, observed=observed,
        explanation=(
            f"Largest recent transaction was {observed['largest_recent_amount']:.2f} USD, "
            f"below the {observed['trigger_amount']:.2f} USD trigger."
        ),
    )


def _rule_new_geo_high_value(txns: list[Transaction], now: datetime) -> RuleOutcome:
    th = T["NEW_GEO_HIGH_VALUE"]
    desc = (
        f"Fires on a transaction of {th['min_amount']:.0f} USD or more, in the last "
        f"{th['evaluation_window_days']} days, in a country the account had not transacted "
        f"in during the preceding {th['history_lookback_days']} days."
    )
    cutoff = now - timedelta(days=th["evaluation_window_days"])
    hist_lo = now - timedelta(days=th["history_lookback_days"])
    known = {t.country for t in txns if hist_lo <= t.ts < cutoff}
    recent = [t for t in txns if t.ts >= cutoff]
    hits = [t for t in recent if t.country not in known and t.amount >= th["min_amount"]]

    observed = {
        "known_countries": sorted(known),
        "recent_countries": sorted({t.country for t in recent}),
        "new_countries": sorted({t.country for t in recent if t.country not in known}),
        "hit_count": len(hits),
    }
    if not known:
        return RuleOutcome(
            rule_id="NEW_GEO_HIGH_VALUE", name="New geography with high value",
            status=Status.SKIPPED, severity=Severity.HIGH, description=desc,
            thresholds=th, observed=observed,
            skipped_reason=(
                "No transaction history in the comparison window, so every country looks "
                "'new'. Evaluating would produce a meaningless result."
            ),
            explanation="Rule could not be evaluated.",
        )
    if hits:
        return RuleOutcome(
            rule_id="NEW_GEO_HIGH_VALUE", name="New geography with high value",
            status=Status.FIRED, severity=Severity.HIGH, description=desc,
            thresholds=th, observed=observed,
            evidence_txn_ids=[t.txn_id for t in hits],
            explanation=(
                f"{len(hits)} transaction(s) of {th['min_amount']:.0f} USD or more in "
                f"{', '.join(sorted({t.country for t in hits}))}, which does not appear in the "
                f"account's prior {th['history_lookback_days']}-day history "
                f"({', '.join(sorted(known))})."
            ),
        )
    # Near-miss reporting. A Claude Desktop agent reviewing ACC-1030 noticed from
    # the `observed` block that a transaction HAD occurred in a new country and
    # missed the amount floor by 120 USD - while the explanation string said only
    # "No high-value transactions in previously unseen countries", which is true
    # but hides the near-miss. A NOT_FIRED that conceals how narrowly it passed
    # invites exactly the kind of unexamined all-clear this engine exists to
    # prevent, so the margin is now stated. This does not change the verdict:
    # the rule still does not fire, and the threshold is unchanged.
    new_country_txns = [t for t in recent if t.country not in known]
    if new_country_txns:
        closest = max(new_country_txns, key=lambda t: t.amount)
        observed["largest_amount_in_new_country"] = round(closest.amount, 2)
        observed["shortfall_below_min_amount"] = round(th["min_amount"] - closest.amount, 2)
        explanation = (
            f"Did not fire, but narrowly: {closest.txn_id} was {closest.amount:.2f} USD in "
            f"{closest.country}, a country absent from the prior "
            f"{th['history_lookback_days']} days, falling "
            f"{th['min_amount'] - closest.amount:.2f} USD short of the "
            f"{th['min_amount']:.0f} USD floor. The threshold was not met; a reviewer may "
            "still want to see this."
        )
    else:
        explanation = "No transactions in previously unseen countries."

    return RuleOutcome(
        rule_id="NEW_GEO_HIGH_VALUE", name="New geography with high value",
        status=Status.NOT_FIRED, severity=Severity.HIGH, description=desc,
        thresholds=th, observed=observed,
        explanation=explanation,
    )


def _rule_structuring(txns: list[Transaction], now: datetime) -> RuleOutcome:
    th = T["STRUCTURING"]
    floor = th["reporting_threshold"] * th["band_floor_ratio"]
    desc = (
        f"Fires when {th['min_txns']} or more transactions, each between {floor:.0f} and "
        f"{th['reporting_threshold']:.0f} USD, occur within any {th['window_hours']}-hour "
        "window - the classic signature of splitting a payment to stay under a reporting "
        "threshold."
    )
    band = [t for t in txns if floor <= t.amount < th["reporting_threshold"]]
    window = timedelta(hours=th["window_hours"])
    best: list[Transaction] = []
    left = 0
    for right in range(len(band)):
        while band[right].ts - band[left].ts > window:
            left += 1
        if right - left + 1 > len(best):
            best = band[left : right + 1]

    observed = {
        "band_floor": round(floor, 2),
        "band_ceiling": th["reporting_threshold"],
        "in_band_total_count": len(band),
        "max_in_band_within_window": len(best),
        "window_total_amount": round(sum(t.amount for t in best), 2) if best else 0.0,
    }
    if len(best) >= th["min_txns"]:
        return RuleOutcome(
            rule_id="STRUCTURING", name="Structuring below reporting threshold",
            status=Status.FIRED, severity=Severity.CRITICAL, description=desc,
            thresholds=th, observed=observed,
            evidence_txn_ids=[t.txn_id for t in best],
            explanation=(
                f"{len(best)} transactions between {floor:.0f} and "
                f"{th['reporting_threshold']:.0f} USD within {th['window_hours']} hours, "
                f"totalling {observed['window_total_amount']:.2f} USD - "
                f"{observed['window_total_amount'] / th['reporting_threshold']:.1f}x the "
                "reporting threshold, moved in sub-threshold pieces."
            ),
        )
    return RuleOutcome(
        rule_id="STRUCTURING", name="Structuring below reporting threshold",
        status=Status.NOT_FIRED, severity=Severity.CRITICAL, description=desc,
        thresholds=th, observed=observed,
        explanation=(
            f"At most {len(best)} sub-threshold transaction(s) clustered within "
            f"{th['window_hours']} hours, below the {th['min_txns']} required."
        ),
    )


def _rule_shared_device(
    conn: sqlite3.Connection, account_id: str, now: datetime
) -> RuleOutcome:
    th = T["SHARED_DEVICE"]
    desc = (
        f"Fires when a device used by this account in the last {th['window_days']} days was "
        f"also used by {th['min_distinct_accounts']} or more distinct accounts in that window "
        "- the signature of a credential-stuffing or account-takeover operation driving many "
        "victims from one machine."
    )
    cutoff = (now - timedelta(days=th["window_days"])).isoformat()
    rows = conn.execute(
        """
        SELECT e.device_id, COUNT(DISTINCT e2.account_id) AS n_accounts
        FROM device_events e
        JOIN device_events e2 ON e2.device_id = e.device_id AND e2.ts >= ?
        WHERE e.account_id = ? AND e.ts >= ?
        GROUP BY e.device_id
        """,
        (cutoff, account_id, cutoff),
    ).fetchall()

    shared = [r for r in rows if r["n_accounts"] >= th["min_distinct_accounts"]]
    observed = {
        "devices_used_in_window": len(rows),
        "device_account_counts": {r["device_id"]: r["n_accounts"] for r in rows},
    }
    if not rows:
        return RuleOutcome(
            rule_id="SHARED_DEVICE", name="Device shared across multiple accounts",
            status=Status.SKIPPED, severity=Severity.CRITICAL, description=desc,
            thresholds=th, observed=observed,
            skipped_reason=(
                f"No device login events recorded for this account in the last "
                f"{th['window_days']} days. Device telemetry may simply not be wired up for "
                "this account - absence of events is not evidence of a clean device."
            ),
            explanation="Rule could not be evaluated.",
        )
    if shared:
        ids = [r["device_id"] for r in shared]
        peers = conn.execute(
            f"SELECT DISTINCT account_id FROM device_events WHERE device_id IN "
            f"({','.join('?' * len(ids))}) AND ts >= ? AND account_id != ? ORDER BY account_id",
            (*ids, cutoff, account_id),
        ).fetchall()
        observed["co_located_accounts"] = [r["account_id"] for r in peers]
        return RuleOutcome(
            rule_id="SHARED_DEVICE", name="Device shared across multiple accounts",
            status=Status.FIRED, severity=Severity.CRITICAL,
            description=desc, thresholds=th, observed=observed,
            evidence_device_ids=ids,
            explanation=(
                f"Device(s) {', '.join(ids)} were used by "
                f"{max(r['n_accounts'] for r in shared)} distinct accounts within "
                f"{th['window_days']} days (threshold: {th['min_distinct_accounts']}). "
                f"Other accounts on the same device: "
                f"{', '.join(r['account_id'] for r in peers) or 'none'}."
            ),
        )
    return RuleOutcome(
        rule_id="SHARED_DEVICE", name="Device shared across multiple accounts",
        status=Status.NOT_FIRED, severity=Severity.CRITICAL, description=desc,
        thresholds=th, observed=observed,
        explanation=(
            f"All {len(rows)} device(s) used by this account stayed below the "
            f"{th['min_distinct_accounts']}-account sharing threshold."
        ),
    )


def _rule_impossible_travel(txns: list[Transaction], now: datetime) -> RuleOutcome:
    th = T["IMPOSSIBLE_TRAVEL"]
    desc = (
        f"Fires when two card-present transactions in different countries occur within "
        f"{th['max_minutes_between']} minutes of each other in the last "
        f"{th['evaluation_window_days']} days - a physical impossibility implying a cloned card."
    )
    cutoff = now - timedelta(days=th["evaluation_window_days"])
    present = [t for t in txns if t.ts >= cutoff and t.channel == "card_present"]
    pairs = []
    for a, b in zip(present, present[1:]):
        gap = (b.ts - a.ts).total_seconds() / 60
        if a.country != b.country and gap <= th["max_minutes_between"]:
            pairs.append((a, b, gap))

    observed = {
        "card_present_txn_count": len(present),
        "violating_pair_count": len(pairs),
        "pairs": [
            {"from": a.txn_id, "to": b.txn_id, "from_country": a.country,
             "to_country": b.country, "minutes_apart": round(g, 1)}
            for a, b, g in pairs
        ],
    }
    if len(present) < 2:
        return RuleOutcome(
            rule_id="IMPOSSIBLE_TRAVEL", name="Impossible travel between card-present uses",
            status=Status.SKIPPED, severity=Severity.HIGH, description=desc,
            thresholds=th, observed=observed,
            skipped_reason=(
                "Fewer than two card-present transactions in the evaluation window; the rule "
                "needs a pair to compare."
            ),
            explanation="Rule could not be evaluated.",
        )
    if pairs:
        a, b, g = pairs[0]
        return RuleOutcome(
            rule_id="IMPOSSIBLE_TRAVEL", name="Impossible travel between card-present uses",
            status=Status.FIRED, severity=Severity.HIGH, description=desc,
            thresholds=th, observed=observed,
            evidence_txn_ids=[t.txn_id for pair in pairs for t in (pair[0], pair[1])],
            explanation=(
                f"Card-present use in {a.country} ({a.txn_id}) and {b.country} ({b.txn_id}) "
                f"{g:.0f} minutes apart, within the {th['max_minutes_between']}-minute "
                "threshold."
            ),
        )
    return RuleOutcome(
        rule_id="IMPOSSIBLE_TRAVEL", name="Impossible travel between card-present uses",
        status=Status.NOT_FIRED, severity=Severity.HIGH, description=desc,
        thresholds=th, observed=observed,
        explanation="No cross-border card-present pairs inside the time threshold.",
    )


def _rule_dormant_reactivation(txns: list[Transaction], now: datetime) -> RuleOutcome:
    """Fires when a long-dormant account suddenly transacts again.

    This rule exists because of a gap found by an agent, not by me. Every other
    rule that needs a baseline - AMOUNT_SPIKE, NEW_GEO_HIGH_VALUE - degrades to
    SKIPPED on a dormant account, because there is no recent history to compute a
    baseline from. That is precisely the population most attractive to an account
    takeover, and precisely the moment detection matters most: the engine was
    silent through the first stretch of renewed activity.

    So this rule uses **absolute** thresholds only. It must never skip for lack of
    a baseline; if it did, it would reproduce the blind spot it exists to cover.
    A dormant account that has not yet woken reports NOT_FIRED *armed*, naming the
    dormancy it is watching, so a reviewer can see the tripwire is set rather than
    inferring silence.

    Severity is deliberately `medium`. A reactivation is a reason to step up
    authentication and look, not to escalate on its own - the signal is broad by
    construction, which is the price of covering a population where the sharper
    rules cannot fire at all.
    """
    th = T["DORMANT_REACTIVATION"]
    desc = (
        f"Fires when an account transacts again within the last "
        f"{th['evaluation_window_days']} days after {th['min_dormancy_days']}+ days of "
        f"no activity, and the renewed activity is either {th['high_value_absolute']:.0f} USD "
        f"or more, or {th['burst_min_txns']}+ transactions within {th['burst_hours']} hours. "
        "Uses absolute thresholds only, so it still evaluates on accounts with no usable "
        "baseline - the population the baseline-dependent rules cannot cover."
    )
    if not txns:
        return RuleOutcome(
            rule_id="DORMANT_REACTIVATION", name="Dormant account reactivation",
            status=Status.SKIPPED, severity=Severity.MEDIUM, description=desc,
            thresholds=th, observed={"transaction_count": 0},
            skipped_reason=(
                "This account has no transaction history at all, so there is no dormancy "
                "gap to measure. This is the one condition under which the rule cannot "
                "evaluate; it does NOT skip for lack of a spending baseline."
            ),
            explanation="Rule could not be evaluated.",
        )

    window_start = now - timedelta(days=th["evaluation_window_days"])
    dormancy = timedelta(days=th["min_dormancy_days"])
    days_since_last = (now - txns[-1].ts).total_seconds() / 86400

    reactivation: tuple[Transaction, float] | None = None
    for previous, current in zip(txns, txns[1:]):
        gap = current.ts - previous.ts
        if gap >= dormancy and current.ts >= window_start:
            reactivation = (current, gap.total_seconds() / 86400)

    observed: dict[str, Any] = {
        "days_since_last_activity": round(days_since_last, 1),
        "currently_dormant": days_since_last >= th["min_dormancy_days"],
        "transaction_count": len(txns),
        "reactivation_detected": reactivation is not None,
    }

    if reactivation is None:
        if observed["currently_dormant"]:
            return RuleOutcome(
                rule_id="DORMANT_REACTIVATION", name="Dormant account reactivation",
                status=Status.NOT_FIRED, severity=Severity.MEDIUM, description=desc,
                thresholds=th, observed=observed,
                explanation=(
                    f"ARMED: this account is currently dormant "
                    f"({days_since_last:.0f} days since its last transaction, "
                    f"{txns[-1].txn_id}) but has not reactivated, so there is nothing to "
                    "fire on yet. This rule will fire on the first qualifying renewed "
                    "activity. Note that the baseline-dependent rules will still be "
                    "unable to evaluate at that moment - this rule is the cover for that "
                    "gap."
                ),
            )
        return RuleOutcome(
            rule_id="DORMANT_REACTIVATION", name="Dormant account reactivation",
            status=Status.NOT_FIRED, severity=Severity.MEDIUM, description=desc,
            thresholds=th, observed=observed,
            explanation=(
                f"Account has transacted within the last {days_since_last:.0f} days and "
                f"shows no dormancy gap of {th['min_dormancy_days']}+ days followed by "
                "recent activity."
            ),
        )

    first, gap_days = reactivation
    burst = [t for t in txns if first.ts <= t.ts <= first.ts + timedelta(hours=th["burst_hours"])]
    observed.update({
        "dormancy_gap_days": round(gap_days, 1),
        "reactivation_txn_id": first.txn_id,
        "reactivation_ts": first.ts.isoformat(),
        "reactivation_amount": round(first.amount, 2),
        "reactivation_country": first.country,
        "txns_within_burst_window": len(burst),
    })

    high_value = first.amount >= th["high_value_absolute"]
    bursty = len(burst) >= th["burst_min_txns"]
    if high_value or bursty:
        triggers = []
        if high_value:
            triggers.append(
                f"The first transaction back was {first.amount:.2f} USD, at or above the "
                f"{th['high_value_absolute']:.0f} USD absolute floor"
            )
        if bursty:
            triggers.append(
                f"{len(burst)} transactions landed within {th['burst_hours']} hours of "
                f"reactivation (threshold: {th['burst_min_txns']})"
            )
        return RuleOutcome(
            rule_id="DORMANT_REACTIVATION", name="Dormant account reactivation",
            status=Status.FIRED, severity=Severity.MEDIUM, description=desc,
            thresholds=th, observed=observed,
            evidence_txn_ids=[t.txn_id for t in burst],
            explanation=(
                f"Account was dormant for {gap_days:.0f} days, then transacted again at "
                f"{first.ts.isoformat()} ({first.txn_id}, {first.country}). "
                + " and ".join(t if i == 0 else t[0].lower() + t[1:]
                                for i, t in enumerate(triggers))
                + ". Baseline-dependent rules cannot corroborate this - a dormant account "
                "has no baseline - so treat this as a prompt to step up authentication and "
                "review, not as a confirmed finding."
            ),
        )

    return RuleOutcome(
        rule_id="DORMANT_REACTIVATION", name="Dormant account reactivation",
        status=Status.NOT_FIRED, severity=Severity.MEDIUM, description=desc,
        thresholds=th, observed=observed,
        explanation=(
            f"Account reactivated after {gap_days:.0f} days, but the renewed activity was "
            f"modest: {first.amount:.2f} USD (floor: {th['high_value_absolute']:.0f}) and "
            f"{len(burst)} transaction(s) in {th['burst_hours']}h (threshold: "
            f"{th['burst_min_txns']}). Consistent with a customer simply returning."
        ),
    )


def evaluate_account(
    conn: sqlite3.Connection, account_id: str, now: datetime
) -> list[RuleOutcome]:
    """Run every rule. Always returns one outcome per rule, in a stable order.

    Returning non-firing rules is deliberate. An agent that only sees firings
    cannot tell "we checked structuring and it was clean" from "nobody ever
    checked structuring", and neither can the auditor reading its report.
    """
    txns = _load_transactions(conn, account_id)
    return [
        _rule_velocity_burst(txns, now),
        _rule_amount_spike(txns, now),
        _rule_new_geo_high_value(txns, now),
        _rule_structuring(txns, now),
        _rule_shared_device(conn, account_id, now),
        _rule_impossible_travel(txns, now),
        _rule_dormant_reactivation(txns, now),
    ]


def overall_severity(outcomes: list[RuleOutcome]) -> str | None:
    """Highest severity among FIRED rules, or None if nothing fired."""
    fired = [o for o in outcomes if o.status is Status.FIRED]
    if not fired:
        return None
    return str(max(fired, key=lambda o: SEVERITY_ORDER[o.severity]).severity)

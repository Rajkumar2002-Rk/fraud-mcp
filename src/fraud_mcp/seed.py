"""Deterministic synthetic dataset with deliberately planted fraud patterns.

All data here is generated from a fixed PRNG seed. Nothing is real, scraped, or
derived from a real institution. Names are drawn from a small invented list.

The point of the planted patterns is that an agent exploring this server should
be able to *find* something. A dataset of uniform noise makes for a demo where
the model's only honest answer is "nothing here", which tests nothing.
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .db import connect, init_schema, set_meta

# Frozen evaluation clock. See `db.as_of` for why this is pinned.
AS_OF = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)
SEED = 1337

FIRST_NAMES = ["Ada", "Bo", "Cai", "Dara", "Eli", "Fen", "Gia", "Hal", "Ines", "Jo",
               "Kit", "Lena", "Mo", "Nia", "Oz", "Pia", "Quinn", "Rae", "Sol", "Tam"]
LAST_NAMES = ["Arlo", "Beck", "Cho", "Diaz", "Eze", "Frost", "Gale", "Hume", "Ito",
              "Kerr", "Lund", "Mora", "Nash", "Oyelaran", "Pike", "Rossi", "Sato", "Vance"]

MERCHANTS = [
    ("Northwind Grocery", "5411", "card_present"),
    ("Contoso Coffee", "5814", "card_present"),
    ("Fabrikam Fuel", "5541", "card_present"),
    ("Tailspin Transit", "4111", "card_present"),
    ("Litware Online", "5999", "ecommerce"),
    ("Proseware Pharmacy", "5912", "card_present"),
    ("Adventure Outfitters", "5941", "ecommerce"),
    ("Wide World Utilities", "4900", "transfer"),
    ("Fourth Coffee", "5814", "card_present"),
    ("Graphic Design Inst", "8299", "ecommerce"),
]

HIGH_RISK_MERCHANTS = [
    ("Blue Yonder Electronics", "5732", "ecommerce"),
    ("Lucerne Luxury Goods", "5944", "ecommerce"),
    ("Wingtip Gift Cards", "5815", "ecommerce"),
]

COUNTRIES = ["US", "GB", "DE", "CA", "AU"]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class Seeder:
    """Builds the dataset. Split into `_background` noise and `_plant_*` patterns
    so the planted cases stay readable and individually documented."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.rng = random.Random(SEED)
        # Separate stream for retrofits, so adding jitter to one account does
        # not shift every subsequent draw and invalidate the whole dataset.
        self.jitter = random.Random(SEED + 1)
        self._txn_counter = 0

    # ---------- primitives ----------

    def _next_txn_id(self) -> str:
        self._txn_counter += 1
        return f"TXN-{self._txn_counter:06d}"

    def _add_txn(
        self,
        account_id: str,
        ts: datetime,
        amount: float,
        *,
        merchant: tuple[str, str, str] | None = None,
        country: str = "US",
        device_id: str | None = None,
        status: str = "settled",
    ) -> str:
        name, mcc, channel = merchant or self.rng.choice(MERCHANTS)
        txn_id = self._next_txn_id()
        self.conn.execute(
            "INSERT INTO transactions(txn_id, account_id, ts, amount, currency, merchant,"
            " mcc, country, channel, device_id, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (txn_id, account_id, _iso(ts), round(amount, 2), "USD", name, mcc,
             country, channel, device_id, status),
        )
        return txn_id

    def _add_device(self, device_id: str, platform: str, ua: str, first_seen: datetime) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO devices(device_id, platform, user_agent, first_seen)"
            " VALUES (?,?,?,?)",
            (device_id, platform, ua, _iso(first_seen)),
        )

    def _add_event(
        self, device_id: str, account_id: str, event_type: str, ts: datetime,
        ip: str, country: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO device_events(device_id, account_id, event_type, ts, ip, country)"
            " VALUES (?,?,?,?,?,?)",
            (device_id, account_id, event_type, _iso(ts), ip, country),
        )

    # ---------- build ----------

    def build(self) -> None:
        self._accounts_and_devices()
        self._background_activity()
        self._plant_velocity_burst()
        self._plant_account_takeover()
        self._plant_structuring()
        self._plant_impossible_travel()
        set_meta(self.conn, "as_of", _iso(AS_OF))
        set_meta(self.conn, "seed", str(SEED))
        set_meta(self.conn, "generator_version", "1")
        set_meta(self.conn, "planted_patterns", json.dumps(PLANTED_PATTERNS))
        self.conn.commit()

    def _accounts_and_devices(self) -> None:
        for i in range(40):
            acct = f"ACC-{1000 + i}"
            name = f"{self.rng.choice(FIRST_NAMES)} {self.rng.choice(LAST_NAMES)}"
            # ACC-1009 is deliberately dormant with zero recent transactions:
            # it is the "empty result" trap for the agent.
            status = "dormant" if acct == "ACC-1009" else "active"
            home = "US" if i % 4 else self.rng.choice(COUNTRIES)
            self.conn.execute(
                "INSERT INTO accounts(account_id, customer_name, home_country, opened_at, status)"
                " VALUES (?,?,?,?,?)",
                (acct, name, home, _iso(AS_OF - timedelta(days=self.rng.randint(400, 1800))), status),
            )
            dev = f"DEV-{2000 + i}"
            self._add_device(dev, self.rng.choice(["ios", "android", "web"]),
                             "Mozilla/5.0 (synthetic)", AS_OF - timedelta(days=300))

    def _background_activity(self) -> None:
        """Ordinary spending, so the planted patterns have a baseline to stand out from."""
        for i in range(40):
            acct = f"ACC-{1000 + i}"
            dev = f"DEV-{2000 + i}"
            home = self.conn.execute(
                "SELECT home_country FROM accounts WHERE account_id = ?", (acct,)
            ).fetchone()["home_country"]
            if acct == "ACC-1009":
                # Dormant: activity exists, but all of it is >180 days old.
                #
                # The timestamps are jittered from a *separate* PRNG stream. An
                # agent reviewing this account in Claude Desktop noticed that the
                # original version placed all nine transactions at exactly 7-day
                # intervals on identical 12:00:00 timestamps - the only account in
                # the dataset that looked machine-generated - and correctly
                # declined to read behavioural meaning into the spacing. Realistic
                # controls matter: a control account that announces itself as
                # synthetic invites the agent to reason about the generator
                # instead of the fraud. The dedicated stream keeps every other
                # account byte-identical to earlier runs.
                for n, d in enumerate(range(200, 260, 7)):
                    ts = AS_OF - timedelta(
                        days=d,
                        hours=self.jitter.randint(-6, 6),
                        minutes=self.jitter.randint(0, 59),
                    )
                    self._add_txn(acct, ts, self.rng.uniform(20, 90),
                                  country=home, device_id=dev)
                continue
            typical = self.rng.uniform(35, 120)
            for day in range(1, 120):
                for _ in range(self.rng.choice([0, 1, 1, 2, 3])):
                    ts = AS_OF - timedelta(
                        days=day, hours=self.rng.randint(0, 23), minutes=self.rng.randint(0, 59)
                    )
                    amount = max(3.0, self.rng.gauss(typical, typical * 0.35))
                    self._add_txn(acct, ts, amount, country=home, device_id=dev)
                    if self.rng.random() < 0.08:
                        self._add_event(dev, acct, "login", ts - timedelta(minutes=2),
                                        f"198.51.100.{self.rng.randint(1, 254)}", home)

    # ---------- planted patterns ----------

    def _plant_velocity_burst(self) -> None:
        """ACC-1007: card-testing burst — 7 small-then-large txns inside 6 minutes.

        Should trip VELOCITY_BURST, and the tail transaction should trip
        AMOUNT_SPIKE against the account's own baseline.
        """
        acct, dev = "ACC-1007", "DEV-2007"
        start = AS_OF - timedelta(days=2, hours=3)
        for n, amount in enumerate([1.00, 1.00, 24.99, 89.50, 149.00, 610.00, 1890.00]):
            self._add_txn(
                acct, start + timedelta(seconds=n * 47), amount,
                merchant=HIGH_RISK_MERCHANTS[n % len(HIGH_RISK_MERCHANTS)],
                country="US", device_id=dev,
                status="declined" if amount == 1.00 else "settled",
            )

    def _plant_account_takeover(self) -> None:
        """Shared-device ATO: one attacker device drives three unrelated accounts.

        ACC-1013 is the victim — password reset from the attacker device, then a
        high-value purchase from a country it has never transacted in. ACC-1014
        and ACC-1015 are the corroborating accounts that make the device, not the
        account, the common factor.
        """
        attacker = "DEV-ATO-01"
        self._add_device(attacker, "web", "Mozilla/5.0 (X11; synthetic-headless)",
                         AS_OF - timedelta(days=9))
        victims = ["ACC-1013", "ACC-1014", "ACC-1015"]
        for n, acct in enumerate(victims):
            base = AS_OF - timedelta(days=4, hours=6 - n * 2)
            self._add_event(attacker, acct, "login_failed", base - timedelta(minutes=12),
                            "203.0.113.77", "NG")
            self._add_event(attacker, acct, "login_failed", base - timedelta(minutes=9),
                            "203.0.113.77", "NG")
            self._add_event(attacker, acct, "password_reset", base - timedelta(minutes=4),
                            "203.0.113.77", "NG")
            self._add_event(attacker, acct, "login", base, "203.0.113.77", "NG")
            self._add_txn(acct, base + timedelta(minutes=6), 2450.00 + n * 310,
                          merchant=HIGH_RISK_MERCHANTS[n % 3], country="NG",
                          device_id=attacker)

    def _plant_structuring(self) -> None:
        """ACC-1021: five transfers just below the 10,000 reporting threshold over 40h."""
        acct, dev = "ACC-1021", "DEV-2021"
        start = AS_OF - timedelta(days=3, hours=4)
        for n, amount in enumerate([9250.00, 9480.00, 9100.00, 9725.00, 9390.00]):
            self._add_txn(
                acct, start + timedelta(hours=n * 9), amount,
                merchant=("Woodgrove Transfer", "6012", "transfer"),
                country="US", device_id=dev,
            )

    def _plant_impossible_travel(self) -> None:
        """ACC-1030: card-present in US then card-present in SG 38 minutes later."""
        acct, dev = "ACC-1030", "DEV-2030"
        t = AS_OF - timedelta(days=1, hours=8)
        self._add_txn(acct, t, 62.40, merchant=MERCHANTS[0], country="US", device_id=dev)
        self._add_txn(acct, t + timedelta(minutes=38), 880.00,
                      merchant=("Changi Duty Free", "5309", "card_present"),
                      country="SG", device_id=dev)


PLANTED_PATTERNS = [
    {"account_id": "ACC-1007", "pattern": "velocity_burst",
     "note": "7 transactions in ~6 minutes, escalating amounts (card testing)"},
    {"account_id": "ACC-1013", "pattern": "account_takeover",
     "note": "password reset + login from shared attacker device DEV-ATO-01, then new-geo high-value"},
    {"account_id": "ACC-1014", "pattern": "account_takeover_corroborating",
     "note": "same attacker device DEV-ATO-01"},
    {"account_id": "ACC-1015", "pattern": "account_takeover_corroborating",
     "note": "same attacker device DEV-ATO-01"},
    {"account_id": "ACC-1021", "pattern": "structuring",
     "note": "5 transfers of 9.1k-9.7k across 40 hours, just under the 10k threshold"},
    {"account_id": "ACC-1030", "pattern": "impossible_travel",
     "note": "US then SG card-present 38 minutes apart"},
    {"account_id": "ACC-1009", "pattern": "control_dormant",
     "note": "no activity within 180 days - empty-result case, NOT fraud"},
    {"account_id": "ACC-1002", "pattern": "control_clean",
     "note": "ordinary activity only - true negative"},
]


def build_database(path: Path | str | None = None, *, overwrite: bool = True) -> Path:
    from .db import db_path

    target = Path(path) if path is not None else db_path()
    if overwrite and target.exists():
        target.unlink()
    conn = connect(target)
    try:
        init_schema(conn)
        Seeder(conn).build()
    finally:
        conn.close()
    return target

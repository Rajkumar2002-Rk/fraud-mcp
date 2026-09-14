"""CLI entry point: `fraud-mcp [stdio|http|seed|check]`."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fraud-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("stdio", help="Run the MCP server over stdio (default; used by Claude Desktop)")
    http = sub.add_parser("http", help="Run the MCP server over streamable HTTP")
    http.add_argument("--host", default="127.0.0.1")
    http.add_argument("--port", type=int, default=8000)
    seed = sub.add_parser("seed", help="(Re)build the synthetic SQLite dataset")
    seed.add_argument("--path", default=None)
    sub.add_parser("check", help="Print a summary of the seeded dataset and exit")

    args = parser.parse_args(argv)
    command = args.command or "stdio"

    if command == "seed":
        from .seed import build_database

        path = build_database(args.path)
        print(f"Seeded database at {path}")
        return 0

    if command == "check":
        from . import db as dbmod

        conn = dbmod.connect(read_only=True)
        try:
            for table in ("accounts", "devices", "device_events", "transactions", "cases"):
                n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                print(f"{table:<15} {n:>6}")
            print(f"as_of           {dbmod.as_of(conn).isoformat()}")
        finally:
            conn.close()
        return 0

    _ensure_seeded()
    from .server import server

    if command == "http":
        server.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        server.run(transport="stdio")
    return 0


def _ensure_seeded() -> None:
    """Build the dataset on first run.

    A server that starts and then errors on every call is a worse first
    experience than one that spends a second seeding itself.
    """
    from . import db as dbmod
    from .seed import build_database

    path = dbmod.db_path()
    if not path.exists():
        print(f"No dataset at {path}; seeding...", file=sys.stderr)
        build_database(path)


if __name__ == "__main__":
    raise SystemExit(main())

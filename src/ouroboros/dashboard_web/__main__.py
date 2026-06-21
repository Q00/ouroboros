"""CLI entrypoints for the dashboard.

- ``--serve-daemon``  : run the singleton daemon (self-selects port, idle-exits).
                        This is what :func:`daemon.ensure_dashboard` spawns.
- ``--run <id>``      : ensure the daemon is up and print the URL for that run.
- (no args)           : ensure the daemon and list recent runs to open.
"""

from __future__ import annotations

import argparse

from ouroboros.dashboard_web.daemon import ensure_dashboard, run_daemon
from ouroboros.dashboard_web.reader import default_db_path, list_recent_executions


def main() -> None:
    parser = argparse.ArgumentParser(prog="ouroboros.dashboard_web")
    parser.add_argument(
        "--serve-daemon", action="store_true", help="run the singleton daemon process"
    )
    parser.add_argument("--run", help="execution_id to open (ensures the daemon)")
    parser.add_argument("--db", default=str(default_db_path()), help="EventStore SQLite path")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    if args.serve_daemon:
        run_daemon(db_path=args.db, host=args.host)
        return

    info = ensure_dashboard(db_path=args.db, host=args.host)
    if info is None:
        print("Could not start the dashboard daemon.")
        return

    state = "reused existing" if info.reused else "started"
    print(f"Dashboard daemon {state} at {info.url}  (pid={info.pid})")
    if args.run:
        print(f"Open: {info.run_url(args.run)}")
        return

    recent = list_recent_executions(args.db)
    if not recent:
        print("No runs found yet — start one with `ooo run` / `ooo auto`.")
        return
    print("Recent runs:")
    for item in recent:
        print(f"  {info.run_url(item['execution_id'])}   ({item['node_count']} nodes)")


if __name__ == "__main__":
    main()

"""``crewspace-manage`` entrypoint — Django-style management CLI.

Dispatches subcommands to crewspace.management.commands and opens/commits the
database for each invocation. Example::

    crewspace-manage createsuperuser
    crewspace-manage changepassword Bilal --password newsecret --no-input
"""
from __future__ import annotations

import argparse
import sys

from . import ManagementCommandError, run_async
from .commands import COMMANDS, SYNC_COMMANDS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crewspace-manage",
        description="Crewspace management commands (Django-style).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, (register, _run) in {**COMMANDS, **SYNC_COMMANDS}.items():
        sub_parser = sub.add_parser(name, help=(register.__doc__ or name))
        register(sub_parser)

    return parser


def main() -> None:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s [%(name)s] %(message)s")
    logging.getLogger("alembic").setLevel(logging.WARNING)

    parser = build_parser()
    args = parser.parse_args()

    if args.command in SYNC_COMMANDS:
        _register, run = SYNC_COMMANDS[args.command]
        try:
            run(args)
        except ManagementCommandError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        return

    _register, run = COMMANDS[args.command]
    # run is a coroutine factory ``run(args, conn)``; run_async opens/commits the DB.
    run_async(lambda conn: run(args, conn))


if __name__ == "__main__":
    main()

"""Command line for a charge. Use --json to print the result as JSON."""

from __future__ import annotations

import argparse

from demo.claims.billing import FEE_RATE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print a charge.")
    parser.add_argument("--raw", action="store_true", help="print the raw charge")
    parser.add_argument("amount", type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    amount = args.amount if args.raw else args.amount * FEE_RATE
    print(amount)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

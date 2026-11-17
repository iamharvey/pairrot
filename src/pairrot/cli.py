"""pairrot review and pairrot eval."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pairrot.eval_import import import_audit
from pairrot.eval_replay import replay
from pairrot.models import Budget
from pairrot.pipeline import review_change
from pairrot.publish_github import publish_from_env


def _budget_from_env(cost_cap: float | None) -> Budget:
    cap = cost_cap
    if cap is None and os.environ.get("PAIRROT_COST_CAP"):
        cap = float(os.environ["PAIRROT_COST_CAP"])
    per = float(os.environ.get("PAIRROT_COST_PER_1K_TOKENS") or 0)
    wall = int(os.environ.get("PAIRROT_WALL_CLOCK_SECONDS") or 600)
    tools = int(os.environ.get("PAIRROT_MAX_TOOL_CALLS") or 30)
    return Budget(
        max_tool_calls_per_perspective=tools,
        wall_clock_seconds=wall,
        cost_cap=cap,
        cost_per_1k_tokens=per,
    )


def _models(verifier_arg: str | None) -> tuple[str | None, str | None]:
    reviewer = os.environ.get("PAIRROT_MODEL") or os.environ.get("OPENAI_MODEL")
    verifier = verifier_arg or os.environ.get("PAIRROT_VERIFIER_MODEL") or reviewer
    return reviewer, verifier


def _cmd_review(args: argparse.Namespace) -> int:
    reviewer, verifier = _models(args.verifier_model)
    report = review_change(
        args.repo,
        args.base,
        args.head,
        args.out,
        ticket=args.ticket,
        ci=args.ci,
        budget=_budget_from_env(args.cost_cap),
        reviewer_model=reviewer,
        verifier_model=verifier,
    )
    print(
        f"status={report.status.value} report={Path(args.out) / 'report.json'} reused={str(report.reused).lower()}"
    )
    return 0 if report.status.value != "failed" else 1


def _cmd_import(args: argparse.Namespace) -> int:
    summary = import_audit(args.xlsx, args.out)
    counts = summary["counts"]
    print(
        f"dataset_a={counts['dataset_a']} dataset_b={counts['dataset_b']} out={args.out}"
    )
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    reviewer, verifier = _models(args.verifier_model)
    report = replay(
        args.dataset,
        args.repos_root,
        args.out,
        budget=_budget_from_env(args.cost_cap),
        reviewer_model=reviewer,
        verifier_model=verifier,
    )
    message = report["dataset_c"].get("message") or "dataset C present"
    print(f"replay={Path(args.out) / 'replay_report.json'} {message}")
    return 0


def _cmd_publish(args: argparse.Namespace) -> int:
    planned = publish_from_env(args.report, args.pr, repository=args.repository, token=args.token)
    print(f"inline={len(planned.inline)} summary=updated")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pairrot")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="Review one local diff and write a JSON report")
    review.add_argument("--repo", required=True)
    review.add_argument("--base", required=True)
    review.add_argument("--head", required=True)
    review.add_argument("--out", required=True)
    review.add_argument("--ticket", help="Local ticket text. Missing means a coverage gap")
    review.add_argument("--ci", help="Local CI JSON. Missing means a coverage gap")
    review.add_argument("--cost-cap", type=float, default=None, help="Unset means uncapped")
    review.add_argument("--verifier-model", default=None)
    review.set_defaults(func=_cmd_review)

    evaluation = sub.add_parser("eval", help="Import the audit workbook or replay datasets")
    eval_sub = evaluation.add_subparsers(dest="eval_command", required=True)

    importer = eval_sub.add_parser("import-audit", help="Import dataset A and dataset B")
    importer.add_argument("--xlsx", required=True)
    importer.add_argument("--out", required=True)
    importer.set_defaults(func=_cmd_import)

    runner = eval_sub.add_parser("replay", help="Replay datasets A, B, and C")
    runner.add_argument("--dataset", required=True, help="Directory with dataset_a/b/c jsonl")
    runner.add_argument("--repos-root", required=True)
    runner.add_argument("--out", required=True)
    runner.add_argument("--cost-cap", type=float, default=None, help="Unset means uncapped")
    runner.add_argument("--verifier-model", default=None)
    runner.set_defaults(func=_cmd_replay)

    publish = sub.add_parser("publish", help="Write a report back to a pull request")
    publish_sub = publish.add_subparsers(dest="publish_command", required=True)
    github = publish_sub.add_parser("github", help="Comment on a GitHub pull request")
    github.add_argument("--report", required=True)
    github.add_argument("--pr", required=True, type=int)
    github.add_argument("--repository", default=None, help="Defaults to GITHUB_REPOSITORY")
    github.add_argument("--token", default=None, help="Defaults to GITHUB_TOKEN")
    github.set_defaults(func=_cmd_publish)
    return parser


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from a local .env. Existing variables win."""
    if os.environ.get("PAIRROT_DISABLE_DOTENV") == "1":
        return
    candidates = []
    if path is not None:
        candidates.append(path)
    else:
        candidates.append(Path.cwd() / ".env")
        candidates.append(Path(__file__).resolve().parents[2] / ".env")
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        for raw in resolved.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ[key] = value


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""pairrot review and pairrot eval."""

from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources
from pathlib import Path

from pairrot.eval_import import import_audit
from pairrot.eval_replay import replay
from pairrot.export import to_markdown, to_sonar
from pairrot.models import Budget
from pairrot.pipeline import review_change
from pairrot.policy import POLICY_FILE
from pairrot.publish_github import load_report, publish_from_env


def _budget_from_env(cost_cap: float | None) -> Budget:
    """Outside GitHub Actions the defaults are larger, so a local run can look across files.

    CI sets GITHUB_ACTIONS and keeps the smaller defaults unless its workflow overrides them.
    An explicit PAIRROT_* variable always wins.
    """
    cap = cost_cap
    if cap is None and os.environ.get("PAIRROT_COST_CAP"):
        cap = float(os.environ["PAIRROT_COST_CAP"])
    per = float(os.environ.get("PAIRROT_COST_PER_1K_TOKENS") or 0)
    ci = os.environ.get("GITHUB_ACTIONS") == "true"
    wall = int(os.environ.get("PAIRROT_WALL_CLOCK_SECONDS") or (600 if ci else 1200))
    tools = int(os.environ.get("PAIRROT_MAX_TOOL_CALLS") or (2 if ci else 8))
    files = int(os.environ.get("PAIRROT_MAX_FILES") or (40 if ci else 80))
    lines = int(os.environ.get("PAIRROT_MAX_CHANGED_LINES") or (2000 if ci else 8000))
    return Budget(
        max_tool_calls_per_perspective=tools,
        wall_clock_seconds=wall,
        max_files=files,
        max_changed_lines=lines,
        cost_cap=cap,
        cost_per_1k_tokens=per,
    )


def _models(verifier_arg: str | None) -> tuple[str | None, str | None]:
    reviewer = os.environ.get("PAIRROT_MODEL") or os.environ.get("OPENAI_MODEL")
    verifier = verifier_arg or os.environ.get("PAIRROT_VERIFIER_MODEL") or reviewer
    return reviewer, verifier


def _print_findings(report) -> None:
    confirmed = sorted(report.confirmed, key=lambda item: (item.location.file, item.location.start_line))
    print(f"findings={len(confirmed)}")
    for finding in confirmed:
        mark = finding.level or finding.severity.value
        title = finding.title or finding.trigger
        print(f"  {mark} {finding.location.file}:{finding.location.start_line} {title}")
    if report.undecided:
        print(f"undecided={len(report.undecided)}")


def _cmd_review(args: argparse.Namespace) -> int:
    if args.worktree and args.head:
        print("error: --worktree 用工作区代替 --head，不要同时传", file=sys.stderr)
        return 1
    if not args.worktree and not args.head:
        print("error: 需要 --head，或用 --worktree 评审工作区", file=sys.stderr)
        return 1
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
        worktree=args.worktree,
        policy_file=args.policy,
    )
    print(
        f"status={report.status.value} check={'failed' if report.fail_check else 'ok'} "
        f"report={Path(args.out) / 'report.json'} reused={str(report.reused).lower()}"
        + (" worktree=true" if report.head_kind == "worktree" else "")
    )
    _print_findings(report)
    if report.error:
        print(f"error: {report.error}", file=sys.stderr)
    if report.policy_source == "local":
        print("note: 使用了本地规则，CI 不会用这份 REVIEW.toml", file=sys.stderr)
    hint = _policy_hint(report, Path(args.repo))
    if hint:
        print(hint, file=sys.stderr)
    return exit_code(report, defer_check=args.defer_check)


def _worktree_policy(repo: Path) -> str | None:
    if not repo.is_dir():
        return None
    for entry in repo.iterdir():
        if entry.is_file() and entry.name.lower() == POLICY_FILE.lower():
            return entry.name
    return None


def _policy_hint(report, repo: Path) -> str | None:
    if report.policy_source != "default" or report.error:
        return None
    local = _worktree_policy(repo)
    if not local:
        return None
    if local != POLICY_FILE:
        return f"note: 工作区里的 {local} 不会被读取，文件名必须是 {POLICY_FILE}"
    return f"note: 工作区有 {POLICY_FILE}，但 base 上没有，本次按默认规则评审。提交并合进 base 后生效"


def _cmd_init(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    existing = _worktree_policy(repo)
    if existing and not args.force:
        print(f"error: {repo / existing} 已存在，加 --force 覆盖", file=sys.stderr)
        return 1
    if existing and existing != POLICY_FILE:
        (repo / existing).unlink()
    target = repo / POLICY_FILE
    text = resources.files("pairrot").joinpath("REVIEW.example.toml").read_text(encoding="utf-8")
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {target}。提交并合进主干后，对之后的 PR 生效")
    return 0


def exit_code(report, *, defer_check: bool = False) -> int:
    if report.status.value == "failed":
        return 1
    if report.fail_check and not defer_check:
        return 1
    return 0


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
    planned = publish_from_env(
        args.report,
        args.pr,
        repository=args.repository,
        token=args.token,
        new_commits=args.new_commits,
    )
    print(f"inline={len(planned.inline)} summary=updated check={planned.check}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"check={planned.check}\n")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    report = load_report(args.report)
    if args.format == "sonar":
        text = json.dumps(to_sonar(report), ensure_ascii=False, indent=2) + "\n"
    else:
        text = to_markdown(report) + "\n"
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"{args.format}={args.out} confirmed={len(report.confirmed)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pairrot")
    sub = parser.add_subparsers(dest="command", required=True)

    review = sub.add_parser("review", help="Review one local diff and write a JSON report")
    review.add_argument("--repo", required=True)
    review.add_argument("--base", required=True)
    review.add_argument("--head", help="Commit to review. Omit it with --worktree")
    review.add_argument(
        "--worktree",
        action="store_true",
        help="Review the working tree, including uncommitted and untracked files, instead of --head",
    )
    review.add_argument(
        "--policy",
        help="Read rules from this file instead of the base commit. CI will not use it",
    )
    review.add_argument("--out", required=True)
    review.add_argument("--ticket", help="Local ticket text. Missing means a coverage gap")
    review.add_argument("--ci", help="Local CI JSON. Missing means a coverage gap")
    review.add_argument("--cost-cap", type=float, default=None, help="Unset means uncapped")
    review.add_argument("--verifier-model", default=None)
    review.add_argument(
        "--defer-check",
        action="store_true",
        help="Exit 0 on must_fix findings and leave the check to publish, which honors the override label",
    )
    review.set_defaults(func=_cmd_review)

    init = sub.add_parser("init", help="Write a starter REVIEW.toml to the repository root")
    init.add_argument("--repo", default=".")
    init.add_argument("--force", action="store_true", help="Overwrite an existing REVIEW.toml")
    init.set_defaults(func=_cmd_init)

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
    github.add_argument(
        "--new-commits",
        action="store_true",
        help="The head moved. Remove the override label so it must be applied to this head again",
    )
    github.set_defaults(func=_cmd_publish)

    export = sub.add_parser("export", help="Render a report as Markdown or SonarQube external issues")
    export.add_argument("format", choices=["markdown", "sonar"])
    export.add_argument("--report", required=True)
    export.add_argument("--out", required=True)
    export.set_defaults(func=_cmd_export)
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
        for raw in resolved.read_text(encoding="utf-8-sig").splitlines():
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

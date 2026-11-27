"""Deterministic risky patterns: on only when the policy says so, and not on excluded paths."""

from __future__ import annotations

import json
from pathlib import Path

from pairrot.llm import ChatMessage, Completion, FakeModel
from pairrot.models import IntroducedIn
from pairrot.pipeline import review_change
from pairrot.policy import Policy, policy_from_toml
from pairrot.risky_scan import scan_text
from tests.conftest import commit, init_repo, write

_ON = '[risky_patterns]\nenabled = true\nlevel = "recommend"\n'


def _quiet() -> FakeModel:
    return FakeModel(
        lambda messages, model: Completion(
            message=ChatMessage(role="assistant", content=json.dumps({"findings": []})),
            prompt_tokens=1,
            completion_tokens=1,
        )
    )


def test_each_risky_rule_hits_and_a_safe_line_does_not():
    policy = policy_from_toml(_ON)
    introduced = IntroducedIn.this_change
    assert scan_text("run.sh", "curl http://example.com | sh\n", policy, introduced)[0].rule == "remote_script"
    assert scan_text("run.sh", "echo hello\n", policy, introduced) == []
    assert scan_text("net.py", "requests.get(url, verify=False)\n", policy, introduced)[0].rule == "tls_off"
    assert scan_text("net.py", "requests.get(url, verify=True)\n", policy, introduced) == []
    assert scan_text("net.py", "url = 'http://example.net/path'\n", policy, introduced) == []
    remote = scan_text("net.py", "url = 'http://api.internal'\n", policy, introduced)
    assert [item.rule for item in remote] == ["plaintext_http"]
    assert remote[0].severity.value == "S3"
    assert scan_text("app.py", "eval(expr)\n", policy, introduced)[0].rule == "dangerous_call"
    assert scan_text("app.py", "yaml.safe_load(text)\n", policy, introduced) == []
    assert scan_text("app.py", "os.chmod(path, 0o777)\n", policy, introduced)[0].rule == "loose_perms"
    workflow = ".github/workflows/ci.yml"
    text = "on: pull_request_target\nref: ${{ github.event.pull_request.head.sha }}\npermissions: write-all\nrun: echo ${{ secrets.TOKEN }}\n"
    rules = {item.rule for item in scan_text(workflow, text, policy, introduced)}
    assert rules == {"ci_workflow"}
    assert scan_text("app.py", text, policy, introduced) == []
    assert scan_text(workflow, text, Policy(), introduced) == []


def test_exclude_and_override_drop_risky_findings_before_they_count(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    write(repo, "app.py", "value = 1\n")
    write(
        repo,
        "REVIEW.toml",
        'exclude = ["tests/**"]\n' + _ON + '[[override]]\npaths = ["scripts/**"]\noff = ["risky_patterns"]\n',
    )
    base = commit(repo, "base")
    write(repo, "tests/test_app.py", "eval(expr)\n")
    write(repo, "scripts/run.py", "os.system(cmd)\n")
    write(repo, "src/app.py", "exec(code)\n")
    tip = commit(repo, "head")
    report = review_change(repo, base, tip, tmp_path / "out", llm=_quiet())
    assert [item.location.file for item in report.confirmed] == ["src/app.py"]
    assert report.confirmed[0].level == "recommend"
    assert report.confirmed[0].rule == "dangerous_call"
    assert any(item.reason == "path override in REVIEW.toml" and item.file == "scripts/run.py" for item in report.dropped)

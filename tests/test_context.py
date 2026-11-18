"""Verifier context: enclosing function, trimmed diff, callees, and callers."""

from __future__ import annotations

from pathlib import Path

from pairrot.context import (
    callees,
    callers,
    diff_hunks,
    enclosing_name,
    function_window,
    module_header,
    relocate,
)
from tests.conftest import commit, init_repo, seed, write

GATEWAY = '''class GatewayError(Exception):
    pass


class Client:
    def charge(self, amount):
        if amount < 0:
            raise GatewayError("bad")
        return "ok"
'''

SHOP = '''from gateway import Client, GatewayError


def helper(x):
    return x + 1


def place(client, amount):
    total = helper(amount)
    try:
        client.charge(total)
    except GatewayError:
        pass
    return "paid"
'''


def _lines(text: str) -> list[str]:
    return text.splitlines()


def test_function_window_covers_the_whole_enclosing_function():
    rows = _lines(SHOP)
    first, last, whole = function_window(rows, 12, 13)
    assert (first, last, whole) == (8, 14, True)
    assert enclosing_name(rows, 12, 13) == "place"
    first, last, _ = function_window(_lines(GATEWAY), 8, 8)
    assert (first, last) == (6, 9)


def test_long_function_is_windowed_and_marked_incomplete():
    body = "def big():\n" + "".join(f"    x{i} = {i}\n" for i in range(200))
    first, last, whole = function_window(_lines(body), 150, 150)
    assert not whole
    assert first <= 150 <= last
    assert last - first + 1 <= 81


def test_diff_rows_are_trimmed_to_the_function_inside_one_big_hunk():
    diff = "@@ -0,0 +1,14 @@\n" + "".join(f"+{row}\n" for row in _lines(SHOP))
    trimmed = diff_hunks(diff, 8, 14)
    assert "+def place(client, amount):" in trimmed
    assert "from gateway import" not in trimmed
    assert trimmed.startswith("@@ head 5 @@")


def test_callees_prefer_functions_nearest_the_finding():
    rows = _lines(SHOP)
    found = callees(rows, 8, 14, 12, 13, {"gateway.py": GATEWAY, "shop.py": SHOP})
    names = [text.splitlines()[0] for _, _, _, text in found]
    assert names[0].endswith("def charge(self, amount):")
    assert any(name.endswith("def helper(x):") for name in names)
    assert names[-1].endswith("class GatewayError(Exception):")


def test_module_header_finds_docstrings_and_comment_blocks():
    doc = '"""Daily job.\n\nAbout 100k rows a day.\n"""\n\nimport os\n'
    assert module_header(_lines(doc)) == (1, 4)
    assert module_header(_lines('"""One line."""\nx = 1\n')) == (1, 1)
    assert module_header(_lines("#!/usr/bin/env python\n# Never log card numbers.\nx = 1\n")) == (2, 2)
    assert module_header(_lines("// Shared by all threads.\n// Keep it short.\nlet x = 1;\n")) == (1, 2)
    assert module_header(_lines(SHOP)) is None


def test_relocate_moves_drifted_lines_onto_the_snippet():
    rows = _lines(SHOP)
    assert relocate(rows, "client.charge(total)", 2, 3) == (11, 11)
    assert relocate(rows, "try:\n    client.charge(total)", 10, 12) == (10, 12)
    assert relocate(rows, "def place(client, amount):\n    total = helper(amount)", 14, 16) == (8, 9)
    assert relocate(rows, "def place(client, amount): total = helper(amount)", 4, 9) == (8, 8)
    assert relocate(rows, "nothing like this here", 2, 3) == (2, 3)
    assert relocate(rows, "", 2, 3) == (2, 3)


def test_callers_list_call_sites_and_report_none_found(tmp_path: Path):
    repo = init_repo(tmp_path / "repo")
    seed(repo)
    write(repo, "gateway.py", GATEWAY)
    write(repo, "shop.py", SHOP)
    head = commit(repo, "add")
    cache: dict[str, list[str]] = {}
    sites, listed = callers(repo, head, "charge", cache)
    assert listed
    assert len(sites) == 1
    assert sites[0].startswith("FILE shop.py:8-14")
    assert "client.charge(total)" in sites[0]
    assert callers(repo, head, "place", cache) == ([], True)
    assert callers(repo, head, "unknown", cache) == ([], False)

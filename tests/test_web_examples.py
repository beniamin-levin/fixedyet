"""The bundled examples: their notes are short markdown bullets with real links."""

from __future__ import annotations

import re

from fixedyet.web import app as web


def test_the_bundled_example_notes_scan_and_link_to_github() -> None:
    items = web.load_examples(web.default_examples_dir())
    assert items
    for item in items:
        lines = item["note"].splitlines()
        assert len(lines) >= 3, item["id"]
        assert all(line.startswith("- ") for line in lines), item["id"]
        without_links = re.sub(r"\[[^\]]*\]\([^)]*\)", "", item["note"])
        assert "http" not in without_links, item["id"]  # every URL is a markdown link
        assert "**" in item["note"] and "`" in item["note"]
        issue = item["issue_url"]
        slug = "/".join(issue.split("/")[3:5]) + "#" + issue.rsplit("/", 1)[1]
        assert f"[{slug}]({issue})" in item["note"], item["id"]
        assert len(item["note"]) < 700
    backport = next(i for i in items if i["kind"] == "backport")
    assert backport["pr_url"] == "https://github.com/beniamin-levin/pytest/pull/1"
    assert f"[beniamin-levin/pytest#1]({backport['pr_url']})" in backport["note"]

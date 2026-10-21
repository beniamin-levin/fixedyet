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
    prs = {i["id"]: i["pr_url"] for i in items if i["kind"] == "backport"}
    assert prs == {
        "real-backport-pydantic-pydantic-13991": "https://github.com/pydantic/pydantic/pull/14043",
        "real-backport-pytest-dev-pytest-13784": "https://github.com/beniamin-levin/pytest/pull/1",
    }
    for item in items:
        if item["kind"] == "backport":
            slug = "/".join(item["pr_url"].split("/")[3:5]) + "#" + item["pr_url"].rsplit("/", 1)[1]
            assert f"[{slug}]({item['pr_url']})" in item["note"], item["id"]

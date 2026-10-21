"""`envbuild.bisect_fix` against a real Docker daemon, on a tiny repository made in the sandbox.

Run with ``pytest -m docker``; CI does (.github/workflows/sandbox-docker.yml). Only a real git
and a real pytest can show that `git bisect run`, the skip of an untestable commit, the check at
the answer and its parent, and `git tag --contains` work together. Nothing here needs GitHub: the
repository is complete, so the prefetch step finds nothing missing.

    main   1.0.0  chore  no import  buggy  chore  FIX  1.1.0rc1  1.1.0  chore
           c0 --- c1 --- c2 ------- c3 --- c4 --- c5 - c6 ------ c7 --- c8
    line2  c0 - chore - chore - merge of topic (whose one commit is the fix) - chore
    line3  c0 - chore - no import - fix
    main4  c0 - rewrite parse (no bug) - chore - merge of stable4 - chore
    stable4 c0 - chore (the reported version: still the bug)
"""

from __future__ import annotations

import re
import shutil

import pytest
import pytest_asyncio

from fixedyet.envbuild import bisect_fix
from fixedyet.sandbox.docker import DockerSandbox

pytestmark = [
    pytest.mark.docker,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed"),
]

SETUP = r"""
set -e
git config --global user.email dev@example.com
git config --global user.name Dev
git config --global init.defaultBranch main
mkdir -p /work/repo/widgets /work/repo/tests && cd /work/repo
git init -q
printf '%s\n' '[build-system]' 'requires = ["setuptools"]' \
  'build-backend = "setuptools.build_meta"' '' '[project]' 'name = "widgets"' \
  'version = "1.0"' '' '[tool.setuptools]' 'packages = ["widgets"]' > pyproject.toml
code() { printf '%s\n' 'def parse(text):' "    return $1" > widgets/__init__.py; }
buggy() { code 'text.split(",")[:-1]'; }
fixed() { code 'text.split(",")'; }
chore() { echo "$1" >> NOTES.md; git add -A; git commit -q -m "chore $1"; }
buggy
git add -A && git commit -q -m "Initial commit" && git tag 1.0.0
echo "__C0__=$(git rev-parse HEAD)"
chore 1
echo 'def parse(text) return' > widgets/__init__.py
git commit -q -am "broken: does not import"
buggy
git commit -q -am "buggy again"
chore 4
fixed
git commit -q -am "use match to remove port"
echo "__FIX__=$(git rev-parse HEAD)"
chore 6; git tag 1.1.0rc1
chore 7; git tag 1.1.0
chore 8
echo "__MAIN__=$(git rev-parse HEAD)"

git checkout -q -b line2 1.0.0
chore line2-1
git checkout -q -b topic
fixed
git commit -q -am "topic: keep the last item"
echo "__TOPIC_FIX__=$(git rev-parse HEAD)"
git checkout -q line2
chore line2-2
git merge -q --no-ff topic -m "Merge topic"
chore line2-3
echo "__LINE2__=$(git rev-parse HEAD)"

git checkout -q -b line3 1.0.0
chore line3-1
echo "__LINE3_START__=$(git rev-parse HEAD)"
echo 'def parse(text) return' > widgets/__init__.py
git commit -q -am "line3: does not import"
echo "__LINE3_BROKEN__=$(git rev-parse HEAD)"
fixed
git commit -q -am "line3: fix"
echo "__LINE3_FIX__=$(git rev-parse HEAD)"

git checkout -q -b main4 1.0.0
code 'list(text.split(","))'
git commit -q -am "rewrite parse"
echo "__REWRITE__=$(git rev-parse HEAD)"
chore main4-1
git checkout -q -b stable4 1.0.0
echo "stable" >> STABLE.md; git add -A; git commit -q -m "stable4 chore"
echo "__OLD4__=$(git rev-parse HEAD)"
git checkout -q main4
git merge -q --no-ff stable4 -m "Merge branch 'stable4'"
echo "__MERGE4__=$(git rev-parse HEAD)"
chore main4-2
echo "__NEW4__=$(git rev-parse HEAD)"

git checkout -q main
pip install -q pytest > /dev/null 2>&1
pip install -q -e . > /dev/null 2>&1
echo __SETUP_DONE__
"""

REPRO_PATH = "tests/test_issue_repro.py"
REPRO = """from widgets import parse


def test_parse_keeps_the_last_item():
    assert parse("a,b,c") == ["a", "b", "c"]
"""
MARKS = (r"^__(C0|FIX|MAIN|TOPIC_FIX|LINE2|LINE3_START|LINE3_BROKEN|LINE3_FIX|REWRITE|OLD4|MERGE4"
         r"|NEW4)__=(\w+)$")


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def world() -> tuple[DockerSandbox, str, dict[str, str]]:
    sandbox = DockerSandbox()
    built = await sandbox.run(
        await sandbox.base_image("3.12"), SETUP, workdir="/work", keep=True, timeout_s=600
    )
    assert built.exit_code == 0, built.stdout + built.stderr
    assert "__SETUP_DONE__" in built.stdout and built.image
    marks = dict(re.findall(MARKS, built.stdout, re.MULTILINE))
    assert len(marks) == 12, built.stdout
    return sandbox, built.image, marks


async def test_bisect_finds_the_fixing_commit_and_the_release_that_ships_it(world) -> None:
    sandbox, image, marks = world
    found = await bisect_fix(
        sandbox, image, old=marks["C0"], new=marks["MAIN"], test_path=REPRO_PATH,
        test_code=REPRO,
    )
    assert found.status == "found", (found.reason, found.log)
    assert found.commit == marks["FIX"] and not found.merge
    assert found.subject == "use match to remove port"
    assert found.range_count == 8
    assert found.release == "1.1.0"  # not 1.1.0rc1, a pre-release, and not 1.0.0
    assert 1 <= len(found.steps) <= 5, found.steps


async def test_bisect_follows_a_fix_into_the_branch_a_merge_brought_in(world) -> None:
    sandbox, image, marks = world
    found = await bisect_fix(
        sandbox, image, old=marks["C0"], new=marks["LINE2"], test_path=REPRO_PATH,
        test_code=REPRO,
    )
    assert found.status == "found", (found.reason, found.log)
    assert found.commit == marks["TOPIC_FIX"]
    assert found.release is None  # no release tag contains it


async def test_a_commit_that_cannot_be_tested_is_skipped_not_judged(world) -> None:
    """The only commit between the two sides does not import: git must skip it (a collection
    error is neither "broken" nor "fixed"), so it cannot tell whether that commit or the next
    fixed the bug, and nothing is recorded."""
    sandbox, image, marks = world
    found = await bisect_fix(
        sandbox, image, old=marks["LINE3_START"], new=marks["LINE3_FIX"], test_path=REPRO_PATH,
        test_code=REPRO,
    )
    assert found.status == "ambiguous", (found.reason, found.log)
    assert set(found.candidates) == {marks["LINE3_BROKEN"], marks["LINE3_FIX"]}
    assert found.commit is None
    assert found.steps == [(marks["LINE3_BROKEN"], 2)]  # pytest's collection error


async def test_a_test_that_fails_everywhere_gets_no_answer(world) -> None:
    sandbox, image, marks = world
    still_broken = REPRO.replace('["a", "b", "c"]', '["a", "b", "c", "d"]')
    found = await bisect_fix(
        sandbox, image, old=marks["C0"], new=marks["MAIN"], test_path=REPRO_PATH,
        test_code=still_broken,
    )
    assert found.status in ("unverified", "not_found"), found


async def test_a_fix_that_main_had_before_merging_the_release_line(world) -> None:
    """The reported version is on a release line; main rewrote the code before that line was
    merged into it (pallets/werkzeug#3312, 3.1.9 and `use match to remove port`). Among the
    commits after the reported one, the merge is the first that passes; the rewrite on main's
    side is named as the candidate, found by `git log -L` on the function the test imports."""
    sandbox, image, marks = world
    found = await bisect_fix(
        sandbox, image, old=marks["OLD4"], new=marks["NEW4"], test_path=REPRO_PATH,
        test_code=REPRO,
    )
    assert found.status == "found", (found.reason, found.log)
    assert found.commit == marks["MERGE4"] and found.merge
    assert found.parent == marks["OLD4"]  # the parent on the reported line, not main's
    assert found.range_count == 2  # the merge and the chore after it: what descends from OLD4
    assert [sha for sha, _ in found.other_side][:1] == [marks["REWRITE"]]
    assert found.other_side[0][1] == "rewrite parse"

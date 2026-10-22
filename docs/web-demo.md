# The web demo

https://fixedyet.benlevin.fyi/ runs the same pipeline behind a small FastAPI app: paste an issue URL, watch the stages live, read the report with the two-sided result and the test. Code runs in a Docker sandbox on a free 1 GB Oracle Cloud VM, so a run takes about 1.5 to 5 minutes; the Nebius Sandboxes backend is implemented and waiting for beta access. The server runs one issue at a time and stops at a daily limit (`FIXEDYET_DEMO_DAILY_LIMIT`), then points visitors at the saved reports.

## Who can run what

| Visitor | Runs | Counted against |
|---|---|---|
| anyone | live runs on public issues | the public daily limit (`FIXEDYET_DEMO_DAILY_LIMIT`, default 5) |
| a hackathon judge with a code | the same, under "Hackathon judge? Enter your code" | the judges' own daily quota (`FIXEDYET_JUDGE_DAILY_LIMIT`, default 20), and a budget the public cannot use (`FIXEDYET_JUDGE_RESERVE_USD`) |
| the maintainer (`FIXEDYET_ADMIN_TOKEN`) | runs, backports, fixes, draft pull requests, hiding a report | nothing but the total budget |

Every run is checked first against the scope: FixedYet runs pure-Python projects tested with pytest, so the server reads the repository's main language from the GitHub API and refuses a repository GitHub lists as another language (422) before anything is counted. A language GitHub does not know, or a lookup that fails, lets the run go ahead. It also reads the issue itself, one request at the same time, for every run including the maintainer's: a number GitHub has no issue for, or a pull request's number, is refused with 422 and counted nowhere. The URL may be pasted as it comes (http or no scheme, `www.`, any case in the host, a trailing slash, a query or an `#issuecomment-...` anchor); the run uses its plain https form. Judge codes are sent as an `X-Judge-Code` header, compared in constant time and never logged; [`deploy/README.md`](../deploy/README.md#judge-access) says how to set them.

All runs share the spend ledger's total cap (`FIXEDYET_TOTAL_BUDGET_USD`): when less than one run's budget is left, every run is refused.

## The reports page

The **All reports** page (`/reports`, linked from the header) lists every example and every finished run, one row per issue: the newest run is shown, and its earlier runs fold under it ("2 earlier runs"). Chips filter by verdict (All / Fixed on main / Open bug / Not reproduced / Other) and a search box by repository or title. A report opens at `reports#<id>`, which can be shared.

A report shows the verdict first, then what can be done about it, then the rest:

1. the verdict, its confidence and the rules that gave it;
2. the fix or the backport: a verified fix shows its diff unfolded, with the suite before and after and the other callers of the changed code; a backport shows its pull request when one was opened;
3. the two-sided result, the reproduction test, what is going on, the evidence, the cost and time, and the markdown report.

On a report that is fixed on main with a known fixing commit, the **Backport fix → draft PR** button tells a visitor that opening pull requests on the demo is limited to its maintainer, and shows the commands to run it themselves (`fixedyet backport <issue URL> --open-pr`, and with `--pr-target upstream` for the upstream project) with a link to a verified example.

## Maintainer mode

Setting `FIXEDYET_ADMIN_TOKEN` turns on maintainer mode: after signing in (the small "Maintainer sign-in" link at the bottom of the reports page) the owner works in two steps. **Verify backport** runs the cherry-pick, the reproduction test and the project's suite on the server and opens no pull request; the card then shows the facts, the patch to review, and a warning when the target branch looks inactive (no commit for 180 days, or a newer release line). A second request then opens a draft pull request in the owner's own fork, or on the upstream project after the owner types its name. On an open bug the maintainer gets **Attempt a fix** instead ([fix mode](fix-mode.md)): the same progress log, then the verified diff (light, with red and green line tints) and the same two pull request buttons. The owner can also hide a report. Without the token every maintainer endpoint answers 404. See [`deploy/README.md`](../deploy/README.md#maintainer-mode).

## The examples

The reports in [`examples/`](../examples) are saved output of real runs on real GitHub issues (Docker sandbox backend, Nemotron 3 Super through Token Factory), shown first on the demo page:

| Example | What it shows |
|---|---|
| [pallets/jinja#2244](https://github.com/pallets/jinja/issues/2244) | an open bug (still open upstream), and the one-line fix that fix mode verified on the live demo: 911 tests pass with and without it. The open upstream pull request [pallets/jinja#2259](https://github.com/pallets/jinja/pull/2259) makes the same check, after the jinja maintainer asked for it; no pull request was opened |
| [pydantic/pydantic#13991](https://github.com/pydantic/pydantic/issues/13991) | fixed on `main`; the fix verified on `v2.14-fixes` and opened upstream, after a human review, as [pydantic/pydantic#14043](https://github.com/pydantic/pydantic/pull/14043) |
| [pallets/click#3384](https://github.com/pallets/click/issues/3384) | fixed on `main`, with the fixing commit and the release that shipped it |
| [pytest-dev/pytest#13784](https://github.com/pytest-dev/pytest/issues/13784) | fixed on `main`; the fix verified on `8.4.x` and opened as a draft pull request in the owner's fork |

Each file names the issue it ran on, and an optional `_example` object sets the label, a short note, an `order` on the page and, for an example that carries its fix in `assessment.fix`, a `fix_note`. A saved `fixedyet backport --json` result in that folder (`real-backport-<owner>-<repo>-<number>.json` and `.md`) is listed and drawn as a backport report.

## Running it yourself

Run it locally with `fixedyet serve`. The page uses only relative URLs, so it works at the root of a host (the demo runs at the root of fixedyet.benlevin.fyi) and under a prefix: behind nginx's `location /fixedyet/ { proxy_pass http://127.0.0.1:8090/; }` (which strips the prefix), start it with `--root-path /fixedyet` or `FIXEDYET_ROOT_PATH=/fixedyet` so redirects carry the prefix. Deployment files for a small Ubuntu VM (nginx, systemd, a deploy script) are in [`deploy/`](../deploy/README.md).

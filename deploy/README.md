# Deploying the demo

The demo at https://fixedyet.benlevin.fyi/ is the FastAPI app (`fixedyet serve`, at the root of its host) behind the nginx of a small Oracle Cloud Ubuntu 24.04 x86 VM with 1 GB of RAM that already serves benlevin.fyi and other sites. It has its own server block, DNS record and Let's Encrypt certificate (set up once, see below). The old address, https://benlevin.fyi/fixedyet/, answers with a 308 redirect to the same path on the new host, so links shared before the move keep working. Everything here is written to leave the other sites alone.

| File | What it is |
|---|---|
| `deploy.sh` | run from your Mac: copies the repo, creates the user and venv, installs the unit and the nginx files, restarts the service |
| `fixedyet.service` | systemd unit, user `fixedyet`, `MemoryMax=300M`, hardened, the app at the root of its host |
| `fixedyet.nginx.conf` | the `location /` block of fixedyet.benlevin.fyi: proxies to `127.0.0.1:8090` (or `FIXEDYET_UPSTREAM`), unbuffered for server-sent events, `proxy_read_timeout 900s`. Installed as `/etc/nginx/snippets/fixedyet-site.conf` |
| `fixedyet-server.nginx.conf` | the fixedyet.benlevin.fyi server block, set up once by hand (commands at its top), then given HTTPS by `certbot --nginx`. deploy.sh never writes it |
| `fixedyet-redirect.nginx.conf` | the old address: a 308 from `benlevin.fyi/fixedyet/...` to `fixedyet.benlevin.fyi/...`. Installed as `/etc/nginx/snippets/fixedyet.conf`, which the benlevin.fyi server block includes |
| `fixedyet-limits.nginx.conf` | the http-level rate-limit zone for POST requests. Installed as `/etc/nginx/conf.d/fixedyet-limits.conf` |

## Where the app lives

The app runs at the root of its own host, https://fixedyet.benlevin.fyi/, so the browser asks for `/api/status` and the app sees `/api/status`: no prefix is stripped or added. The page itself uses only relative URLs (`static/app.js`, `api/runs`, never `/api/runs`), so the same app still works under a prefix elsewhere (`--root-path` / `FIXEDYET_ROOT_PATH`, see the table below).

The old address, `https://benlevin.fyi/fixedyet/...`, is a `308` to `https://fixedyet.benlevin.fyi/...` (same path and query), from the snippet the benlevin.fyi server block already includes.

## Before the first deploy

- benlevin.fyi already served by nginx over HTTPS on the server.
- SSH access as `ubuntu` with passwordless `sudo`. The script checks this and stops if it is missing.
- nginx installed on the server, with the stock `include /etc/nginx/conf.d/*.conf;` inside `http { }` (Ubuntu's package has it), and `python3.12` (Ubuntu 24.04 ships it). If `python3.12-venv` is missing the script installs that one package.

## Deploy

```bash
deploy/deploy.sh SERVER_ADDRESS            # first run, and every update after it
deploy/deploy.sh SERVER_ADDRESS --dry-run  # only list the files that would be copied
```

Optional environment variables: `DEPLOY_USER` (default `ubuntu`), `DEPLOY_SSH_KEY` (path to a key, no spaces), `DEPLOY_HOST` (instead of the argument).

What the script does, in order, and what it will not do:

1. Checks SSH and `sudo`, then `rsync`s the repository to `/opt/fixedyet`. It never copies `.venv`, `.env`, `runs/` or `.git`, and it never deletes them on the server.
2. Creates the system user `fixedyet` and `/opt/fixedyet/runs` (owned by that user, mode 750) on the first run.
3. Creates `/opt/fixedyet/.venv` with the server's `python3.12` on the first run, then `pip install -e .`.
4. Installs `/etc/systemd/system/fixedyet.service` and enables it.
5. Installs the three nginx files (`/etc/nginx/snippets/fixedyet-site.conf`, `/etc/nginx/snippets/fixedyet.conf` and `/etc/nginx/conf.d/fixedyet-limits.conf`), then runs `nginx -t`. nginx is reloaded only if the test passes. If it fails, the old copies are put back and the script stops.
6. Looks for both `include` lines below in the enabled nginx configuration and prints **ACTION NEEDED** when one is missing.
7. Restarts the service if `/etc/fixedyet.env` exists, then waits for `GET /healthz`. If the file is missing it prints what to do and does not start the service.

It never writes an nginx server block and never writes `/etc/fixedyet.env`.

## The nginx setup you do once yourself

**The fixedyet.benlevin.fyi server.** Add an A record `fixedyet` for benlevin.fyi pointing at the nginx host, then run the commands at the top of `deploy/fixedyet-server.nginx.conf`: copy the server block into `sites-available`, enable it, reload, and `sudo certbot --nginx -d fixedyet.benlevin.fyi --redirect`. certbot adds the HTTPS listener and renews the certificate on its own.

**The old address.** The script cannot know which file holds your benlevin.fyi server, and a wrong edit would break the other sites, so you add one line, once, inside that `server { }` block (the HTTPS one if there are two):

```nginx
server {
    server_name benlevin.fyi;
    # ... the existing configuration ...

    include snippets/fixedyet.conf;
}
```

Then check and reload:

```bash
sudo nginx -t && sudo systemctl reload nginx
curl -s https://fixedyet.benlevin.fyi/healthz        # {"status":"ok"}
```

The app's snippet is the whole `location /` of its own server. For server-sent events it sets `proxy_buffering off` and `proxy_read_timeout 900s`; the app sends a keepalive comment every 15 seconds, so an idle stream stays open.

## Secrets

The service reads its secrets from `/etc/fixedyet.env`, owned by root with mode 600. They never go in the repository. Create it once on the server:

```bash
sudo install -m 600 -o root -g root /dev/null /etc/fixedyet.env
sudo nano /etc/fixedyet.env
```

Copy the `KEY=value` lines from `.env.example` and fill them in. systemd does not understand inline comments, so **do not copy the `# ...` text after a value**: a line like `SOME_KEY=abc   # note` would give the key the value `abc   # note`. Put comments on their own lines, and use no quotes and no `export`.

| Variable | Required | Notes |
|---|---|---|
| `NEBIUS_API_KEY` | yes | Token Factory API key. Also used for Sandboxes unless `CONTREE_TOKEN` is set |
| `NEBIUS_BASE_URL` | no | default `https://api.tokenfactory.nebius.com/v1/` |
| `NEBIUS_PROJECT_ID` | if your project needs it | the Sandboxes client sends it as the `Project` header, see the note at the top of `src/fixedyet/sandbox/nebius.py` |
| `CONTREE_TOKEN` | no | only if the Sandboxes token differs from `NEBIUS_API_KEY` |
| `TAVILY_API_KEY` | recommended | without it the evidence step is skipped |
| `GITHUB_TOKEN` | recommended | lifts the GitHub rate limit, which an anonymous server reaches quickly. For maintainer mode it is also the token the draft pull requests are opened with: a classic personal access token with only the `public_repo` scope and a 30-day expiry (see [Maintainer mode](#maintainer-mode)). Without maintainer mode a fine-grained read-only token for public repositories is enough |
| `FIXEDYET_ADMIN_TOKEN` | no | turns on maintainer mode (reports page: verify a backport, attempt a fix of an open bug, open a draft PR, hide a report). A random secret of 32 bytes or more: `openssl rand -hex 32`. Empty or unset means off, and every maintainer endpoint answers 404 |
| `FIXEDYET_MODEL`, `FIXEDYET_SMALL_MODEL` | no | default to Nemotron 3 Super and Nano |
| `FIXEDYET_SANDBOX` | no | `nebius` (default) |
| `FIXEDYET_ROOT_PATH` | no | URL prefix the app is served under. Leave it out: the demo runs at the root of fixedyet.benlevin.fyi. Set it (or pass `--root-path`) only when nginx serves the app under a path |
| `FIXEDYET_KEEP_CHECKPOINTS` | no | only for the `docker` sandbox: how many of the newest `fixedyet-ckpt` images to keep after each run, default `6` |
| `FIXEDYET_RUN_BUDGET_USD` | no | hard cost cap per run, default `0.60` |
| `FIXEDYET_TOTAL_BUDGET_USD` | no | hard cap on model spend over the life of the install, default `20`, counted in the spend ledger (`FIXEDYET_LEDGER_PATH`). Token Factory charges the card when the balance goes negative, so this is the real spending limit |
| `FIXEDYET_LEDGER_PATH` | no | the spend ledger file. Leave it out of this file: the units set it (see [The spend ledger](#the-spend-ledger)) |
| `FIXEDYET_MAX_TURNS` | no | agent turns per run, default `20` |
| `FIXEDYET_DEMO_DAILY_LIMIT` | no | live runs per UTC day for the whole site, default `5`. This is the spending limit of the demo |
| `FIXEDYET_JUDGE_CODES` | no | comma-separated codes for hackathon judges, 12 characters or more each (`openssl rand -hex 8`); shorter ones are ignored. A visitor types one in under the run form ("Hackathon judge? Enter your code"); their runs then count against `FIXEDYET_JUDGE_DAILY_LIMIT` instead of the public limit. Sent as `X-Judge-Code`, compared in constant time, never logged. Empty or unset means off |
| `FIXEDYET_JUDGE_DAILY_LIMIT` | no | live runs per UTC day for all judge codes together, default `20` |
| `FIXEDYET_JUDGE_RESERVE_USD` | no | model budget kept for judges, default `0`: public runs are refused once less than this plus one run's budget is left under `FIXEDYET_TOTAL_BUDGET_USD`; judges and the maintainer may use it |
| `FIXEDYET_RUNS_DIR` | no | default `runs`, which is `/opt/fixedyet/runs` |

After editing the file, run `deploy/deploy.sh SERVER_ADDRESS` again (or `sudo systemctl restart fixedyet` on the server).

### The spend ledger

The lifetime model spend (`FIXEDYET_TOTAL_BUDGET_USD` is checked against it) lives in one JSON file. It used to be `<runs dir>/spend-ledger.json`, so pointing `FIXEDYET_RUNS_DIR` somewhere new started a fresh ledger at $0. It is now `FIXEDYET_LEDGER_PATH`, and without that variable a user-level file (`~/.local/state/fixedyet/spend-ledger.json`) that does not depend on the runs directory.

The server keeps its old file, so its accounting continues without a migration: `fixedyet.service` sets `FIXEDYET_LEDGER_PATH=/opt/fixedyet/runs/spend-ledger.json`, and the Docker host's drop-in (`fixedyet-docker-host.conf`) sets `/var/lib/fixedyet/runs/spend-ledger.json`, the file that holds the demo's spend so far. The service user's home is not writable under the unit's hardening, so the user-level default would not work there anyway. A `FIXEDYET_LEDGER_PATH` line in `/etc/fixedyet.env` would override the unit's value; leave it out. Check after a deploy that the total carried on:

```bash
systemctl show fixedyet -p Environment | tr ' ' '\n' | grep LEDGER   # the path the service uses
curl -s http://127.0.0.1:8090/api/status   # budget_remaining_usd: the cap minus the spend so far, not the whole cap
```

If a ledger is ever moved by hand, copy the file to the new path before the first run there. When the new file does not exist yet, FixedYet reads the old `<runs dir>/spend-ledger.json` of the current runs directory and writes its total to the new place on the next spend, so a move never resets the total.

Worst-case spend is (`FIXEDYET_DEMO_DAILY_LIMIT` + `FIXEDYET_JUDGE_DAILY_LIMIT`) times `FIXEDYET_RUN_BUDGET_USD` per day for model tokens, plus Tavily credits and sandbox time. Set the limit to what the credits can carry. Independently of both, `FIXEDYET_TOTAL_BUDGET_USD` stops all model calls once the ledger total reaches it; `GET /api/status` shows `budget_remaining_usd`.

## Maintainer mode

The public reports page (`/reports`) needs nothing. Maintainer mode adds controls for you only: **Verify backport** (or **Attempt a fix** on an open bug), then **Open draft PR on <owner/repo>** (after you type the repository name) or **Open in my fork only**, and **Hide / Unhide**. Your runs from the main page also skip the daily limit once you are signed in. It is off until `FIXEDYET_ADMIN_TOKEN` is set in `/etc/fixedyet.env`.

1. Generate the admin token and put it in the file: `FIXEDYET_ADMIN_TOKEN=$(openssl rand -hex 32)` (32 bytes or more). Keep a copy in your password manager: you paste it once into the "Maintainer sign-in" link at the bottom of the reports page, and the browser keeps it in localStorage. It is sent as `Authorization: Bearer <token>`, compared in constant time, and never logged or returned. A wrong or missing token is a 401 with no detail.
2. Create a **classic** personal access token for `GITHUB_TOKEN` with **only** the `public_repo` scope and an expiry you are comfortable with. It is used to fork the upstream project into the token owner's account, push a branch to that fork and open a **draft** pull request: in the fork, or on the upstream project when the maintainer typed the repository's name in the confirmation box (the request carries it as `confirm`; anything else is refused before GitHub is called). Renew the token before it expires; a job that finds no usable token fails at once, with a message, before any sandbox work. The token owner must not own the upstream repository (there would be no fork).
3. The server needs `git` (`sudo apt-get install -y git`): the draft pull request is pushed from the host with `git fetch`, `am` and `push` (hooks off, no project code run). The backport itself runs in the sandbox backend of the same server, so on the two-VM setup below the Docker VM needs `git` and these variables, not the proxy VM.
4. Restart: `deploy/deploy.sh SERVER_ADDRESS` or `sudo systemctl restart fixedyet`. Check: `curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/api/admin/check` answers `{"ok":true}`; without the variable it answers 404.

The work has two steps. **Verify backport** is a job that takes the same single slot as a live run (a second one is refused with 409) and does not count against `FIXEDYET_DEMO_DAILY_LIMIT`: the cherry-pick, the reproduction test and the project's suite, in the sandbox, with no pull request. It needs no model call, so it adds sandbox time and no token spend; `FIXEDYET_TOTAL_BUDGET_USD` is unchanged. After the verification it also reads, with `GITHUB_TOKEN`, when the target branch last got a commit and the project's latest release (best effort: a failure there is stored as null and never fails the job). Its result is saved in `/opt/fixedyet/runs/backports/<report id>.json`, and a verified result is not run again. **Opening the draft PR** (`POST /api/reports/<id>/backport/pr`, body `{"target": "fork"}` or `{"target": "upstream", "confirm": "<owner/repo>"}`) takes a few seconds, needs a verified result (409 `not_verified` otherwise) and the token (503 `no_github_token` without one), and returns the pull request, which it also saves; asking again returns the same one. Every pull request body ends with the fixed AI-assistance disclosure line, naming the token owner.

On an **open bug** the card offers **Attempt a fix** instead: the same kind of job (one slot, 409 when busy, not counted against the daily limit), but it does call the model, so it spends up to `FIXEDYET_FIX_BUDGET_USD` (default $0.25) through the same spend ledger, and it is refused with 429 when `FIXEDYET_TOTAL_BUDGET_USD` has less than that left. It runs fix mode (the README's "Fix mode" section) and saves to `/opt/fixedyet/runs/fixes/<report id>.json`; a verified fix is not run again. The card then shows the verified diff and the same two pull request buttons, which call `POST /api/reports/<id>/fix/pr` with the same body and the same rules. The pull request goes from `fixedyet/fix-<issue>` on the fork to the default branch and adds the reproduction test as a regression test.

## Judge access

Hackathon judges must be able to use the demo until the judging period ends, and the public daily limit could lock them out. Judge codes give them their own quota:

1. `openssl rand -hex 8` once per code (12 characters or more; shorter codes are ignored), then in `/etc/fixedyet.env`: `FIXEDYET_JUDGE_CODES=judge-<that>` (comma-separated for more than one), and `FIXEDYET_JUDGE_DAILY_LIMIT=20` if the default does not suit.
2. Optionally keep budget back from the public: `FIXEDYET_JUDGE_RESERVE_USD=6` refuses public runs once less than $6 plus one run's budget is left under `FIXEDYET_TOTAL_BUDGET_USD`. Judges and the maintainer can still spend it.
3. Restart, then check: `curl -s -H "X-Judge-Code: <code>" http://127.0.0.1:8090/api/judge/check` answers `{"ok": true, ...}`, and without the header `api/status` shows `"judge_access": true`.

Put the code in the private testing instructions of the submission, never in the repository. A judge types it under the run form ("Hackathon judge? Enter your code"); the browser keeps it in localStorage and sends it as `X-Judge-Code`. Every run, public or judge, first reads the repository's language from the GitHub API and is refused with 422 (and counted nowhere) when GitHub lists it as anything but Python. Every run, the maintainer's too, also reads the issue: a pull request's number, or a number with no issue behind it, is refused with 422 the same way.

## Live runs on a second VM (Docker sandbox)

Live runs need a sandbox. With the `docker` backend, the runs need more memory and disk than the VM that serves the website should give up, so they can run on a second VM in the same private subnet:

```sh
# on the Docker VM, once: swap, Docker, and a firewall rule for the proxy VM only
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
sudo apt-get install -y docker.io python3.12-venv iptables-persistent
sudo iptables -I INPUT 5 -s <proxy-private-ip>/32 -p tcp --dport 8090 -m state --state NEW -j ACCEPT
sudo netfilter-persistent save
# /etc/fixedyet.env as above, with FIXEDYET_SANDBOX=docker

# from your Mac
DEPLOY_DOCKER_HOST=1 deploy/deploy.sh <docker-vm-public-ip>
FIXEDYET_UPSTREAM=<docker-vm-private-ip>:8090 deploy/deploy.sh <proxy-vm-public-ip>
```

Inside each sandbox container the project's tests run as an unprivileged user (`fixedyet`, created by the baseline step; setup steps stay root), so the host needs no change for it. Checkpoint images built by an older version have no such user, and runs from them fall back to root; every run builds a fresh baseline, so there is nothing to clear.

Also allow TCP 8090 from the proxy VM's private IP in the cloud security list. The second command points the nginx snippet at the Docker VM, stops and disables the local service on the proxy VM, and checks the upstream's `/healthz`. To go back, run a plain `deploy/deploy.sh <proxy-vm-public-ip>`.

## Operating it

```bash
ssh ubuntu@SERVER_ADDRESS
sudo systemctl status fixedyet
sudo journalctl -u fixedyet -f                    # live log
curl -s http://127.0.0.1:8090/healthz             # {"status":"ok"}, the app directly, prefix already stripped
curl -s http://127.0.0.1:8090/api/status          # runs left today
curl -s https://fixedyet.benlevin.fyi/api/status  # the same through nginx
sudo tail -f /var/log/nginx/error.log             # or the error_log of the benlevin.fyi server
```

- Finished runs and the daily counter are JSON files in `/opt/fixedyet/runs/web/`, so a restart loses nothing. Delete the files to clear history. Each finished run's report is also a folder `/opt/fixedyet/runs/<timestamp>-<owner>-<repo>-<number>/`, which is what the reports page lists. Hidden report ids are in `/opt/fixedyet/runs/hidden.json`; delete a report's folder to remove it for good.
- If a run is in progress when the service restarts, that run is recorded as failed and the daily counter keeps its count.
- The unit allows 300 MB. On a 1 GB machine shared with other sites, check `systemctl status fixedyet` for the memory figure after a few runs.
- nginx limits POSTs to 6 a minute per address (zone `fixedyet_post`) on top of the application's own one-run-at-a-time and daily limits.
- To take the demo offline without touching the rest of benlevin.fyi, remove the `include snippets/fixedyet.conf;` line, reload nginx and `sudo systemctl stop fixedyet`.

## Rolling back

The server holds only the last deployed copy. To go back, check out the older commit on your Mac and run `deploy/deploy.sh SERVER_ADDRESS` again.

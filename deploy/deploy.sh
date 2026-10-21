#!/usr/bin/env bash
# Deploy FixedYet to the Oracle Cloud Ubuntu 24.04 VM. Run it from your Mac:
#
#   deploy/deploy.sh HOST [--dry-run]
#
# HOST is the server's address (or DEPLOY_HOST in the environment). The login user is
# "ubuntu" unless DEPLOY_USER is set; DEPLOY_SSH_KEY (a path without spaces) picks the key.
#
# The demo is served at https://benlevin.fyi/fixedyet/, inside the existing benlevin.fyi nginx
# server. Safe to run again and again. What it will and will not do:
#   - copies the repository to /opt/fixedyet (never .venv, .env, runs/ or .git)
#   - creates the system user "fixedyet" and the venv (python3.12) on the first run
#   - installs the systemd unit and two nginx files (a location snippet and a rate-limit zone)
#   - runs `nginx -t` and reloads nginx only when it passes, undoing its change when it fails
#   - NEVER edits an existing nginx server block: you add one `include` line to the
#     benlevin.fyi server once, and the script tells you when that is still missing
#   - NEVER writes /etc/fixedyet.env (your secrets)
set -euo pipefail

REMOTE_DIR=/opt/fixedyet
SERVICE=fixedyet
ENV_FILE=/etc/fixedyet.env
PORT=8090
PUBLIC_URL=https://benlevin.fyi/fixedyet/

HOST="${DEPLOY_HOST:-}"
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) HOST="$arg" ;;
  esac
done
if [ -z "$HOST" ]; then
  echo "usage: deploy/deploy.sh HOST [--dry-run]" >&2
  exit 2
fi

SSH_USER="${DEPLOY_USER:-ubuntu}"
TARGET="$SSH_USER@$HOST"
SSH_OPTS=(-o ServerAliveInterval=30)
if [ -n "${DEPLOY_SSH_KEY:-}" ]; then
  SSH_OPTS+=(-i "$DEPLOY_SSH_KEY" -o IdentitiesOnly=yes)
fi
ssh_run() { ssh "${SSH_OPTS[@]}" "$TARGET" "$@" </dev/null; }

cd "$(dirname "$0")/.."
if [ ! -f pyproject.toml ] || [ ! -d src/fixedyet ]; then
  echo "run this from a checkout of FixedYet" >&2
  exit 1
fi

RSYNC_ARGS=(
  -rltz --delete
  --include '/.env.example'
  --exclude '/.env' --exclude '/.env.*'
  --exclude '/.venv/' --exclude '/runs/' --exclude '.git/'
  --exclude '__pycache__/' --exclude '*.pyc' --exclude '.pytest_cache/' --exclude '.ruff_cache/'
  --exclude '.DS_Store' --exclude '/dist/' --exclude '/build/' --exclude '*.egg-info/'
  --exclude '/eval/results/' --exclude '/eval/cache/'
  -e "ssh ${SSH_OPTS[*]}"
)

if [ "$DRY_RUN" = 1 ]; then
  echo "== dry run: files that would be copied to $TARGET:$REMOTE_DIR =="
  rsync -n -v "${RSYNC_ARGS[@]}" ./ "$TARGET:$REMOTE_DIR/"
  exit 0
fi

echo "== checking access to $TARGET =="
ssh_run 'sudo -n true' || { echo "need passwordless sudo for $SSH_USER on $HOST" >&2; exit 1; }
ssh_run "sudo install -d -o '$SSH_USER' -g '$SSH_USER' -m 755 '$REMOTE_DIR'"

echo "== copying the repository =="
rsync "${RSYNC_ARGS[@]}" ./ "$TARGET:$REMOTE_DIR/"

echo "== installing on the server =="
ssh "${SSH_OPTS[@]}" "$TARGET" bash -s -- "$REMOTE_DIR" "$SERVICE" "$ENV_FILE" "$PORT" <<'REMOTE'
set -euo pipefail
REMOTE_DIR=$1; SERVICE=$2; ENV_FILE=$3; PORT=$4

# Files must be readable by the service user whatever their mode was on the Mac.
find "$REMOTE_DIR" \( -path "$REMOTE_DIR/runs" -o -path "$REMOTE_DIR/.venv" \) -prune \
  -o -exec chmod go+rX {} +

# 1. The service user and its private data directory.
if ! id -u fixedyet >/dev/null 2>&1; then
  echo "creating system user fixedyet"
  sudo useradd --system --home-dir "$REMOTE_DIR" --no-create-home --shell /usr/sbin/nologin fixedyet
fi
sudo install -d -o fixedyet -g fixedyet -m 750 "$REMOTE_DIR/runs"

# 2. The virtualenv, created once with the server's python3.12.
if ! command -v python3.12 >/dev/null 2>&1; then
  echo "python3.12 is missing. On Ubuntu 24.04: sudo apt install python3.12 python3.12-venv" >&2
  exit 1
fi
if [ ! -x "$REMOTE_DIR/.venv/bin/python" ]; then
  echo "creating the virtualenv"
  if ! python3.12 -m venv "$REMOTE_DIR/.venv" 2>/dev/null; then
    echo "installing python3.12-venv"
    sudo apt-get install -y python3.12-venv >/dev/null </dev/null
    rm -rf "$REMOTE_DIR/.venv"
    python3.12 -m venv "$REMOTE_DIR/.venv"
  fi
fi
"$REMOTE_DIR/.venv/bin/pip" install --quiet --no-cache-dir -e "$REMOTE_DIR" </dev/null
"$REMOTE_DIR/.venv/bin/python" -m compileall -q "$REMOTE_DIR/src" >/dev/null || true

# 3. The systemd unit.
sudo install -m 644 -o root -g root "$REMOTE_DIR/deploy/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1

# 4. nginx: a location snippet and a rate-limit zone. The existing benlevin.fyi server block
#    is never edited; it includes the snippet with one line that you add once.
if ! command -v nginx >/dev/null 2>&1; then
  echo "nginx is not installed; skipping the nginx files" >&2
else
  SNIPPET=/etc/nginx/snippets/fixedyet.conf
  LIMITS=/etc/nginx/conf.d/fixedyet-limits.conf
  undo_nginx() {  # put back what was there before this deploy touched nginx
    for pair in "$SNIPPET" "$LIMITS"; do
      if [ -e "$pair.bak" ]; then sudo mv -f "$pair.bak" "$pair"; else sudo rm -f "$pair"; fi
    done
  }
  for dest in "$SNIPPET" "$LIMITS"; do
    if [ -e "$dest" ]; then sudo cp -p "$dest" "$dest.bak"; fi
  done
  sudo install -d -m 755 /etc/nginx/snippets /etc/nginx/conf.d
  sudo install -m 644 -o root -g root "$REMOTE_DIR/deploy/fixedyet.nginx.conf" "$SNIPPET"
  sudo install -m 644 -o root -g root "$REMOTE_DIR/deploy/fixedyet-limits.nginx.conf" "$LIMITS"
  if sudo nginx -t; then
    sudo rm -f "$SNIPPET.bak" "$LIMITS.bak"
    sudo systemctl reload nginx
    echo "nginx files installed, nginx reloaded"
  else
    echo "nginx -t failed: nginx was NOT reloaded, undoing this deploy's nginx change" >&2
    undo_nginx
    exit 1
  fi
  if ! sudo grep -rqE 'include[[:space:]]+(/etc/nginx/)?snippets/fixedyet\.conf' \
      /etc/nginx/sites-enabled /etc/nginx/conf.d 2>/dev/null; then
    echo
    echo "ACTION NEEDED: add this line inside the existing benlevin.fyi server { } block:"
    echo "    include snippets/fixedyet.conf;"
    echo "then: sudo nginx -t && sudo systemctl reload nginx"
  fi
fi

# 5. Start the service, but only once the secrets file exists. It is never created here.
if [ ! -f "$ENV_FILE" ]; then
  echo
  echo "NOT STARTED: $ENV_FILE does not exist."
  echo "Create it from .env.example (see deploy/README.md), then run this script again:"
  echo "  sudo install -m 600 -o root -g root /dev/null $ENV_FILE && sudo nano $ENV_FILE"
  exit 0
fi
mode=$(sudo stat -c '%a' "$ENV_FILE")
if [ "$mode" != 600 ]; then
  echo "WARNING: $ENV_FILE has mode $mode; run: sudo chmod 600 $ENV_FILE" >&2
fi
sudo systemctl restart "$SERVICE"
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "service is up: $(curl -fsS "http://127.0.0.1:$PORT/healthz")"
    exit 0
  fi
  sleep 1
done
echo "the service did not answer on port $PORT within 20 seconds; last log lines:" >&2
sudo journalctl -u "$SERVICE" -n 30 --no-pager >&2
exit 1
REMOTE

cat <<DONE

== deployed ==
Open $PUBLIC_URL (benlevin.fyi already has DNS and HTTPS; this adds no new host).
If the script printed ACTION NEEDED, add the include line to the benlevin.fyi server first.
Logs: ssh $TARGET sudo journalctl -u $SERVICE -f
DONE

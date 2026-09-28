#!/usr/bin/env bash
# Commit and push local changes, then update the host machine over SSH.
#
#   scripts/deploy.sh [-m "commit message"] [--host user@machine] [--no-remote]
#
# --host is remembered in .deploy.env (not committed). Use the host's
# Tailscale name, e.g. --host me@studio-mac, so SSH goes through the tailnet.
# On the host the repo lives in ~/minimax-h3-studio and is set up by
# scripts/install.sh (Python, background service, tailscale serve).
set -euo pipefail

REPO_URL="https://github.com/Shaney17/minimax-h3-studio.git"
BRANCH="main"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/.deploy.env"
MESSAGE=""
REMOTE=1
DEPLOY_HOST="${DEPLOY_HOST:-}"
[ -f "$CONFIG" ] && [ -z "$DEPLOY_HOST" ] && . "$CONFIG"

while [ $# -gt 0 ]; do
  case "$1" in
    -m|--message) MESSAGE="$2"; shift 2 ;;
    --host) DEPLOY_HOST="$2"; printf 'DEPLOY_HOST=%q\n' "$2" > "$CONFIG"; shift 2 ;;
    --no-remote) REMOTE=0; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Tham số không hợp lệ: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mLỗi: %s\033[0m\n' "$*" >&2; exit 1; }
cd "$ROOT"

say "Kiểm tra code"
python3 -m py_compile web_ui.py scripts/check_workflow.py
if command -v node >/dev/null 2>&1; then node --check web/app.js; fi
bash -n scripts/install.sh scripts/deploy.sh
WORKER_DIR="${H3_WORKER_DIR:-$ROOT/../minimax-h3-cinematic-runpod}"
if [ -f "$WORKER_DIR/Dockerfile" ]; then
  python3 scripts/check_workflow.py --worker-dir "$WORKER_DIR"
else
  python3 scripts/check_workflow.py
fi

say "Commit và push lên GitHub"
if [ ! -d .git ]; then
  git init -q -b "$BRANCH"
fi
git remote get-url origin >/dev/null 2>&1 || git remote add origin "$REPO_URL"
git add -A
if git diff --cached --quiet; then
  echo "Không có thay đổi mới để commit."
else
  git commit -q -m "${MESSAGE:-Deploy $(date '+%Y-%m-%d %H:%M')}"
  git log -1 --oneline
fi
git fetch -q origin 2>/dev/null || true
if git rev-parse -q --verify "origin/$BRANCH" >/dev/null; then
  git pull -q --rebase origin "$BRANCH"
fi
git push -u origin "$BRANCH"

if [ "$REMOTE" -eq 0 ]; then
  echo "Bỏ qua cập nhật máy chủ (--no-remote)."
  exit 0
fi
[ -n "$DEPLOY_HOST" ] || fail "Chưa có máy đích. Chạy lại với --host user@ten-may-tailscale (hoặc --no-remote)."

say "Cập nhật máy $DEPLOY_HOST"
# -t lets install.sh ask for a sudo password on Linux when it needs one.
ssh -t "$DEPLOY_HOST" "bash -lc '
  set -e
  dir=\"\$HOME/minimax-h3-studio\"
  if [ -d \"\$dir/.git\" ]; then
    git -C \"\$dir\" pull --ff-only
  else
    git clone $REPO_URL \"\$dir\"
  fi
  \"\$dir/scripts/install.sh\" --no-pull
'"

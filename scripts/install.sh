#!/usr/bin/env bash
# Install or update MiniMax H3 Studio on the machine that hosts it.
#
#   scripts/install.sh [--port 8765] [--no-pull] [--no-serve]
#
# Run it again at any time to update: it pulls the latest code, restarts the
# background service, and keeps the Tailscale HTTPS proxy in place.
# Supports macOS (launchd agent) and Linux (systemd user service).
set -euo pipefail

APP="minimax-h3-studio"
LABEL="com.shaney17.${APP}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${H3_PORT:-8765}"
PULL=1
SERVE=1

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --no-pull) PULL=0; shift ;;
    --no-serve) SERVE=0; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Tham số không hợp lệ: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;33m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mLỗi: %s\033[0m\n' "$*" >&2; exit 1; }
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *) fail "Chỉ hỗ trợ macOS và Linux (máy này: $OS)." ;;
esac

# ---------------------------------------------------------------- code
if [ "$PULL" -eq 1 ] && [ -d "$REPO_DIR/.git" ]; then
  say "Cập nhật code từ GitHub"
  if [ -n "$(git -C "$REPO_DIR" status --porcelain --untracked-files=no)" ]; then
    echo "Có thay đổi chưa commit trong $REPO_DIR; bỏ qua git pull."
  else
    git -C "$REPO_DIR" pull --ff-only
  fi
fi

# ---------------------------------------------------------------- python
python_ok() {
  "$1" - <<'PY' 2>/dev/null
import sqlite3, sys
ok = sys.version_info >= (3, 10) and sqlite3.sqlite_version_info >= (3, 24, 0)
sys.exit(0 if ok else 1)
PY
}

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && python_ok "$(command -v "$candidate")"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

say "Kiểm tra Python 3.10+"
if ! PYTHON="$(find_python)"; then
  echo "Chưa có Python 3.10+ (kèm SQLite 3.24+). Đang cài…"
  if [ "$OS" = "Darwin" ]; then
    command -v brew >/dev/null 2>&1 || fail "Cần Homebrew (https://brew.sh) hoặc cài Python từ https://www.python.org/downloads/ rồi chạy lại."
    brew install python@3.12
  elif command -v apt-get >/dev/null 2>&1; then
    as_root apt-get update
    as_root apt-get install -y python3
  elif command -v dnf >/dev/null 2>&1; then
    as_root dnf install -y python3
  else
    fail "Không nhận ra trình quản lý gói. Hãy cài Python 3.10+ rồi chạy lại."
  fi
  hash -r
  PYTHON="$(find_python)" || fail "Đã cài nhưng vẫn không tìm thấy Python 3.10+. Hãy kiểm tra PATH."
fi
echo "Dùng $PYTHON ($("$PYTHON" --version 2>&1))"

say "Kiểm tra ffmpeg (lấy khung cuối và ghép phim)"
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  echo "Chưa có ffmpeg. Đang cài…"
  if [ "$OS" = "Darwin" ]; then
    command -v brew >/dev/null 2>&1 || fail "Cần Homebrew (https://brew.sh) để cài ffmpeg, hoặc tự cài ffmpeg rồi chạy lại."
    brew install ffmpeg
  elif command -v apt-get >/dev/null 2>&1; then
    as_root apt-get update
    as_root apt-get install -y ffmpeg
  elif command -v dnf >/dev/null 2>&1; then
    as_root dnf install -y ffmpeg || fail "dnf không cài được ffmpeg (có thể cần bật RPM Fusion). Hãy tự cài ffmpeg rồi chạy lại."
  else
    fail "Không nhận ra trình quản lý gói. Hãy cài ffmpeg rồi chạy lại."
  fi
  hash -r
fi
FFMPEG="$(command -v ffmpeg)" || fail "Không tìm thấy ffmpeg sau khi cài."
command -v ffprobe >/dev/null 2>&1 || fail "Không tìm thấy ffprobe (thường đi kèm ffmpeg)."
echo "Dùng $FFMPEG ($("$FFMPEG" -version | head -1))"
# Background services start with a minimal PATH; give them the ffmpeg directory.
SERVICE_PATH="$(dirname "$FFMPEG"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

say "Kiểm tra code"
"$PYTHON" -m py_compile "$REPO_DIR/web_ui.py"
"$PYTHON" "$REPO_DIR/scripts/check_workflow.py"

# ---------------------------------------------------------------- tailscale
TAILSCALE=""
for candidate in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
  if command -v "$candidate" >/dev/null 2>&1; then TAILSCALE="$(command -v "$candidate")"; break; fi
done

TS_NAME=""
if [ "$SERVE" -eq 1 ]; then
  say "Kiểm tra Tailscale"
  if [ -z "$TAILSCALE" ]; then
    if [ "$OS" = "Darwin" ]; then
      fail "Chưa cài Tailscale. Cài app từ https://tailscale.com/download/mac, đăng nhập, rồi chạy lại. (Dùng --no-serve để bỏ qua.)"
    fi
    echo "Chưa cài Tailscale. Cài bằng script chính thức của Tailscale…"
    curl -fsSL https://tailscale.com/install.sh | sh
    TAILSCALE="$(command -v tailscale)"
  fi
  TS_NAME="$("$TAILSCALE" status --json 2>/dev/null | "$PYTHON" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
if data.get("BackendState") == "Running":
    print((data.get("Self") or {}).get("DNSName", "").rstrip("."))
')" || true
  if [ -z "$TS_NAME" ]; then
    if [ "$OS" = "Linux" ]; then
      echo "Máy chưa đăng nhập Tailscale. Chạy đăng nhập (mở link hiện ra để xác thực)…"
      as_root "$TAILSCALE" up
      TS_NAME="$("$TAILSCALE" status --json | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
    else
      fail "Tailscale chưa kết nối. Mở app Tailscale, đăng nhập, rồi chạy lại."
    fi
  fi
  [ -n "$TS_NAME" ] || fail "Không đọc được tên MagicDNS của máy. Hãy bật MagicDNS trong trang quản trị Tailscale."
  echo "Tên máy trong tailnet: $TS_NAME"
fi

# ---------------------------------------------------------------- service
ARGS=("$PYTHON" "$REPO_DIR/web_ui.py" --port "$PORT")
[ -n "$TS_NAME" ] && ARGS+=(--allow-host "$TS_NAME")

if [ "$OS" = "Darwin" ]; then
  say "Cài dịch vụ launchd"
  PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
  LOG="$HOME/Library/Logs/${APP}.log"
  mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
  {
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>${LABEL}</string>"
    echo '  <key>ProgramArguments</key><array>'
    for arg in "${ARGS[@]}"; do printf '    <string>%s</string>\n' "$arg"; done
    echo '  </array>'
    echo "  <key>WorkingDirectory</key><string>${REPO_DIR}</string>"
    echo "  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string><key>PATH</key><string>${SERVICE_PATH}</string></dict>"
    echo '  <key>RunAtLoad</key><true/>'
    echo '  <key>KeepAlive</key><true/>'
    echo "  <key>StandardOutPath</key><string>${LOG}</string>"
    echo "  <key>StandardErrorPath</key><string>${LOG}</string>"
    echo '</dict></plist>'
  } > "$PLIST"
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  launchctl kickstart -k "gui/$(id -u)/${LABEL}"
  LOG_HINT="tail -f $LOG"
else
  say "Cài dịch vụ systemd (user)"
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  EXEC=""
  for arg in "${ARGS[@]}"; do EXEC+=" $(printf '%q' "$arg")"; done
  cat > "$UNIT_DIR/${APP}.service" <<UNIT
[Unit]
Description=MiniMax H3 Studio
After=network-online.target

[Service]
WorkingDirectory=${REPO_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PATH=${SERVICE_PATH}
ExecStart=${EXEC# }
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable "${APP}.service" >/dev/null
  systemctl --user restart "${APP}.service"
  # Keep the user service running after logout and start it at boot.
  if [ "$(loginctl show-user "$USER" --property=Linger --value 2>/dev/null)" != "yes" ]; then
    as_root loginctl enable-linger "$USER" || echo "Không bật được linger; dịch vụ chỉ chạy khi bạn đăng nhập."
  fi
  LOG_HINT="journalctl --user -u ${APP} -f"
fi

say "Chờ server khởi động"
"$PYTHON" - "$PORT" <<'PY' || fail "Server không phản hồi. Xem log: $LOG_HINT"
import sys, time, urllib.request
url = f"http://127.0.0.1:{sys.argv[1]}/api/workflow"
for _ in range(30):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status == 200:
                sys.exit(0)
    except OSError:
        time.sleep(1)
sys.exit(1)
PY
echo "Server chạy tại http://127.0.0.1:${PORT}"

# ---------------------------------------------------------------- tailscale serve
if [ "$SERVE" -eq 1 ]; then
  say "Chia sẻ trong tailnet bằng tailscale serve (không dùng Funnel, không public)"
  SERVE_CMD=("$TAILSCALE" serve --bg --https=443 "http://127.0.0.1:${PORT}")
  if [ "$OS" = "Linux" ] && [ "$(id -u)" -ne 0 ]; then SERVE_CMD=(sudo "${SERVE_CMD[@]}"); fi
  if ! "${SERVE_CMD[@]}"; then
    fail "tailscale serve thất bại. Bật MagicDNS và HTTPS Certificates tại https://login.tailscale.com/admin/dns rồi chạy lại."
  fi
  "$TAILSCALE" serve status || true
fi

say "Hoàn tất"
if [ -n "$TS_NAME" ]; then
  echo "Mở từ thiết bị trong tailnet: https://${TS_NAME}"
fi
echo "Trên máy này:                  http://127.0.0.1:${PORT}"
echo "Dữ liệu:                        ${REPO_DIR}/data"
echo "Xem log:                        ${LOG_HINT}"

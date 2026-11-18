#!/usr/bin/env bash
# Install / refresh every JobHunter systemd user unit. Safe to re-run any time.
#   Review Center (always on) · Telegram bot (always on) · cloud sync every 30 min
#   laptop scan every 2 h · morning brief 07:30 · encrypted backup Sundays 21:00
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
for u in "$REPO"/scripts/systemd/*.service "$REPO"/scripts/systemd/*.timer; do
  sed "s#%h/jobhunter#$REPO#g" "$u" > "$UNIT_DIR/$(basename "$u")"
done
# the Review Center's Approve button opens a visible browser: give services your display
systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now jobhunter-review.service jobhunter-sync.timer jobhunter-scan.timer \
  jobhunter-morning.timer jobhunter-backup.timer
if grep -qE '^TELEGRAM_CHAT_ID=.+' "$REPO/.env" 2>/dev/null; then
  systemctl --user enable --now jobhunter-bot.service
else
  echo "· Telegram not set yet: bot service skipped (re-run this script after adding TELEGRAM_CHAT_ID)"
fi
systemctl --user restart jobhunter-review.service
echo
systemctl --user list-timers 'jobhunter-*' --no-pager || true
echo
echo "Review Center: http://localhost:8765"

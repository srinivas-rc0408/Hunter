#!/usr/bin/env bash
# One-shot setup for Arch Linux (works on any Linux/macOS with Python 3.10+).
#   git clone git@github.com:srinivas-rc0408/jobhunter.git ~/jobhunter && ~/jobhunter/scripts/install.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
echo "▸ Python venv"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e .
echo "▸ Chromium for Playwright"
.venv/bin/python -m playwright install chromium
[ -f .env ] || cp .env.example .env
echo "▸ hunt on your PATH"
if command -v fish >/dev/null 2>&1; then
  fish -c "fish_add_path -U $REPO/.venv/bin" && echo "  fish: added $REPO/.venv/bin"
fi
for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  [ -f "$rc" ] && ! grep -q "jobhunter/.venv/bin" "$rc" && echo "export PATH=\"$REPO/.venv/bin:\$PATH\"" >> "$rc" || true
done
echo "▸ Claude Code skills (global)"
bash scripts/install-skills.sh
echo "▸ systemd user timers (laptop scans every 2 h, cloud sync every 30 min)"
if command -v systemctl >/dev/null 2>&1; then
  mkdir -p "$HOME/.config/systemd/user"
  for u in scripts/systemd/*.service scripts/systemd/*.timer; do
    sed "s#%h/jobhunter#$REPO#g" "$u" > "$HOME/.config/systemd/user/$(basename "$u")"
  done
  systemctl --user daemon-reload
  systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XDG_RUNTIME_DIR 2>/dev/null || true
  systemctl --user enable --now jobhunter-sync.timer jobhunter-scan.timer jobhunter-review.service || true
  echo "  Review Center: http://localhost:8765 (bookmark it)"
fi
echo
.venv/bin/hunt doctor || true
cat <<'NEXT'

Next steps (5 minutes):
  1. Telegram: @BotFather → /newbot → paste token in .env → message your bot → `hunt telegram-id` → paste id in .env
  2. `hunt doctor --test-alert`         (a test alert should reach your phone)
  3. `hunt login linkedin` and `hunt login naukri`   (log in once in the window that opens)
  4. `hunt scan --include-existing`     (first scan; treats current govt notices as new)
  5. `hunt apply --dry-run --top 3`     (watch it fill, nothing is submitted)
  6. `hunt apply --top 10`              (the real thing)
NEXT

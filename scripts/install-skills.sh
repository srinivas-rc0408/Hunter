#!/usr/bin/env bash
# Install the 11 hunter-* skills + the jobhunter skill GLOBALLY for Claude Code
# (~/.claude/skills), so every project and every session can use them.
# Re-run any time to update. Uses symlinks, so `git pull` updates them too.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
mkdir -p "$DEST"
for d in "$REPO"/skills/hunter-* "$REPO"/skills/jobhunter; do
  name="$(basename "$d")"
  if [ -e "$DEST/$name" ] && [ ! -L "$DEST/$name" ]; then
    echo "· $name exists as a real folder in $DEST; backing it up to $name.bak"
    mv "$DEST/$name" "$DEST/$name.bak.$(date +%s)"
  fi
  ln -sfn "$d" "$DEST/$name"
  echo "✓ $name → $DEST/$name"
done
echo
echo "Done. In Claude Code, type /hunter-scout, /hunter-resume, /jobhunter, … (restart Claude Code if it was open)."
echo "No Claude Code? Paste any skills/<name>/SKILL.md at the top of a claude.ai chat and it runs as a mode."

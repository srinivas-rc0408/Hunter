#!/usr/bin/env bash
# Local LLM for JobHunter: installs Ollama, starts it at boot, pulls the model that fits your
# GPU, and writes OLLAMA_MODEL to .env. Works offline afterwards; no key, no quota, no expiry.
#   ./scripts/install-local-llm.sh            # pick by VRAM (4 GB → qwen3:4b)
#   ./scripts/install-local-llm.sh phi4-mini  # or name a model yourself
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

vram_mb=0
if command -v nvidia-smi >/dev/null 2>&1; then
  vram_mb=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc 0-9 || echo 0)
  vram_mb=${vram_mb:-0}
fi
echo "▸ GPU memory: ${vram_mb} MB"

if ! command -v ollama >/dev/null 2>&1; then
  echo "▸ Installing Ollama"
  if command -v pacman >/dev/null 2>&1; then
    if [ "$vram_mb" -gt 0 ]; then sudo pacman -S --needed --noconfirm ollama-cuda
    else sudo pacman -S --needed --noconfirm ollama; fi
  else
    curl -fsSL https://ollama.com/install.sh | sh
  fi
fi

echo "▸ Starting the Ollama service (and at every boot)"
sudo systemctl enable --now ollama.service
for _ in $(seq 1 30); do curl -fsS http://localhost:11434/api/version >/dev/null 2>&1 && break; sleep 1; done
curl -fsS http://localhost:11434/api/version >/dev/null || { echo "✗ Ollama didn't start: journalctl -u ollama -n 50"; exit 1; }

# Model weights + working memory must fit in VRAM, or it spills to CPU and gets ~5x slower.
#   ≤ 6 GB (your 4 GB card): qwen3:4b  ~2.5 GB   best quality that fits fully
#   ≥ 7.5 GB               : qwen3:8b  ~5.2 GB
#   no NVIDIA GPU           : qwen3:4b on CPU (works, slower)
model="${1:-}"
if [ -z "$model" ]; then
  if [ "$vram_mb" -ge 7500 ]; then model="qwen3:8b"; else model="qwen3:4b"; fi
fi
echo "▸ Pulling $model"
ollama pull "$model"

touch .env
if grep -q '^OLLAMA_MODEL=' .env; then sed -i "s#^OLLAMA_MODEL=.*#OLLAMA_MODEL=$model#" .env
else echo "OLLAMA_MODEL=$model" >> .env; fi
echo "▸ .env: OLLAMA_MODEL=$model"

echo "▸ Test (first answer loads the model, ~10-30 s)"
ollama run "$model" "Reply with one word: ready /no_think" | sed 's/<think>//;s#</think>##' | tr -s '\n' | head -3
ollama ps || true
echo
echo "✓ Done. 'hunt llm --test' checks the whole chain (NVIDIA → Ollama → Gemini → Groq)."
echo "  'ollama ps' should say 100% GPU while a model is loaded."

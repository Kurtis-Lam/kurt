#!/usr/bin/env bash
# Removes Kurt (macOS / Linux)
rm -f "$HOME/.local/bin/kurt"
rm -rf "$HOME/.kurt"
echo "✅ Kurt removed. (Your Ollama model is untouched; remove it with: ollama rm <model>)"
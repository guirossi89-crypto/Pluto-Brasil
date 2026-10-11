#!/usr/bin/env bash
# Gera a lista M3U do Pluto TV Brasil localmente.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Gerando lista VOD Pluto TV Brasil..."
python3 gerar_pluto_vod.py "$@"

if [[ "${1:-}" == "--push" ]]; then
  shift
  echo "==> Enviando para o GitHub..."
  git add playlists/pluto_vod.m3u
  if git diff --staged --quiet; then
    echo "Nenhuma mudança para enviar."
    exit 0
  fi
  git commit -m "Atualização manual da lista VOD Pluto TV ($(date -u '+%Y-%m-%d %H:%M UTC'))"
  git push origin HEAD:main
  echo "Push concluído."
fi

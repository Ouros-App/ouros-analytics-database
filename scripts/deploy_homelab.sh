#!/usr/bin/env bash
set -Eeuo pipefail

repo_dir="${OUROS_ANALYTICS_REPO_DIR:-$HOME/ouros-analytics-database}"
branch="${OUROS_ANALYTICS_BRANCH:-main}"

if [[ ! -d "$repo_dir/.git" ]]; then
  echo "Repositorio nao encontrado em $repo_dir" >&2
  exit 1
fi

cd "$repo_dir"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "Deploy abortado: existem alteracoes locais versionadas no homelab." >&2
  git status --short
  exit 1
fi

echo "Atualizando $repo_dir para origin/$branch..."
git fetch --prune origin "$branch"
git checkout "$branch"
git reset --hard "origin/$branch"

uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" && -x "$HOME/.local/bin/uv" ]]; then
  uv_bin="$HOME/.local/bin/uv"
fi

if [[ -z "$uv_bin" ]]; then
  echo "uv nao encontrado no homelab." >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  "$uv_bin" python install 3.12
  "$uv_bin" venv --python 3.12 .venv
fi

"$uv_bin" pip install --python .venv/bin/python -r requirements.txt

echo "Deploy concluido em $(git rev-parse --short HEAD)."
echo "O systemd timer existente usara este codigo no proximo sync."

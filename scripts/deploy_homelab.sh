#!/usr/bin/env bash
set -Eeuo pipefail

deploy_sha="${1:-}"
repo_dir="${OUROS_ANALYTICS_REPO_DIR:-$HOME/ouros-analytics-database}"
branch="${OUROS_ANALYTICS_BRANCH:-main}"

if [[ ! "$deploy_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "SHA de deploy invalido: '$deploy_sha'" >&2
  exit 1
fi

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

uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" && -x "$HOME/.local/bin/uv" ]]; then
  uv_bin="$HOME/.local/bin/uv"
fi
if [[ -z "$uv_bin" ]]; then
  echo "uv nao encontrado no homelab." >&2
  exit 1
fi

echo "Buscando origin/$branch e validando o commit $deploy_sha..."
git fetch --prune origin "$branch"

if ! git cat-file -e "${deploy_sha}^{commit}" 2>/dev/null; then
  echo "Commit $deploy_sha nao ficou disponivel apos o fetch." >&2
  exit 1
fi

if ! git merge-base --is-ancestor "$deploy_sha" "origin/$branch"; then
  echo "Commit $deploy_sha nao pertence ao historico atual de origin/$branch." >&2
  exit 1
fi

previous_sha="$(git rev-parse HEAD)"
stage_root="$(mktemp -d)"
stage_src="$stage_root/source"
candidate_venv="$repo_dir/.venv.candidate.$$"
backup_venv="$repo_dir/.venv.previous.$$"

cleanup() {
  if [[ -d "$stage_src" ]]; then
    git worktree remove --force "$stage_src" >/dev/null 2>&1 || true
  fi
  rm -rf "$stage_root"
  if [[ -d "$candidate_venv" ]]; then
    rm -rf "$candidate_venv"
  fi
}
trap cleanup EXIT

echo "Preparando a revisao antes de ativa-la..."
git worktree add --detach "$stage_src" "$deploy_sha" >/dev/null

use_candidate_venv=1
if [[ -x "$repo_dir/.venv/bin/python" ]] &&
   git diff --quiet "$previous_sha" "$deploy_sha" -- requirements.txt; then
  use_candidate_venv=0
  test_python="$repo_dir/.venv/bin/python"
else
  "$uv_bin" python install 3.12
  "$uv_bin" venv --python 3.12 "$candidate_venv"
  "$uv_bin" pip install \
    --python "$candidate_venv/bin/python" \
    -r "$stage_src/requirements.txt"
  test_python="$candidate_venv/bin/python"
fi

(
  cd "$stage_src"
  "$test_python" -m unittest discover -s tests -v
)

conflicts=()
while IFS= read -r -d '' path; do
  if git cat-file -e "$deploy_sha:$path" 2>/dev/null; then
    conflicts+=("$path")
  fi
done < <(git ls-files -z --others --exclude-standard)

if (( ${#conflicts[@]} > 0 )); then
  echo "Deploy abortado: arquivos locais nao rastreados seriam sobrescritos:" >&2
  printf '  %s\n' "${conflicts[@]}" >&2
  exit 1
fi

echo "Ativando exatamente $deploy_sha..."
if ! git checkout -B "$branch" "$deploy_sha"; then
  echo "Nao foi possivel ativar o commit; o deploy anterior foi preservado." >&2
  exit 1
fi

if (( use_candidate_venv == 1 )); then
  if [[ -e "$repo_dir/.venv" || -L "$repo_dir/.venv" ]]; then
    mv "$repo_dir/.venv" "$backup_venv"
  fi

  if ! mv "$candidate_venv" "$repo_dir/.venv"; then
    echo "Falha ao ativar o ambiente Python; revertendo codigo." >&2
    git checkout -B "$branch" "$previous_sha" || true
    if [[ -e "$backup_venv" || -L "$backup_venv" ]]; then
      mv "$backup_venv" "$repo_dir/.venv" || true
    fi
    exit 1
  fi

  rm -rf "$backup_venv"
fi

deployed_sha="$(git rev-parse HEAD)"
if [[ "$deployed_sha" != "$deploy_sha" ]]; then
  echo "SHA ativo inesperado: $deployed_sha" >&2
  git checkout -B "$branch" "$previous_sha" || true
  exit 1
fi

echo "Deploy concluido em $(git rev-parse --short HEAD)."
echo "O systemd timer existente usara esta revisao no proximo sync."

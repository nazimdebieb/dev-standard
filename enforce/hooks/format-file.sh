#!/usr/bin/env bash
# STY-001: format each file right after the agent writes it. Never blocks.
# PostToolUse hook, matcher Edit|Write|MultiEdit.
source "$(dirname "$0")/lib.sh"
ds_need_jq
ds_read_input

file=$(ds_file_path)
[[ -n "$file" && -f "$file" && "$file" == "$DS_ROOT"/* ]] || exit 0

first_tool() {
  local candidate
  for candidate in "$@"; do
    if [[ -x "$candidate" ]]; then printf '%s' "$candidate"; return 0; fi
    if command -v "$candidate" >/dev/null 2>&1; then command -v "$candidate"; return 0; fi
  done
  return 1
}

case "$file" in
  *.py | *.pyi)
    if ruff=$(first_tool "$DS_ROOT/.venv/bin/ruff" ruff); then "$ruff" format --quiet "$file" || true; fi
    ;;
  *.ts | *.tsx | *.js | *.jsx | *.mjs | *.cjs | *.json | *.jsonc | *.css)
    if biome=$(first_tool "$DS_ROOT/node_modules/.bin/biome"); then "$biome" format --write "$file" >/dev/null 2>&1 || true; fi
    ;;
  *.c | *.h | *.cc | *.cpp | *.hpp)
    if [[ -f "$DS_ROOT/.clang-format" ]] && cf=$(first_tool clang-format); then "$cf" -i "$file" || true; fi
    ;;
esac
exit 0

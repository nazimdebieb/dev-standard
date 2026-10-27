#!/usr/bin/env bash
# Shared helpers for dev-standard agent hooks. Sourced by each hook, not run directly.
# Claude Code hook contract: JSON arrives on stdin; exit 2 blocks and shows stderr to the
# agent; other non-zero codes show stderr to the user without blocking. PreToolUse and
# PostToolUse hooks can also print hookSpecificOutput.additionalContext on exit 0, which
# reaches the agent without blocking; warn mode uses that.
set -euo pipefail

# ds_norm_path <path>: one spelling per path. On Windows, Claude Code sends C:\x\y while
# bash may say /c/x/y (Git Bash, MSYS2) or /cygdrive/c/x/y (Cygwin), so cygpath turns
# them all into C:/x/y.
# Elsewhere only backslashes change, which no Linux or macOS payload path contains.
ds_norm_path() {
  local path=${1//\\//}
  if [[ -n "$path" ]] && command -v cygpath >/dev/null 2>&1; then
    path=$(cygpath -m -- "$path")
  fi
  printf '%s' "${path%/}"
}

DS_ROOT=$(ds_norm_path "${CLAUDE_PROJECT_DIR:-$(pwd)}")
DS_MANIFEST="$DS_ROOT/.dev-standard.json"
DS_INPUT=""

# ds_jq <arg>...: jq with LF line endings. jq for Windows prints CRLF, and the trailing \r
# breaks every comparison in bash: "backend/\r" never matches "backend/". It also drops
# \r inside values, which the hooks only match patterns against.
ds_jq() { jq "$@" | tr -d '\r'; }

ds_need_jq() {
  if ! command -v jq >/dev/null 2>&1; then
    echo "dev-standard: jq is not installed, so $(basename "$0") was skipped. Install jq to enable it." >&2
    exit 1
  fi
}

# Read the hook payload once. Repos that haven't adopted the standard (no .dev-standard.json)
# are left alone, so the hooks are safe to load globally through the plugin.
ds_read_input() {
  DS_INPUT=$(cat)
  [[ -f "$DS_MANIFEST" ]] || exit 0
}

# ds_field <jq-filter>: a field of the hook payload, empty when missing.
ds_field() { ds_jq -r "$1 // empty" <<<"$DS_INPUT"; }

# ds_file_path: the tool's target file, with ds_norm_path's spelling.
ds_file_path() { ds_norm_path "$(ds_field '.tool_input.file_path')"; }

# ds_rel_path: the tool's target file, relative to the project root.
ds_rel_path() {
  local file
  file=$(ds_file_path)
  printf '%s' "${file#"$DS_ROOT"/}"
}

# ds_new_text: all text the Edit, Write or MultiEdit call would write.
ds_new_text() {
  ds_jq -r '[.tool_input.content, .tool_input.new_string, (.tool_input.edits[]?.new_string)]
         | map(select(. != null)) | join("\n")' <<<"$DS_INPUT"
}

# ds_conf <jq-filter> <default>: a value from .dev-standard.json.
ds_conf() {
  local value=""
  if [[ -f "$DS_MANIFEST" ]]; then
    value=$(ds_jq -r "$1 // empty" "$DS_MANIFEST" 2>/dev/null || true)
  fi
  printf '%s' "${value:-$2}"
}

# ds_level <PREFIX>: block | warn | off for a rule group.
ds_level() {
  ds_conf ".enforcement.groups.\"$1\"" "$(ds_conf '.enforcement.default' 'block')"
}

# ds_waived <RULE-ID> <relative-path>: succeeds when an unexpired waiver covers the path.
ds_waived() {
  [[ -f "$DS_MANIFEST" ]] || return 1
  jq -e --arg rule "$1" --arg path "$2" --arg today "$(date +%F)" '
    any(.waivers[]?; . as $w
      | $w.rule == $rule
      and (($w.expires // "9999-12-31") >= $today)
      and ($path | startswith($w.scope // "")))' "$DS_MANIFEST" >/dev/null 2>&1
}

# ds_fail <PREFIX> <line>...: block, warn or ignore, per the group's enforcement level.
ds_fail() {
  local prefix=$1; shift
  case "$(ds_level "$prefix")" in
    off) exit 0 ;;
    warn) ds_warn "$@" "(warning only: $prefix is set to warn in .dev-standard.json)" ;;
    *) printf '%s\n' "$@" >&2; exit 2 ;;
  esac
}

# ds_warn <line>...: tell the agent without blocking. Tool hooks use additionalContext;
# other events fall back to a non-blocking stderr notice for the user.
ds_warn() {
  local event message
  event=$(ds_field '.hook_event_name')
  message=$(printf '%s\n' "$@")
  if [[ "$event" == "PreToolUse" || "$event" == "PostToolUse" ]]; then
    jq -n --arg e "$event" --arg m "$message" '{hookSpecificOutput: {hookEventName: $e, additionalContext: $m}}'
    exit 0
  fi
  printf '%s\n' "$message" >&2
  exit 1
}

# ds_ask <reason>: hand the decision to the user through Claude Code's permission prompt.
ds_ask() {
  jq -n --arg r "$1" '{hookSpecificOutput: {hookEventName: "PreToolUse",
    permissionDecision: "ask", permissionDecisionReason: $r}}'
  exit 0
}

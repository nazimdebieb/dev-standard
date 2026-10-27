#!/usr/bin/env bash
# SDD-001: no source edits on a Spec Kit feature branch (NNN-slug) until its spec is approved.
# PreToolUse hook, matcher Edit|Write|MultiEdit.
source "$(dirname "$0")/lib.sh"
ds_need_jq
ds_read_input

rel=$(ds_rel_path)
[[ -n "$rel" ]] || exit 0

branch=$(git -C "$DS_ROOT" branch --show-current 2>/dev/null || true)
[[ "$branch" =~ ^([0-9]{3})- ]] || exit 0
num=${BASH_REMATCH[1]}

in_source=false
while IFS= read -r dir; do
  if [[ -n "$dir" && "$rel" == "$dir"* ]]; then in_source=true; fi
done < <(ds_jq -r '(.paths.source // ["src/", "app/"])[]' "$DS_MANIFEST" 2>/dev/null || printf 'src/\napp/\n')
$in_source || exit 0
if ds_waived SDD-001 "$rel"; then exit 0; fi

spec=$(compgen -G "$DS_ROOT/specs/${num}-*/spec.md" | head -n 1 || true)
if [[ -z "$spec" ]] || ! grep -qiE '^\*\*Status\*\*: *approved' "$spec"; then
  ds_fail SDD \
    "SDD-001: spec $num is not approved, so source files can't be edited on branch $branch yet." \
    "Finish /speckit.clarify, then ask the user to set '**Status**: Approved' in specs/${num}-*/spec.md." \
    "Tests and docs can still be written. Rule: .dev-standard/standards/sdd.md"
fi
exit 0

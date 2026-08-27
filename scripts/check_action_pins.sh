#!/usr/bin/env bash
# Fail unless every GitHub Actions `uses:` reference in .github/workflows/ is
# pinned to a full 40-character hexadecimal commit SHA (no tags, no branches,
# no short SHAs). Local actions (`./...`) and reusable-workflow file refs
# (`owner/repo/.github/workflows/x.yml@...`) are checked the same way.
set -euo pipefail

workflow_dir="${1:-.github/workflows}"
fail=0

mapfile -t lines < <(grep -rnE '^\s*(- )?uses:\s*\S+' "$workflow_dir" 2>/dev/null || true)

if [ "${#lines[@]}" -eq 0 ]; then
  echo "==> no 'uses:' references found under $workflow_dir"
  exit 0
fi

echo "==> action-pin audit ($workflow_dir)"
for entry in "${lines[@]}"; do
  # entry looks like: path:lineno:        uses: owner/repo@<ref> # comment
  ref="$(printf '%s\n' "$entry" | sed -E 's/.*uses:[[:space:]]*//; s/[[:space:]]*#.*$//' | tr -d '"'"'"'')"
  loc="$(printf '%s\n' "$entry" | cut -d: -f1,2)"

  case "$ref" in
    ./*|.\\*)
      echo "  ok   (local)     $ref   [$loc]" ; continue ;;
  esac

  sha="${ref##*@}"
  name="${ref%@*}"
  if [ "$sha" = "$ref" ]; then
    echo "  FAIL (no @ref)   $ref   [$loc]" ; fail=1 ; continue
  fi
  if printf '%s' "$sha" | grep -qE '^[0-9a-f]{40}$'; then
    echo "  ok   (40-hex)    $name@$sha   [$loc]"
  else
    echo "  FAIL (not a 40-char commit SHA: '${sha}')   $name   [$loc]"
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "==> FAIL: one or more actions are not pinned to a 40-char commit SHA"
  exit 1
fi
echo "==> OK: every action is pinned to a 40-character commit SHA"

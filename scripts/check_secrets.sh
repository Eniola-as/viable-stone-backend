#!/usr/bin/env bash
# Lightweight secret-leak scan for CI and pre-commit use.
#
# Dependency-free (grep only). For deeper coverage run `gitleaks detect
# --no-git` locally or add the gitleaks action to CI.
#
# Testability: set SECRET_SCAN_PATHS to a newline-separated list of files to
# scan those instead of `git ls-files`; in that mode the repo-global .env /
# .env.example checks are skipped. See apps/core/tests/test_secret_scanner.py.
set -euo pipefail

fail=0
note() { printf '  - %s\n' "$1"; fail=1; }

# The ONLY inline-credential database URLs allowed to appear anywhere in
# tracked files: the documented CI placeholders in .github/workflows/ci.yml.
# Anything else with an inline password fails — local host or not.
APPROVED_DB_URLS=(
  "postgres://viable:viable@localhost:5432/viable_ci"
  "postgres://viable:viable@127.0.0.1:5432/viable_ci"
)

echo "==> secret-leak scan"

if [ -n "${SECRET_SCAN_PATHS:-}" ]; then
  scan_targets="$SECRET_SCAN_PATHS"
else
  # 1. .env must never be tracked; .env.example must be.
  if git ls-files --error-unmatch .env >/dev/null 2>&1; then
    note ".env is tracked by git — it must be git-ignored"
  fi
  if ! grep -qxE '\.env' .gitignore 2>/dev/null; then
    note ".env is not listed in .gitignore"
  fi
  if ! grep -qxE '!\.env\.example' .gitignore 2>/dev/null; then
    note ".gitignore does not re-include !.env.example"
  fi
  if ! git ls-files --error-unmatch .env.example >/dev/null 2>&1; then
    note ".env.example is not tracked"
  fi

  # 2. .env.example must contain placeholders only — no filled-in secret values.
  if grep -E '^(SECRET_KEY|OFFLINE_SIGNING_KEY|VAPID_PRIVATE_KEY|AWS_S3_SECRET_ACCESS_KEY|SENTRY_DSN)=..+' .env.example \
     | grep -viE '=(replace|change|your|placeholder|example|<)' >/dev/null 2>&1; then
    note ".env.example appears to contain a real secret value"
  fi

  scan_targets=$(git ls-files 'apps/**/*.py' 'config/**/*.py' '*.py' '*.yml' '*.yaml' \
    | grep -vE '(^|/)tests?/' || true)
fi

patterns=(
  'AKIA[0-9A-Z]{16}'                         # AWS access key id
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'       # PEM private key
  'sk_live_[0-9a-zA-Z]{16,}'                 # generic live secret key
  'xox[baprs]-[0-9A-Za-z-]{10,}'             # Slack token
)

if [ -n "$scan_targets" ]; then
  for p in "${patterns[@]}"; do
    # -e so a pattern that begins with '-' (PEM header) is not read as options.
    if echo "$scan_targets" | xargs grep -nEI -e "$p" 2>/dev/null; then
      note "matched secret pattern: $p"
    fi
  done

  # Any postgres/postgresql URL that carries an inline password. Every match
  # must be an EXACT approved CI placeholder; there is no host-based exemption.
  db_urls=$(echo "$scan_targets" | xargs grep -hoE \
    "postgres(ql)?://[^ \"'\$]+:[^ @\"'\$]+@[^ \"'\$/]+(:[0-9]+)?(/[^ \"'\$]*)?" 2>/dev/null \
    | sort -u || true)
  if [ -n "$db_urls" ]; then
    while IFS= read -r url; do
      [ -n "$url" ] || continue
      ok=0
      for approved in "${APPROVED_DB_URLS[@]}"; do
        [ "$url" = "$approved" ] && ok=1
      done
      if [ "$ok" -ne 1 ]; then
        note "inline-credential database URL is not the approved CI placeholder: ${url}"
      fi
    done <<< "$db_urls"
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "==> FAIL: potential secret exposure"
  exit 1
fi
echo "==> OK: no obvious secrets in tracked source"

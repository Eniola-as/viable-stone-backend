#!/usr/bin/env bash
# Lightweight secret-leak scan for CI and pre-commit use.
#
# It is intentionally dependency-free (grep only). For deeper coverage, run
# `gitleaks detect --no-git` locally or add the gitleaks action to CI.
set -euo pipefail

fail=0
note() { printf '  - %s\n' "$1"; fail=1; }

echo "==> secret-leak scan"

# 1. .env must never be tracked; .env.example must be.
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  note ".env is tracked by git — it must be git-ignored"
fi
if ! git ls-files --error-unmatch .env.example >/dev/null 2>&1; then
  note ".env.example is not tracked"
fi

# 2. .env.example must contain placeholders only — no filled-in secret values.
if grep -E '^(SECRET_KEY|OFFLINE_SIGNING_KEY|VAPID_PRIVATE_KEY|AWS_S3_SECRET_ACCESS_KEY|SENTRY_DSN)=..+' .env.example \
   | grep -viE '=(replace|change|your|placeholder|example|<)' >/dev/null 2>&1; then
  note ".env.example appears to contain a real secret value"
fi

# 3. Scan tracked source for high-signal secret shapes.
scan_targets=$(git ls-files 'apps/**/*.py' 'config/**/*.py' '*.py' '*.yml' '*.yaml' \
  | grep -vE '(^|/)tests?/' || true)
patterns=(
  'AKIA[0-9A-Z]{16}'                         # AWS access key id
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'       # PEM private key
  'sk_live_[0-9a-zA-Z]{16,}'                 # generic live secret key
  'xox[baprs]-[0-9A-Za-z-]{10,}'             # Slack token
)
if [ -n "$scan_targets" ]; then
  for p in "${patterns[@]}"; do
    if echo "$scan_targets" | xargs grep -nEI "$p" 2>/dev/null; then
      note "matched secret pattern: $p"
    fi
  done

  # A database URL with an inline password — but only flag REAL hosts. Loopback
  # / .internal / docker service hosts are CI dummies and are allowed.
  db_hits=$(echo "$scan_targets" | xargs grep -nEI \
    "postgres(ql)?://[^ \"'\$]+:[^ @\"'\$]+@" 2>/dev/null \
    | grep -vE '@(localhost|127\.0\.0\.1|\[::1\]|[a-z0-9_-]+\.internal|postgres|db)([:/]|$)' \
    || true)
  if [ -n "$db_hits" ]; then
    echo "$db_hits"
    note "database URL with an inline password to a non-local host"
  fi
fi

if [ "$fail" -ne 0 ]; then
  echo "==> FAIL: potential secret exposure"
  exit 1
fi
echo "==> OK: no obvious secrets in tracked source"

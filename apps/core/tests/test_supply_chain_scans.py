"""Stage 17 correction — regression tests for the CI supply-chain guards:
`scripts/check_secrets.sh` (inline-credential DB URLs) and
`scripts/check_action_pins.sh` (every GitHub Action pinned to a 40-hex SHA)."""

import subprocess
import textwrap
from pathlib import Path

import pytest
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
SECRET_SCAN = ROOT / "scripts" / "check_secrets.sh"
PIN_AUDIT = ROOT / "scripts" / "check_action_pins.sh"

# The exact documented CI placeholder — the ONLY inline-credential DB URL the
# secret scanner is allowed to accept.
APPROVED_CI_DB_URL = "postgres://viable:viable@localhost:5432/viable_ci"


def _run(cmd, **env):
    proc = subprocess.run(
        ["bash", *cmd],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, **env},
    )
    return proc.returncode, proc.stdout + proc.stderr


def _scan_fixture(tmp_path: Path, content: str) -> tuple[int, str]:
    fixture = tmp_path / "fixture.yml"
    fixture.write_text(textwrap.dedent(content), encoding="utf-8")
    # POSIX path so grep/xargs under Git Bash on Windows handle it.
    return _run([str(SECRET_SCAN)], SECRET_SCAN_PATHS=fixture.as_posix())


class TestSecretScannerDbUrls:
    def test_rejects_real_looking_inline_credential_on_a_remote_host(self, tmp_path):
        rc, out = _scan_fixture(
            tmp_path,
            "DATABASE_URL: postgres://app:S3cr3tP4ss@db.prod.example.com:5432/app\n",
        )
        assert rc != 0
        assert "not the approved CI placeholder" in out

    def test_rejects_real_looking_inline_credential_on_localhost(self, tmp_path):
        rc, out = _scan_fixture(
            tmp_path,
            "DATABASE_URL: postgres://app:hunter2pass@localhost:5432/devdb\n",
        )
        assert rc != 0
        assert "not the approved CI placeholder" in out

    def test_rejects_a_near_miss_of_the_approved_placeholder(self, tmp_path):
        rc, _out = _scan_fixture(
            tmp_path,
            "DATABASE_URL: postgres://viable:viable2@localhost:5432/viable_ci\n",
        )
        assert rc != 0

    def test_accepts_only_the_exact_approved_ci_placeholder(self, tmp_path):
        rc, out = _scan_fixture(tmp_path, f"DATABASE_URL: {APPROVED_CI_DB_URL}\n")
        assert rc == 0, out
        # the loopback-IP form is also approved
        rc2, _ = _scan_fixture(
            tmp_path,
            "run: -e DATABASE_URL=postgres://viable:viable@127.0.0.1:5432/viable_ci\n",
        )
        assert rc2 == 0

    def test_still_catches_a_pem_private_key(self, tmp_path):
        rc, out = _scan_fixture(tmp_path, "key: -----BEGIN OPENSSH PRIVATE KEY-----\n")
        assert rc != 0
        assert "PRIVATE KEY" in out


class TestSecretScannerRepoState:
    def test_real_repo_passes_all_checks(self):
        rc, out = _run([str(SECRET_SCAN)])
        assert rc == 0, out

    def test_dotenv_is_untracked_and_ignored(self):
        tracked = subprocess.run(
            ["git", "ls-files", ".env"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()
        assert tracked == "", ".env must not be tracked"
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert ".env" in gitignore
        assert "!.env.example" in gitignore


class TestActionPinAudit:
    def test_real_workflows_are_all_pinned_to_40_hex_shas(self):
        rc, out = _run([str(PIN_AUDIT)])
        assert rc == 0, out
        assert "every action is pinned to a 40-character commit SHA" in out

    def test_audit_fails_on_a_tag_or_short_sha(self, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "bad.yml").write_text(
            "jobs:\n  x:\n    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - uses: some/action@abcdef0123456789\n",
            encoding="utf-8",
        )
        rc, out = _run([str(PIN_AUDIT), str(wf)])
        assert rc != 0
        assert "not a 40-char commit SHA" in out


_PINNED_ACTIONS = [
    ("actions/checkout", "3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    ("actions/setup-python", "5fda3b95a4ea91299a34e894583c3862153e4b97", "v7.0.0"),
    (
        "aquasecurity/trivy-action",
        "ed142fd0673e97e23eac54620cfb913e5ce36c25",
        "v0.36.0",
    ),
]


@pytest.mark.parametrize(("name", "sha", "tag"), _PINNED_ACTIONS)
def test_ci_workflow_uses_lines_carry_a_full_40_hex_sha(name, sha, tag):
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    expected = f"uses: {name}@{sha} # {tag}"
    assert expected in ci, f"expected pinned reference missing from ci.yml:\n{expected}"
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)

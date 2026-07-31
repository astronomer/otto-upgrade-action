"""verify.sh's detect_parse_mode wires BUILD_SECRETS into the parse command.

The function is extracted from verify.sh verbatim and driven against fake
`astro` binaries emitting the three help shapes that exist in the field:
modern (--build-secret, CLI >= 1.44), legacy (--build-secrets, a StringSlice
that comma-splits and rejoins — exact for one secret, wrong for several), and
ancient (no secret flag at all).
"""

import re
import stat
import subprocess
from pathlib import Path

import pytest

VERIFY_SH = Path(__file__).resolve().parent.parent / "scripts" / "verify.sh"

MODERN_HELP = """\
  --docker             Run in Docker mode
  --build-secret stringArray   Secret to expose to the build. Repeat to specify multiple secrets. (format: "id=mysecret[,src=/local/secret]")
"""

# Real v1.42 help: the description mentions "docker build --secret" — the
# modern probe's trailing space must not false-match on it.
LEGACY_HELP = """\
  --build-secrets strings   Mimics docker build --secret flag. See https://docs.docker.com/build/building/secrets/ for more information. Example input id=mysecret,src=secrets.txt
  --docker             Run in Docker mode
"""

ANCIENT_HELP = """\
  --docker             Run in Docker mode
"""

SENTINEL = "===PARSE_CMD==="


def extract_detect_parse_mode() -> str:
    text = VERIFY_SH.read_text()
    match = re.search(r"^detect_parse_mode\(\) \{\n.*?^\}$", text, re.M | re.S)
    if not match:
        raise AssertionError("detect_parse_mode not found in verify.sh")
    return match.group(0)


def run_detect(tmp_path, help_text, build_secrets, project=None):
    """Run detect_parse_mode; return (parse_cmd argv, warning/log lines)."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    astro = fake_bin / "astro"
    astro.write_text(f"#!/bin/bash\ncat <<'EOF'\n{help_text}EOF\n")
    astro.chmod(astro.stat().st_mode | stat.S_IXUSR)

    if project is None:
        project = tmp_path / "proj"
        project.mkdir(exist_ok=True)

    script = (
        "set -euo pipefail\n"
        "parse_cmd=(astro dev parse)\n"
        f"{extract_detect_parse_mode()}\n"
        "detect_parse_mode\n"
        f"echo '{SENTINEL}'\n"
        'printf \'%s\\n\' "${parse_cmd[@]}"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "BUILD_SECRETS": build_secrets,
            "PROJECT_PATH": str(project),
        },
        check=True,
    )
    before, _, after = proc.stdout.partition(f"{SENTINEL}\n")
    return after.splitlines(), before.splitlines()


def test_modern_cli_single_env_secret(tmp_path):
    cmd, warnings = run_detect(tmp_path, MODERN_HELP, "id=netrc,env=NETRC_CONTENT")
    assert cmd == [
        "astro", "dev", "parse", "--docker",
        "--build-secret", "id=netrc,env=NETRC_CONTENT",
    ]
    assert warnings == []


def test_modern_cli_multiple_secrets_and_blank_lines(tmp_path):
    cmd, _ = run_detect(
        tmp_path, MODERN_HELP, "id=netrc,env=NETRC_CONTENT\n\n   \nid=pypi,env=PYPI_CREDS\n"
    )
    assert cmd == [
        "astro", "dev", "parse", "--docker",
        "--build-secret", "id=netrc,env=NETRC_CONTENT",
        "--build-secret", "id=pypi,env=PYPI_CREDS",
    ]


def test_legacy_cli_uses_plural_flag(tmp_path):
    cmd, warnings = run_detect(tmp_path, LEGACY_HELP, "id=netrc,env=NETRC_CONTENT")
    assert cmd == [
        "astro", "dev", "parse", "--docker",
        "--build-secrets", "id=netrc,env=NETRC_CONTENT",
    ]
    assert warnings == []


def test_legacy_cli_multiple_secrets_warns(tmp_path):
    cmd, warnings = run_detect(tmp_path, LEGACY_HELP, "id=a,env=A\nid=b,env=B")
    assert cmd == [
        "astro", "dev", "parse", "--docker",
        "--build-secrets", "id=a,env=A",
        "--build-secrets", "id=b,env=B",
    ]
    assert any("folds multiple build secrets" in w for w in warnings)


def test_ancient_cli_warns_and_forwards_nothing(tmp_path):
    cmd, warnings = run_detect(tmp_path, ANCIENT_HELP, "id=netrc,env=NETRC_CONTENT")
    assert cmd == ["astro", "dev", "parse", "--docker"]
    assert any("no build-secret flag" in w for w in warnings)


def test_empty_build_secrets_is_noop(tmp_path):
    cmd, warnings = run_detect(tmp_path, MODERN_HELP, "")
    assert cmd == ["astro", "dev", "parse", "--docker"]
    assert warnings == []


def test_spec_lines_are_trimmed(tmp_path):
    cmd, _ = run_detect(tmp_path, MODERN_HELP, "   id=netrc,env=NETRC_CONTENT   ")
    assert cmd[-1] == "id=netrc,env=NETRC_CONTENT"


@pytest.mark.parametrize("key", ["src", "source"])
def test_relative_src_resolved_against_project_root(tmp_path, key):
    # The target build (rsync copy, has gitignored files) and baseline build
    # (clean worktree, committed files only) run from different directories; a
    # relative path must be pinned so both read the identical file.
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".netrc").write_text("machine github.com\n")

    cmd, warnings = run_detect(
        tmp_path, MODERN_HELP, f"id=netrc,{key}=.netrc", project=project
    )
    assert cmd[-1] == f"id=netrc,{key}={project}/.netrc"
    assert warnings == []


def test_absolute_src_left_alone_and_missing_file_warns(tmp_path):
    cmd, warnings = run_detect(tmp_path, MODERN_HELP, "id=netrc,src=/nonexistent/netrc")
    assert cmd[-1] == "id=netrc,src=/nonexistent/netrc"
    assert any("does not exist" in w for w in warnings)


def test_env_specs_are_never_rewritten(tmp_path):
    cmd, _ = run_detect(tmp_path, MODERN_HELP, "id=netrc,env=NETRC_CONTENT,type=env")
    assert cmd[-1] == "id=netrc,env=NETRC_CONTENT,type=env"

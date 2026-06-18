"""build-prompt.sh must explicitly name the airflow-upgrade skill + versions.

This is the crux of skill engagement: a free-text "upgrade" prompt routes Otto to
generic doc-search and skips the curated KB skill. The prompt has to name the
skill and pass currentVersion/targetVersion (paired with --allowed-skills in
run-otto.sh). Lock that in so it can't silently regress.
"""

import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build-prompt.sh"


def _run(tmp_path, plan: dict, project="/proj") -> str:
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan))
    subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "WORKDIR": str(tmp_path),
            "PLAN_FILE": str(plan_file),
            "PROJECT_PATH": project,
        },
        check=True, capture_output=True, text=True,
    )
    return (tmp_path / "user-prompt.txt").read_text()


def test_runtime_upgrade_prompt_names_skill_and_versions(tmp_path):
    plan = {"runtime": {"current_airflow": "2.9.5", "target_airflow": "3.2.2",
                        "current_tag": "9.10.0", "target_tag": "3.2-5", "tier": "major"},
            "providers": []}
    prompt = _run(tmp_path, plan)
    assert "airflow-upgrade skill" in prompt
    assert "currentVersion=2.9.5" in prompt
    assert "targetVersion=3.2.2" in prompt


def test_provider_only_prompt_still_names_skill(tmp_path):
    # Runtime unchanged (provider-only bump) — still routes through the skill.
    plan = {"runtime": {"current_airflow": "3.2.2", "target_airflow": "3.2.2",
                        "current_tag": "3.2-5", "target_tag": "3.2-5", "tier": "none"},
            "providers": [{"package": "apache-airflow-providers-amazon",
                          "current": "9.0.0", "target": "9.30.0", "tier": "minor"}]}
    prompt = _run(tmp_path, plan)
    assert "airflow-upgrade skill" in prompt
    assert "amazon 9.0.0 -> 9.30.0" in (tmp_path / "upgrade-context.md").read_text()

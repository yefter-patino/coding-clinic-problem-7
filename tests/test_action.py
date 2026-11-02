"""Checks that action.yml and the script agree, so an input can never be silently ignored."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ACTION = yaml.safe_load((ROOT / "action.yml").read_text())
SCRIPT = (ROOT / "src" / "semver_release.py").read_text()
RELEASE_STEP = next(step for step in ACTION["runs"]["steps"] if step.get("id") == "release")


def test_every_setting_the_script_reads_is_passed_by_the_action():
    read_by_script = set(re.findall(r'env\("([A-Z_]+)"', SCRIPT)) | {"TOKEN"}
    passed_by_action = {name.removeprefix("SR_") for name in RELEASE_STEP["env"] if name.startswith("SR_")}
    # DATE is only for tests; REMOTE is always "origin" inside GitHub Actions
    assert read_by_script - {"DATE", "REMOTE"} <= passed_by_action


def test_every_input_is_used():
    used = (ROOT / "action.yml").read_text()
    for name in ACTION["inputs"]:
        assert f"inputs.{name}" in used, f"input {name} is never used"


def test_every_output_comes_from_a_step():
    for name, output in ACTION["outputs"].items():
        assert output["value"].startswith("${{ steps."), name


def test_inputs_reach_the_script_only_through_env():
    """No ${{ }} inside run: blocks (script injection)."""
    for step in ACTION["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")


def test_composite_steps_all_declare_a_shell():
    assert ACTION["runs"]["using"] == "composite"
    for step in ACTION["runs"]["steps"]:
        if "run" in step:
            assert step["shell"] == "bash"

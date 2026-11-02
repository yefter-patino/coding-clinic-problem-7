"""Integration tests: the real script against real git repositories, with a real (local) remote.

Each test gets a fresh repository and a bare "origin" to push to. Nothing touches GitHub.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "src" / "semver_release.py"


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.remote = path.parent / "origin.git"
        sh("git", "init", "--bare", "-q", "-b", "main", str(self.remote), cwd=path.parent)
        sh("git", "init", "-q", "-b", "main", cwd=path)
        sh("git", "config", "user.name", "Test", cwd=path)
        sh("git", "config", "user.email", "test@example.com", cwd=path)
        sh("git", "config", "tag.gpgSign", "false", cwd=path)
        sh("git", "remote", "add", "origin", str(self.remote), cwd=path)

    def git(self, *args):
        return sh("git", *args, cwd=self.path)

    def commit(self, message):
        n = len(list(self.path.glob("file-*"))) + 1
        (self.path / f"file-{n}.txt").write_text(message)
        self.git("add", ".")
        self.git("commit", "-q", "-m", message)

    def tag(self, name):
        self.git("tag", "-a", name, "-m", name)

    def release(self, *args, extra_env=None):
        """Run the script like the action does. Returns (exit code, output text, outputs dict)."""
        output_file = self.path.parent / "github_output.txt"
        output_file.write_text("")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SR_", "GITHUB_"))}
        env.update({"GITHUB_OUTPUT": str(output_file), "SR_DATE": "2026-10-23", **(extra_env or {})})
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args], cwd=self.path, capture_output=True, text=True, env=env
        )
        return result.returncode, result.stdout + result.stderr, parse_outputs(output_file.read_text())

    def remote_tags(self):
        return sh("git", "tag", "--list", cwd=self.remote).split()


def parse_outputs(text):
    """Read a GITHUB_OUTPUT file, including name<<DELIMITER multi-line values."""
    outputs, lines, i = {}, text.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line:
            name, delimiter = line.split("<<", 1)
            value = []
            i += 1
            while lines[i] != delimiter:
                value.append(lines[i])
                i += 1
            outputs[name] = "\n".join(value)
        elif "=" in line:
            name, value = line.split("=", 1)
            outputs[name] = value
        i += 1
    return outputs


@pytest.fixture
def repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    return Repo(work)


# ---------------------------------------------------------------- the main paths
def test_first_release_tags_pushes_and_writes_the_changelog(repo):
    repo.commit("chore: initial setup")
    repo.commit("feat: add login page")
    repo.commit("fix(api): handle empty body")
    code, log, out = repo.release()
    assert code == 0, log
    assert out["released"] == "true" and out["tag"] == "v0.1.0" and out["bump"] == "initial"
    assert repo.remote_tags() == ["v0.1.0"]
    assert repo.git("cat-file", "-t", "v0.1.0") == "tag"  # annotated, not lightweight
    changelog = (repo.path / "CHANGELOG.md").read_text()
    assert "## 0.1.0 (2026-10-23)" in changelog
    assert "### Features\n\n* add login page" in changelog
    assert "* **api:** handle empty body" in changelog
    assert "initial setup" not in changelog


@pytest.mark.parametrize(
    "start, message, expected",
    [
        ("v1.2.3", "fix: small bug", "v1.2.4"),
        ("v1.2.3", "perf: faster", "v1.2.4"),
        ("v1.2.3", "feat: new thing", "v1.3.0"),
        ("v1.2.3", "feat!: breaking thing", "v2.0.0"),
        ("v0.4.0", "feat!: breaking in 0.x", "v0.5.0"),
    ],
)
def test_bumps_from_an_existing_tag(repo, start, message, expected):
    repo.commit("feat: base")
    repo.tag(start)
    repo.commit(message)
    code, log, out = repo.release()
    assert code == 0, log
    assert out["tag"] == expected
    assert out["previous-version"] == start[1:]


def test_breaking_change_footer_in_the_body_is_a_major_bump(repo):
    repo.commit("feat: base")
    repo.tag("v1.0.0")
    repo.commit("fix: change the config format\n\nBREAKING CHANGE: the old config file is not read any more")
    code, _, out = repo.release()
    assert out["tag"] == "v2.0.0"
    assert "the old config file is not read any more" in out["notes"]


def test_nothing_to_release_creates_no_tag(repo):
    repo.commit("feat: base")
    repo.tag("v1.0.0")
    repo.commit("docs: improve README")
    repo.commit("chore: bump tooling")
    code, log, out = repo.release()
    assert code == 0, log
    assert out["released"] == "false" and out["version"] == "1.0.0"
    assert repo.git("tag", "--list") == "v1.0.0"
    assert not (repo.path / "CHANGELOG.md").exists()


def test_running_twice_does_nothing_the_second_time(repo):
    repo.commit("feat: a")
    repo.release()
    code, log, out = repo.release()
    assert code == 0
    assert "already released as v0.1.0" in log
    assert out["released"] == "false"
    assert repo.remote_tags() == ["v0.1.0"]


def test_dry_run_changes_nothing(repo):
    repo.commit("feat: a")
    code, log, out = repo.release("--dry-run", "true")
    assert code == 0, log
    assert out["version"] == "0.1.0" and out["released"] == "false" and out["dry-run"] == "true"
    assert repo.git("tag", "--list") == ""
    assert repo.remote_tags() == []
    assert not (repo.path / "CHANGELOG.md").exists()


def test_the_tag_message_keeps_the_markdown_headings(repo):
    repo.commit("feat: a")
    repo.release()
    message = repo.git("tag", "-l", "--format=%(contents)", "v0.1.0")
    assert "## 0.1.0 (2026-10-23)" in message and "### Features" in message


def test_multiline_release_notes_are_a_valid_github_output(repo):
    repo.commit("feat: a")
    repo.commit("fix: b")
    _, _, out = repo.release()
    assert out["notes"].startswith("## 0.1.0") and "### Bug Fixes" in out["notes"]


# ---------------------------------------------------------------- optional behaviour
def test_commit_changelog_commits_and_pushes_branch_and_tag_together(repo):
    repo.commit("feat: a")
    repo.git("push", "-q", "origin", "main")
    code, log, out = repo.release("--commit-changelog", "true", "--branch", "main")
    assert code == 0, log
    assert repo.git("log", "-1", "--format=%s") == "chore(release): v0.1.0 [skip ci]"
    assert repo.git("rev-list", "-n1", "v0.1.0") == repo.git("rev-parse", "HEAD")  # tag on the release commit
    assert sh("git", "log", "-1", "--format=%s", "main", cwd=repo.remote) == "chore(release): v0.1.0 [skip ci]"
    assert "CHANGELOG.md" in repo.git("show", "--name-only", "--format=", "HEAD")


def test_the_release_commit_does_not_trigger_a_new_release(repo):
    repo.commit("feat: a")
    repo.git("push", "-q", "origin", "main")
    repo.release("--commit-changelog", "true", "--branch", "main")
    code, log, out = repo.release("--commit-changelog", "true", "--branch", "main")
    assert out["released"] == "false"  # HEAD is the tagged release commit


def test_custom_prefix_and_initial_version(repo):
    repo.commit("feat: a")
    _, _, out = repo.release("--tag-prefix", "release-", "--initial-version", "1.0.0")
    assert out["tag"] == "release-1.0.0"


def test_no_push_creates_the_tag_only_locally(repo):
    repo.commit("feat: a")
    repo.release("--push", "false")
    assert repo.git("tag", "--list") == "v0.1.0"
    assert repo.remote_tags() == []


def test_settings_come_from_environment_variables(repo):
    repo.commit("docs: only docs")
    _, _, out = repo.release(extra_env={"SR_PATCH_TYPES": "docs"})
    assert out["released"] == "true"


# ---------------------------------------------------------------- safety
def test_strict_mode_fails_on_non_conventional_commits(repo):
    repo.commit("feat: a")
    repo.commit("Updated stuff")
    code, log, _ = repo.release("--strict", "true")
    assert code == 1
    assert "not Conventional Commits" in log
    assert repo.git("tag", "--list") == ""


def test_non_conventional_commits_are_listed_but_ignored_by_default(repo):
    repo.commit("feat: a")
    repo.commit("Updated stuff")
    code, log, out = repo.release()
    assert code == 0 and out["tag"] == "v0.1.0"
    assert "not a Conventional Commit, ignored" in log and "Updated stuff" in log


def test_a_shallow_clone_is_refused_with_a_clear_message(repo, tmp_path):
    for n in range(3):
        repo.commit(f"feat: {n}")
    shallow = tmp_path / "shallow"
    sh("git", "clone", "-q", "--depth", "1", f"file://{repo.path}", str(shallow), cwd=tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=shallow, capture_output=True, text=True)
    assert result.returncode == 1
    assert "fetch-depth: 0" in result.stderr


def test_an_existing_tag_elsewhere_is_never_moved(repo):
    repo.commit("feat: a")
    repo.tag("v0.1.0")
    repo.git("checkout", "-q", "-b", "side")  # v0.2.0 is released from a side branch...
    repo.commit("feat: side work")
    repo.tag("v0.2.0")
    repo.git("checkout", "-q", "main")  # ...that main does not contain
    repo.commit("feat: main work")
    code, log, _ = repo.release()
    assert code == 1
    assert "v0.2.0 already exists" in log


def test_tags_on_other_branches_are_not_the_base(repo):
    repo.commit("feat: a")
    repo.tag("v1.0.0")
    repo.git("checkout", "-q", "-b", "experiment")
    repo.commit("feat: experiment")
    repo.tag("v5.0.0")
    repo.git("checkout", "-q", "main")
    repo.commit("fix: on main")
    _, _, out = repo.release()
    assert out["tag"] == "v1.0.1"


def test_merge_commits_are_skipped(repo):
    repo.commit("feat: base")
    repo.tag("v1.0.0")
    repo.git("checkout", "-q", "-b", "topic")
    repo.commit("fix: real change")
    repo.git("checkout", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "topic", "-m", "Merge pull request #1 from topic")
    code, log, out = repo.release()
    assert out["tag"] == "v1.0.1"
    assert "Merge pull request" not in log


def test_a_bad_prefix_is_refused(repo):
    repo.commit("feat: a")
    code, log, _ = repo.release("--tag-prefix", "v;rm")
    assert code == 1


# ---------------------------------------------------------------- push credentials
def test_token_is_passed_through_env_only_when_checkout_did_not_store_one(repo, monkeypatch):
    monkeypatch.chdir(repo.path)
    sys.path.insert(0, str(SCRIPT.parent))
    from semver_release import push_auth_env

    assert push_auth_env("", "https://github.com") == {}
    env = push_auth_env("secret-token", "https://github.com")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")
    assert "secret-token" not in env["GIT_CONFIG_VALUE_0"]  # base64, and never on a command line
    repo.git("config", "http.https://github.com/.extraheader", "AUTHORIZATION: basic xyz")
    assert push_auth_env("secret-token", "https://github.com") == {}  # avoid a duplicate header


def test_if_the_branch_push_is_rejected_the_tag_is_not_pushed_either(repo):
    """Like a protected main branch: the remote refuses the changelog commit. --atomic means the
    tag must not be published on its own (no release tag pointing at a commit main never got)."""
    repo.commit("feat: a")
    repo.git("push", "-q", "origin", "main")
    # an "update" hook judges each ref separately, like branch protection does
    hook = repo.remote / "hooks" / "update"
    hook.write_text('#!/bin/sh\n[ "$1" = refs/heads/main ] && { echo "protected branch" >&2; exit 1; }\nexit 0\n')
    hook.chmod(0o755)
    code, log, _ = repo.release("--commit-changelog", "true", "--branch", "main")
    assert code == 1
    assert "protected branch" in log
    assert repo.remote_tags() == []

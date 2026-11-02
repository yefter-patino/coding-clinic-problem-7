#!/usr/bin/env python3
"""Semantic versioning + changelog + Git tag from Conventional Commits.

Reads the commits since the last version tag, decides the next version, writes the changelog,
creates an annotated tag and pushes it. Standard library only: runs anywhere Python 3.10+ and
git are installed (every GitHub-hosted runner has both).

  feat: ...                         -> minor   1.4.2 -> 1.5.0
  fix: ... / perf: ... / revert:    -> patch   1.4.2 -> 1.4.3
  feat!: ... or "BREAKING CHANGE:"  -> major   1.4.2 -> 2.0.0
  docs/chore/ci/test/style/...      -> no release

Every setting is a command-line option with an environment-variable default (SR_<OPTION>),
which is how the GitHub composite action passes them in. Run with --help for the list.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import os
import re
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- data


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @classmethod
    def parse(cls, text: str) -> Version:
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", text)
        if not match:
            raise ValueError(f"not a version like 1.2.3: {text!r}")
        return cls(*(int(part) for part in match.groups()))

    def bump(self, kind: str) -> Version:
        if kind == "major":
            return Version(self.major + 1, 0, 0)
        if kind == "minor":
            return Version(self.major, self.minor + 1, 0)
        if kind == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        raise ValueError(f"unknown bump {kind!r}")


@dataclass
class Commit:
    sha: str
    subject: str
    body: str = ""


@dataclass
class ParsedCommit:
    commit: Commit
    type: str
    scope: str
    description: str
    breaking: bool
    breaking_notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- conventional commits

# type(scope)!: description      (scope and ! are optional)
_HEADER = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^()\r\n]+)\))?(?P<bang>!)?: (?P<description>\S.*)$")
# "BREAKING CHANGE: text" or "BREAKING-CHANGE: text" footer, text may continue on the next lines
_BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE: (?P<text>.+(?:\n(?!\S+(?:: | #)).+)*)", re.MULTILINE)


def parse_commit(commit: Commit) -> ParsedCommit | None:
    """Return the parsed commit, or None if the subject is not a Conventional Commit."""
    match = _HEADER.match(commit.subject.strip())
    if not match:
        return None
    notes = [" ".join(m.group("text").split()) for m in _BREAKING_FOOTER.finditer(commit.body)]
    breaking = bool(match.group("bang")) or bool(notes)
    if match.group("bang") and not notes:
        notes = [match.group("description").strip()]
    return ParsedCommit(
        commit=commit,
        type=match.group("type").lower(),
        scope=(match.group("scope") or "").strip(),
        description=match.group("description").strip(),
        breaking=breaking,
        breaking_notes=notes,
    )


def decide_bump(parsed: list[ParsedCommit], minor_types: set[str], patch_types: set[str]) -> str | None:
    """The biggest bump any commit asks for: major > minor > patch > None."""
    if any(p.breaking for p in parsed):
        return "major"
    if any(p.type in minor_types for p in parsed):
        return "minor"
    if any(p.type in patch_types for p in parsed):
        return "patch"
    return None


def next_version(current: Version | None, bump: str, initial: Version, major_on_zero: bool) -> Version:
    """The version to release. The first release is always `initial`."""
    if current is None:
        return initial
    # SemVer: 0.x means "still in development", so by default a breaking change only bumps the minor.
    if bump == "major" and current.major == 0 and not major_on_zero:
        bump = "minor"
    return current.bump(bump)


# ---------------------------------------------------------------- changelog

SECTIONS = [
    ("feat", "Features"),
    ("fix", "Bug Fixes"),
    ("perf", "Performance Improvements"),
    ("revert", "Reverts"),
]


def render_changelog(
    version: Version,
    tag: str,
    previous_tag: str | None,
    parsed: list[ParsedCommit],
    date: dt.date,
    repo_url: str = "",
) -> str:
    """One Markdown section for this release, newest commits first."""

    def commit_link(sha: str) -> str:
        short = sha[:7]
        return f"([{short}]({repo_url}/commit/{sha}))" if repo_url else f"({short})"

    def entry(text: str, p: ParsedCommit) -> str:
        scope = f"**{p.scope}:** " if p.scope else ""
        return f"* {scope}{text} {commit_link(p.commit.sha)}"

    if repo_url and previous_tag:
        title = f"## [{version}]({repo_url}/compare/{previous_tag}...{tag}) ({date.isoformat()})"
    else:
        title = f"## {version} ({date.isoformat()})"

    lines = [title, ""]
    breaking = [(note, p) for p in parsed if p.breaking for note in p.breaking_notes]
    if breaking:
        lines += ["### ⚠ BREAKING CHANGES", ""]
        lines += [entry(note, p) for note, p in breaking]
        lines.append("")
    for commit_type, heading in SECTIONS:
        items = [p for p in parsed if p.type == commit_type]
        if items:
            lines += [f"### {heading}", ""]
            lines += [entry(p.description, p) for p in items]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


CHANGELOG_TITLE = (
    "# Changelog\n\nAll notable changes to this project are documented here (generated from Conventional Commits).\n"
)


def update_changelog_file(path: Path, section: str, version: Version) -> bool:
    """Put the new section at the top of the file. Returns False if this version is already there."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if re.search(rf"^## \[?{re.escape(str(version))}\]?[ (]", existing, re.MULTILINE):
        return False
    if existing.startswith("# "):
        # keep the title (and the paragraph under it), insert before the first "## " heading
        first_release = existing.find("\n## ")
        head, rest = (
            (existing, "") if first_release == -1 else (existing[: first_release + 1], existing[first_release + 1 :])
        )
        new = head.rstrip() + "\n\n" + section + ("\n" + rest if rest else "")
    else:
        new = CHANGELOG_TITLE + "\n" + section + ("\n" + existing if existing else "")
    path.write_text(new, encoding="utf-8")
    return True


# ---------------------------------------------------------------- git


class GitError(RuntimeError):
    pass


def git(*args: str, env: dict[str, str] | None = None, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603 (fixed program, arguments are not interpreted by a shell)
        ["git", *args],  # noqa: S607
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def tag_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}(\d+\.\d+\.\d+)$")


def latest_tag(prefix: str) -> tuple[str | None, Version | None]:
    """Highest release tag (prefix + X.Y.Z) that is part of HEAD's history. Pre-releases are ignored."""
    pattern = tag_pattern(prefix)
    best: tuple[str | None, Version | None] = (None, None)
    for tag in git("tag", "--merged", "HEAD", "--list", f"{prefix}*").split():
        match = pattern.match(tag)
        if match:
            version = Version.parse(match.group(1))
            if best[1] is None or version > best[1]:
                best = (tag, version)
    return best


def release_tag_on_head(prefix: str) -> str | None:
    pattern = tag_pattern(prefix)
    for tag in git("tag", "--points-at", "HEAD").split():
        if pattern.match(tag):
            return tag
    return None


def commits_since(tag: str | None) -> list[Commit]:
    """Commits after `tag` up to HEAD (all of them if there is no tag), newest first, merges skipped."""
    revision = f"{tag}..HEAD" if tag else "HEAD"
    raw = git("log", "--no-merges", "--format=%H%x1f%s%x1f%b%x1e", revision)
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip("\n")
        if record:
            sha, subject, body = (record.split("\x1f") + ["", ""])[:3]
            commits.append(Commit(sha=sha.strip(), subject=subject, body=body.strip()))
    return commits


def push_auth_env(token: str, server_url: str) -> dict[str, str]:
    """Credentials for one git command only, through environment variables (never on the command line).

    Skipped when actions/checkout already stored credentials, because two Authorization headers
    make GitHub reject the request.
    """
    if not token:
        return {}
    key = f"http.{server_url.rstrip('/')}/.extraheader"
    if git("config", "--get-all", key, check=False).strip():
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": key, "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}


# ---------------------------------------------------------------- GitHub outputs


def write_outputs(values: dict[str, str]) -> None:
    """Write step outputs for GitHub Actions (also printed, for local runs)."""
    for name, value in values.items():
        print(f"output {name}={value if chr(10) not in value else '<' + str(len(value)) + ' chars>'}")
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        for name, value in values.items():
            if "\n" in value:
                delimiter = f"EOF_{secrets.token_hex(8)}"
                handle.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                handle.write(f"{name}={value}\n")


def add_summary(text: str) -> None:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


# ---------------------------------------------------------------- main flow


def env(name: str, default: str = "") -> str:
    return os.environ.get(f"SR_{name}", default)


def as_bool(text: str) -> bool:
    if text.lower() in ("true", "1", "yes"):
        return True
    if text.lower() in ("false", "0", "no", ""):
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {text!r}")


def type_set(text: str) -> set[str]:
    return {part.strip().lower() for part in text.split(",") if part.strip()}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--tag-prefix", default=env("TAG_PREFIX", "v"), help="tag prefix (default v -> v1.2.3)")
    p.add_argument("--initial-version", default=env("INITIAL_VERSION", "0.1.0"), help="first release (default 0.1.0)")
    p.add_argument("--minor-types", type=type_set, default=type_set(env("MINOR_TYPES", "feat")))
    p.add_argument("--patch-types", type=type_set, default=type_set(env("PATCH_TYPES", "fix,perf,revert")))
    p.add_argument(
        "--major-on-zero",
        type=as_bool,
        default=as_bool(env("MAJOR_ON_ZERO", "false")),
        help="let a breaking change in 0.x go to 1.0.0 (default false: 0.x bumps minor)",
    )
    p.add_argument("--changelog-file", default=env("CHANGELOG_FILE", "CHANGELOG.md"))
    p.add_argument("--notes-file", default=env("NOTES_FILE", ""), help="where to write this release's notes")
    p.add_argument(
        "--commit-changelog",
        type=as_bool,
        default=as_bool(env("COMMIT_CHANGELOG", "false")),
        help="commit CHANGELOG.md and push it to the branch (needs push rights to the branch)",
    )
    p.add_argument("--branch", default=env("BRANCH", os.environ.get("GITHUB_REF_NAME", "")))
    p.add_argument("--remote", default=env("REMOTE", "origin"))
    p.add_argument("--push", type=as_bool, default=as_bool(env("PUSH", "true")), help="push the tag (default true)")
    p.add_argument(
        "--dry-run",
        type=as_bool,
        default=as_bool(env("DRY_RUN", "false")),
        help="only calculate and print, change nothing",
    )
    p.add_argument(
        "--strict",
        type=as_bool,
        default=as_bool(env("STRICT", "false")),
        help="fail if any commit is not a Conventional Commit",
    )
    p.add_argument("--git-user-name", default=env("GIT_USER_NAME", "github-actions[bot]"))
    p.add_argument(
        "--git-user-email", default=env("GIT_USER_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com")
    )
    p.add_argument("--date", default=env("DATE", ""), help="release date YYYY-MM-DD (default: today, UTC)")
    return p


def run(args: argparse.Namespace) -> int:
    prefix = args.tag_prefix
    if not re.fullmatch(r"[A-Za-z0-9._/-]*", prefix):
        raise ValueError(f"tag prefix may only contain letters, digits and . _ / -, got {prefix!r}")
    initial = Version.parse(args.initial_version)
    date = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(dt.timezone.utc).date()
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    repo_url = f"{server}/{repository}" if repository else ""

    if git("rev-parse", "--is-shallow-repository").strip() == "true":
        raise GitError(
            "this is a shallow clone, so old tags and commits are missing. Use actions/checkout with 'fetch-depth: 0'."
        )

    # Idempotency: this exact commit was already released.
    existing = release_tag_on_head(prefix)
    if existing:
        print(f"HEAD is already released as {existing}. Nothing to do.")
        write_outputs(
            {
                "released": "false",
                "version": existing[len(prefix) :],
                "tag": existing,
                "previous-version": existing[len(prefix) :],
                "bump": "none",
            }
        )
        return 0

    previous_tag, current = latest_tag(prefix)
    commits = commits_since(previous_tag)
    print(f"Last release: {previous_tag or 'none'}; {len(commits)} commit(s) since then.")

    parsed, ignored = [], []
    for commit in commits:
        result = parse_commit(commit)
        (parsed if result else ignored).append(result or commit)
    for commit in ignored:
        print(f"  not a Conventional Commit, ignored: {commit.sha[:7]} {commit.subject}")
    if ignored and args.strict:
        raise ValueError(f"{len(ignored)} commit(s) are not Conventional Commits (strict mode)")

    bump = decide_bump(parsed, args.minor_types, args.patch_types)
    previous_text = str(current) if current else ""
    if bump is None:
        print("No feat/fix/perf/breaking commits since the last release. Nothing to release.")
        write_outputs(
            {
                "released": "false",
                "version": previous_text,
                "tag": previous_tag or "",
                "previous-version": previous_text,
                "bump": "none",
            }
        )
        return 0

    version = next_version(current, bump, initial, args.major_on_zero)
    tag = f"{prefix}{version}"
    notes = render_changelog(version, tag, previous_tag, parsed, date, repo_url)
    reason = bump if current else "first release"
    if bump == "major" and current and current.major == 0 and not args.major_on_zero:
        reason = "major, but 0.x only bumps the minor (set major-on-zero to reach 1.0.0)"
    print(f"Bump: {reason} -> {tag}\n\n{notes}")

    if (
        git("tag", "--list", tag).strip()
        or git("ls-remote", "--tags", args.remote, f"refs/tags/{tag}", check=False).strip()
    ):
        raise GitError(f"tag {tag} already exists (but not on HEAD). Refusing to move a release tag.")

    if args.notes_file:
        Path(args.notes_file).write_text(notes, encoding="utf-8")

    outputs = {
        "released": "true",
        "version": str(version),
        "tag": tag,
        "previous-version": previous_text,
        "bump": bump if current else "initial",
        "notes": notes,
    }

    if args.dry_run:
        print("DRY RUN: no file changed, no tag created, nothing pushed.")
        add_summary(f"### Next release would be {tag} ({outputs['bump']})\n\n{notes}")
        write_outputs({**outputs, "released": "false", "dry-run": "true"})
        return 0

    identity = {
        "GIT_AUTHOR_NAME": args.git_user_name,
        "GIT_AUTHOR_EMAIL": args.git_user_email,
        "GIT_COMMITTER_NAME": args.git_user_name,
        "GIT_COMMITTER_EMAIL": args.git_user_email,
    }
    changelog = Path(args.changelog_file)
    changed = update_changelog_file(changelog, notes, version)

    if args.commit_changelog and changed:
        if not args.branch:
            raise ValueError("--commit-changelog needs --branch (or GITHUB_REF_NAME)")
        git("add", "--", str(changelog))
        # [skip ci] stops this commit from starting the pipeline again.
        git("commit", "-m", f"chore(release): {tag} [skip ci]", env=identity)

    # --cleanup=verbatim keeps the Markdown "##" lines, which git would otherwise strip as comments.
    git("tag", "-a", "--cleanup=verbatim", tag, "-m", f"Release {tag}\n\n{notes}", env=identity)
    print(f"Created annotated tag {tag} on {git('rev-parse', '--short', 'HEAD').strip()}")

    if args.push:
        refs = [f"refs/tags/{tag}"]
        if args.commit_changelog and changed:
            refs.insert(0, f"HEAD:refs/heads/{args.branch}")
        token_env = push_auth_env(os.environ.get("SR_TOKEN", ""), server)
        # --atomic: the commit and the tag are pushed together, or neither is.
        git("push", "--atomic", args.remote, *refs, env=token_env)
        print(f"Pushed {' and '.join(refs)} to {args.remote}")

    write_outputs(outputs)
    add_summary(f"### Released {tag}\n\n{notes}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (GitError, ValueError, OSError) as err:
        print(f"::error::{err}" if os.environ.get("GITHUB_ACTIONS") else f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

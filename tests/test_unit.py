"""Unit tests: parsing, bump rules, changelog text. No git needed."""

import datetime as dt

import pytest

from semver_release import (
    Commit,
    Version,
    decide_bump,
    next_version,
    parse_commit,
    render_changelog,
    update_changelog_file,
)

MINOR, PATCH = {"feat"}, {"fix", "perf", "revert"}


def parsed(subject, body=""):
    return parse_commit(Commit(sha="a" * 40, subject=subject, body=body))


# ---------------------------------------------------------------- parsing
@pytest.mark.parametrize(
    "subject, kind, scope, breaking",
    [
        ("feat: add login", "feat", "", False),
        ("fix(api): handle empty body", "fix", "api", False),
        ("feat(auth)!: drop v1 tokens", "feat", "auth", True),
        ("refactor!: rename package", "refactor", "", True),
        ("FEAT: upper case type", "feat", "", False),
        ("perf(db): add index", "perf", "db", False),
    ],
)
def test_conventional_subjects_are_parsed(subject, kind, scope, breaking):
    p = parsed(subject)
    assert (p.type, p.scope, p.breaking) == (kind, scope, breaking)


@pytest.mark.parametrize(
    "subject",
    ["Update README", "feat add login", "feat:no space", "fix(): empty scope", "Merge branch 'x'", "feat: "],
)
def test_other_subjects_are_not_conventional(subject):
    assert parsed(subject) is None


def test_breaking_change_footer_is_found_and_joined():
    p = parsed("feat(api): new pagination", "Details here.\n\nBREAKING CHANGE: page numbers\nstart at 1 now\nRefs: #12")
    assert p.breaking
    assert p.breaking_notes == ["page numbers start at 1 now"]


def test_breaking_change_footer_with_dash_also_counts():
    assert parsed("fix: x", "BREAKING-CHANGE: y").breaking


def test_bang_without_footer_uses_the_description_as_the_note():
    assert parsed("feat!: remove the old API").breaking_notes == ["remove the old API"]


def test_breaking_in_the_middle_of_a_sentence_does_not_count():
    assert not parsed("fix: x", "This is not a BREAKING CHANGE: just text").breaking


# ---------------------------------------------------------------- bump rules
@pytest.mark.parametrize(
    "subjects, expected",
    [
        (["fix: a", "docs: b"], "patch"),
        (["fix: a", "feat: b"], "minor"),
        (["fix: a", "feat!: b"], "major"),
        (["docs: a", "chore: b", "ci: c", "test: d", "style: e", "refactor: f", "build: g"], None),
        (["perf: a"], "patch"),
        (["revert: a"], "patch"),
        ([], None),
    ],
)
def test_the_biggest_bump_wins(subjects, expected):
    assert decide_bump([parsed(s) for s in subjects], MINOR, PATCH) == expected


def test_bump_types_are_configurable():
    assert decide_bump([parsed("docs: a")], MINOR, {"docs"}) == "patch"


@pytest.mark.parametrize(
    "current, bump, expected",
    [
        ("1.4.2", "patch", "1.4.3"),
        ("1.4.2", "minor", "1.5.0"),
        ("1.4.2", "major", "2.0.0"),
        ("0.4.2", "major", "0.5.0"),  # 0.x: breaking changes only bump the minor
        ("0.4.2", "minor", "0.5.0"),
        ("9.9.9", "patch", "9.9.10"),
    ],
)
def test_next_version(current, bump, expected):
    assert str(next_version(Version.parse(current), bump, Version(0, 1, 0), major_on_zero=False)) == expected


def test_major_on_zero_lets_a_breaking_change_reach_1_0_0():
    assert next_version(Version(0, 4, 2), "major", Version(0, 1, 0), major_on_zero=True) == Version(1, 0, 0)


def test_the_first_release_is_the_initial_version():
    assert next_version(None, "major", Version(1, 0, 0), major_on_zero=False) == Version(1, 0, 0)


def test_versions_sort_as_numbers_not_text():
    assert Version.parse("1.10.0") > Version.parse("1.9.0")


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3", "1.2.3-rc.1", "a.b.c"])
def test_bad_versions_are_refused(bad):
    with pytest.raises(ValueError):
        Version.parse(bad)


# ---------------------------------------------------------------- changelog
COMMITS = [
    parsed("feat(api)!: remove v1 endpoints", "BREAKING CHANGE: clients must call /v2"),
    parsed("feat: add search"),
    parsed("fix(ui): button alignment"),
    parsed("perf: cache results"),
    parsed("docs: typo"),
]


def test_changelog_has_sections_in_order_and_hides_docs():
    text = render_changelog(Version(2, 0, 0), "v2.0.0", "v1.4.2", COMMITS, dt.date(2026, 10, 23))
    assert text.startswith("## 2.0.0 (2026-10-23)")
    order = [text.index(h) for h in ("BREAKING CHANGES", "### Features", "### Bug Fixes", "### Performance")]
    assert order == sorted(order)
    assert "* **api:** clients must call /v2 (aaaaaaa)" in text
    assert "* **ui:** button alignment (aaaaaaa)" in text
    assert "typo" not in text


def test_changelog_links_to_commits_and_the_compare_view():
    text = render_changelog(
        Version(2, 0, 0), "v2.0.0", "v1.4.2", COMMITS, dt.date(2026, 10, 23), "https://github.com/o/r"
    )
    assert text.startswith("## [2.0.0](https://github.com/o/r/compare/v1.4.2...v2.0.0) (2026-10-23)")
    assert f"([aaaaaaa](https://github.com/o/r/commit/{'a' * 40}))" in text


def test_new_sections_go_on_top_and_the_same_version_is_never_added_twice(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    first = render_changelog(Version(1, 0, 0), "v1.0.0", None, [parsed("feat: a")], dt.date(2026, 1, 1))
    second = render_changelog(Version(1, 1, 0), "v1.1.0", "v1.0.0", [parsed("feat: b")], dt.date(2026, 2, 1))
    assert update_changelog_file(path, first, Version(1, 0, 0))
    assert update_changelog_file(path, second, Version(1, 1, 0))
    assert not update_changelog_file(path, second, Version(1, 1, 0))
    text = path.read_text()
    assert text.startswith("# Changelog")
    assert text.index("## 1.1.0") < text.index("## 1.0.0")
    assert text.count("## 1.1.0") == 1


def test_an_existing_hand_written_title_is_kept(tmp_path):
    path = tmp_path / "CHANGELOG.md"
    path.write_text("# History of my app\n\nSome intro.\n\n## 0.9.0 (2025-01-01)\n\n* old\n")
    update_changelog_file(path, "## 1.0.0 (2026-01-01)\n\n* new\n", Version(1, 0, 0))
    text = path.read_text()
    assert text.startswith("# History of my app\n\nSome intro.\n\n## 1.0.0")
    assert "## 0.9.0" in text

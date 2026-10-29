import importlib.util
import os
import subprocess

import pytest

_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "contrib", "release_notes.py"
)
_spec = importlib.util.spec_from_file_location("release_notes", _PATH)
rn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rn)


def test_parse_plain_note():
    note = rn.parse("changes/fix-thing.bugfix.md", "Fix the thing\nwhen it breaks.\n")
    assert note.kind == "bugfix"
    assert note.text == "Fix the thing when it breaks."
    assert note.prs == [] and note.authors == []


def test_parse_front_matter():
    note = rn.parse(
        "changes/x.feature.md",
        "---\npr: 702, #705\nauthors: [MikeBishop, @evheros]\n---\n\nAdd x.\n",
    )
    assert note.prs == ["702", "705"]
    assert note.authors == ["MikeBishop", "evheros"]
    assert note.text == "Add x."


@pytest.mark.parametrize(
    "name, content",
    [
        ("changes/x.feat.md", "Add x."),
        ("changes/X.feature.md", "Add x."),
        ("changes/x.feature.md", "   \n"),
        ("changes/x.feature.md", "---\nissue: 1\n---\nAdd x."),
        ("changes/x.feature.md", "---\npr: 1\nAdd x."),
    ],
)
def test_parse_rejects(name, content):
    with pytest.raises(ValueError):
        rn.parse(name, content)


def test_from_subject():
    assert rn.from_subject("Merge pull request #730 from MikeBishop/docs/1.4.1") == (
        "730",
        "MikeBishop",
    )
    assert rn.from_subject("Fix the thing (#12)") == ("12", None)
    assert rn.from_subject("Fix the thing") == (None, None)


def test_render_orders_types_and_prs():
    notes = [
        rn.Note("changes/b.bugfix.md", "bugfix", "Fix b.", ["9"], ["x"]),
        rn.Note("changes/a.feature.md", "feature", "Add a.", ["20"], ["y"]),
        rn.Note("changes/c.feature.md", "feature", "Add c.", ["3"], ["y", "z"]),
        rn.Note("changes/d.feature.md", "feature", "Add d."),
    ]
    assert rn.render(notes, "## v1 - today") == (
        "## v1 - today\n\n"
        "* Features\n"
        "    * (@y / @z) - Add c (#3)\n"
        "    * (@y) - Add a (#20)\n"
        "    * Add d\n"
        "* Bugfixes\n"
        "    * (@x) - Fix b (#9)\n"
    )


def test_advice_flags_long_notes():
    short = rn.Note("changes/a.feature.md", "feature", "Add a. It helps.")
    long = rn.Note("changes/b.feature.md", "feature", "Add b. One. Two.")
    assert rn.advice(short) == []
    assert len(rn.advice(long)) == 1


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def merged(tmp_path, monkeypatch):
    """A repo whose main merged a branch adding one note; returns its path."""
    repo = str(tmp_path)
    _git(repo, "init", "-q", "-b", "main")
    os.mkdir(os.path.join(repo, "changes"))
    _git(repo, "commit", "-q", "--allow-empty", "-m", "root")
    _git(repo, "checkout", "-q", "-b", "topic")
    with open(os.path.join(repo, "changes", "x.feature.md"), "w") as f:
        f.write("Add x.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Add x")
    _git(repo, "checkout", "-q", "main")
    monkeypatch.setattr(rn, "ROOT", repo)
    return repo


def test_resolve_reads_the_merge_subject_without_gh(merged, monkeypatch):
    _git(
        merged,
        "merge",
        "-q",
        "--no-ff",
        "topic",
        "-m",
        "Merge pull request #42 from alice/topic",
    )
    monkeypatch.setattr(rn, "gh", lambda path, jq: None)
    (note,) = rn.load("HEAD")
    assert note.prs == ["42"]
    assert note.authors == ["alice"]


def test_resolve_credits_the_pr_author_not_the_merger(merged, monkeypatch):
    # Merged in git, so no PR in the subject; GitHub still links the commit.
    _git(merged, "merge", "-q", "--no-ff", "topic", "-m", "Merge branch 'topic'")
    answers = {"pulls": "42 bob"}
    monkeypatch.setattr(rn, "gh", lambda path, jq: answers.get(path.split("/")[-1]))
    (note,) = rn.load("HEAD")
    assert note.prs == ["42"]
    assert note.authors == ["bob"]


def test_resolve_without_a_pr_credits_the_commit_author(merged, monkeypatch):
    _git(merged, "merge", "-q", "--ff-only", "topic")
    monkeypatch.setattr(
        rn, "gh", lambda path, jq: "carol" if jq == ".author.login" else None
    )
    (note,) = rn.load("HEAD")
    assert note.prs == []
    assert note.authors == ["carol"]


def test_resolve_named_pr_credits_its_author(merged, monkeypatch):
    os.makedirs(os.path.join(merged, "changes"), exist_ok=True)
    with open(os.path.join(merged, "changes", "x.feature.md"), "w") as f:
        f.write("---\npr: 7\n---\nAdd x.\n")
    _git(merged, "add", ".")
    _git(merged, "commit", "-qm", "Add a note for #7")
    monkeypatch.setattr(
        rn, "gh", lambda path, jq: "dave" if path == "pulls/7" else None
    )
    (note,) = rn.load("HEAD")
    assert note.prs == ["7"]
    assert note.authors == ["dave"]


_CHANGELOG = (
    "# ChangeLog\n\n"
    f"{rn.MARKER}\n\n"
    "## v1.4.0 - 2026-10-26\n\n"
    "* Bugfixes\n"
    "    * Fix a\n\n"
    "## v1.3.4 - 2026-10-23\n\n"
    "* Bugfixes\n"
    "    * Fix old\n"
)
_FIX = [rn.Note("changes/b.bugfix.md", "bugfix", "Fix b.", ["740"], ["x"])]


def test_insert_release_below_marker():
    out = rn.insert(_CHANGELOG, _FIX, "1.4.1", "2026-11-01")
    assert out.startswith(
        f"# ChangeLog\n\n{rn.MARKER}\n\n"
        "## v1.4.1 - 2026-11-01\n\n"
        "* Bugfixes\n"
        "    * (@x) - Fix b (#740)\n\n"
        "## v1.4.0 - 2026-10-26\n"
    )


def test_insert_post_release_after_parent_entries():
    out = rn.insert(_CHANGELOG, _FIX, "1.4.0.post1", "2026-11-02")
    out = rn.insert(out, _FIX, "1.4.0.post2", "2026-11-03")
    assert out == (
        "# ChangeLog\n\n"
        f"{rn.MARKER}\n\n"
        "## v1.4.0 - 2026-10-26\n\n"
        "* Bugfixes\n"
        "    * Fix a\n\n"
        "### v1.4.0.post1 - 2026-11-02\n\n"
        "* Bugfixes\n"
        "    * (@x) - Fix b (#740)\n\n"
        "### v1.4.0.post2 - 2026-11-03\n\n"
        "* Bugfixes\n"
        "    * (@x) - Fix b (#740)\n\n"
        "## v1.3.4 - 2026-10-23\n\n"
        "* Bugfixes\n"
        "    * Fix old\n"
    )


def test_insert_post_release_into_last_section():
    out = rn.insert(_CHANGELOG, _FIX, "1.3.4.post1", "2026-11-02")
    assert out.endswith(
        "    * Fix old\n\n### v1.3.4.post1 - 2026-11-02\n\n"
        "* Bugfixes\n    * (@x) - Fix b (#740)\n"
    )


def test_insert_post_release_needs_its_parent():
    with pytest.raises(ValueError):
        rn.insert(_CHANGELOG, _FIX, "1.2.0.post1", "2026-11-02")

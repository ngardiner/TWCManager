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


def test_resolve_reads_the_merge_that_added_the_note(tmp_path, monkeypatch):
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
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "topic",
        "-m",
        "Merge pull request #42 from alice/topic",
    )

    monkeypatch.setattr(rn, "ROOT", repo)
    monkeypatch.setattr(rn, "gh_login", lambda pr: None)
    (note,) = rn.load("HEAD")
    assert note.prs == ["42"]
    assert note.authors == ["alice"]

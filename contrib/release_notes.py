#!/usr/bin/env python3
"""Collect the release notes in changes/ into CHANGELOG.md.

Each pull request adds a file changes/<slug>.<type>.md holding its changelog
line. The PR number and author are read from git history at release time, so
the file needs neither, and a change merged without a PR is credited to the
commit that added its note. A front matter block (pr: 702, commit: fd00543d,
authors: name) sets them where history would name the wrong change, as for
notes moved between files.

  release_notes.py draft                    print the upcoming section
  release_notes.py build --version 1.4.1    write it to CHANGELOG.md, remove the notes
  release_notes.py check --base origin/main CI: validate notes, require one per PR
"""

import argparse
import datetime
import functools
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES_DIR = "changes"
CHANGELOG = "CHANGELOG.md"
# Named outright: gh's own pick among a clone's remotes may be a fork.
REPO = os.environ.get("GITHUB_REPOSITORY", "ngardiner/TWCManager")
MARKER = "<!-- release notes go below this line -->"

# File type, and the heading it is listed under, in changelog order.
TYPES = {
    "feature": "Features",
    "architecture": "Architecture",
    "bugfix": "Bugfixes",
}

# A PR touching only these paths needs no note.
EXEMPT = re.compile(r"^(docs/|tests/|\.github/|[^/]+\.md$)")

NAME = re.compile(r"^[a-z0-9][a-z0-9-]*\.([a-z]+)\.md$")
MERGE = re.compile(r"^Merge pull request #(\d+) from ([^/\s]+)/")
SQUASH = re.compile(r"\(#(\d+)\)$")
POST = re.compile(r"^(\d+\.\d+\.\d+)\.post\d+$")

# Advisory only: a note longer than this belongs in the docs.
MAX_WORDS = 50
MAX_SENTENCES = 2


class Note:
    def __init__(self, path, kind, text, prs=(), authors=(), commit=None):
        self.path = path
        self.kind = kind
        self.text = text
        self.prs = list(prs)
        self.authors = list(authors)
        self.commit = commit


def git(*args):
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout


def parse(path, content):
    """Read one note file; raises ValueError on a malformed one."""
    match = NAME.match(os.path.basename(path))
    if not match or match.group(1) not in TYPES:
        raise ValueError(
            f"{path}: name must be <slug>.<type>.md, type one of {', '.join(TYPES)}"
        )
    meta = {}
    body = content.strip()
    if body.startswith("---"):
        header, sep, body = body[3:].partition("\n---")
        if not sep:
            raise ValueError(f"{path}: front matter is not closed with ---")
        for line in header.strip().splitlines():
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if key not in ("pr", "commit", "authors"):
                raise ValueError(f"{path}: unknown front matter key {key!r}")
            meta[key] = [v.strip(" #@[]") for v in value.split(",") if v.strip(" #@[]")]
    text = " ".join(body.split())
    if not text:
        raise ValueError(f"{path}: note is empty")
    return Note(
        path,
        match.group(1),
        text,
        meta.get("pr", []),
        meta.get("authors", []),
        (meta.get("commit") or [None])[0],
    )


def advice(note):
    """Style warnings for a note that is too long to scan."""
    warnings = []
    words = len(note.text.split())
    sentences = len(re.findall(r"[.!?](?:\s|$)", note.text + " "))
    if words > MAX_WORDS or sentences > MAX_SENTENCES:
        warnings.append(
            f"{note.path}: {words} words, {sentences} sentences; keep a note to "
            f"1-{MAX_SENTENCES} sentences and put the details in the docs"
        )
    return warnings


def from_subject(subject):
    """(PR number, branch owner) named by a merge or squash commit subject."""
    match = MERGE.match(subject)
    if match:
        return match.group(1), match.group(2)
    match = SQUASH.search(subject)
    if match:
        return match.group(1), None
    return None, None


@functools.lru_cache(maxsize=None)
def gh(path, jq):
    """One GitHub API answer, or None without gh or a match."""
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{REPO}/{path}", "--jq", jq],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return out or None


def commit_pr(sha):
    """(PR number, author) of the first merged PR holding the commit.

    GitHub links a commit to its PR however the PR was merged: the merge
    button, a squash, or a merge made in git and pushed. A commit in stacked
    PRs is linked to each; the first one merged carried it.
    """
    found = gh(
        f"commits/{sha}/pulls",
        "[.[] | select(.merged_at)] | sort_by(.merged_at) | first // empty"
        ' | "\\(.number) \\(.user.login)"',
    )
    if not found:
        return None, None
    pr, login = found.split()
    return pr, login


def resolve(note, ref):
    """Fill in the PR and author from the commit that added the note.

    The PR's author is credited, not whoever merged it. Without gh, the
    merge commit's subject names the PR and the branch's owner, who is the
    author when the branch is on a fork. A note no PR carried names the
    commit that added it, credited to its author.
    """
    if (note.prs or note.commit) and note.authors:
        return
    added = git("log", ref, "--diff-filter=A", "--format=%H", "--", note.path).split()
    merged = git(
        "log",
        ref,
        "--first-parent",
        "--diff-filter=A",
        "--format=%s",
        "--",
        note.path,
    ).splitlines()
    pr, login = commit_pr(added[-1]) if added else (None, None)
    if not pr and merged:
        pr, login = from_subject(merged[-1])
    if not note.prs and pr:
        note.prs = [pr]
    if not note.prs and not note.commit and added:
        note.commit = added[-1][:8]
    if note.authors:
        return
    if note.prs and note.prs[0] != pr:
        login = gh(f"pulls/{note.prs[0]}", ".user.login")
    elif not note.prs and note.commit:
        # Credit whoever GitHub matches to the commit's author.
        login = gh(f"commits/{note.commit}", ".author.login")
    if login:
        note.authors = [login]


def load(ref=None):
    notes = []
    directory = os.path.join(ROOT, NOTES_DIR)
    for name in sorted(os.listdir(directory)):
        if name == "README.md":
            continue
        path = f"{NOTES_DIR}/{name}"
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            note = parse(path, f.read())
        if ref:
            resolve(note, ref)
        notes.append(note)
    return notes


def line(note):
    out = "    * "
    if note.authors:
        out += "(" + " / ".join("@" + a for a in note.authors) + ") - "
    # The changelog's lines carry no closing full stop.
    out += note.text[:-1] if note.text.endswith(".") else note.text
    if note.prs:
        out += " (" + ", ".join("#" + p for p in note.prs) + ")"
    elif note.commit:
        out += f" ({note.commit})"
    return out


def render(notes, heading):
    lines = [heading, ""]
    for kind, title in TYPES.items():
        group = [n for n in notes if n.kind == kind]
        if not group:
            continue
        group.sort(key=lambda n: (int(n.prs[0]) if n.prs else sys.maxsize, n.path))
        lines.append(f"* {title}")
        lines.extend(line(n) for n in group)
    return "\n".join(lines) + "\n"


def insert(changelog, notes, version, date):
    """CHANGELOG.md with the release added; raises ValueError if it can't be.

    A release goes below the marker. A post release (1.4.0.post1) is listed
    after its parent's own entries, so the parent's section shows everything
    the 1.4.0 line shipped.
    """
    match = POST.match(version)
    if not match:
        if MARKER not in changelog:
            raise ValueError(f"{CHANGELOG} has no {MARKER!r} line")
        section = render(notes, f"## v{version} - {date}").rstrip()
        return changelog.replace(MARKER, f"{MARKER}\n\n{section}", 1)

    parent = re.search(
        rf"^## v{re.escape(match.group(1))} .*$", changelog, flags=re.MULTILINE
    )
    if not parent:
        raise ValueError(f"{CHANGELOG} has no v{match.group(1)} section")
    following = re.compile(r"^## ", flags=re.MULTILINE).search(changelog, parent.end())
    end = following.start() if following else len(changelog)
    section = render(notes, f"### v{version} - {date}").rstrip()
    before = changelog[:end].rstrip()
    after = changelog[end:]
    return f"{before}\n\n{section}\n" + (f"\n{after}" if after else "")


def build(version, date, ref):
    notes = load(ref)
    if not notes:
        sys.exit(f"no notes in {NOTES_DIR}/")
    path = os.path.join(ROOT, CHANGELOG)
    with open(path, encoding="utf-8") as f:
        changelog = f.read()
    try:
        changelog = insert(changelog, notes, version, date)
    except ValueError as e:
        sys.exit(str(e))
    with open(path, "w", encoding="utf-8") as f:
        f.write(changelog)
    git("rm", "--quiet", *[n.path for n in notes])
    print(f"Wrote v{version} to {CHANGELOG} from {len(notes)} notes")


def check(base):
    try:
        notes = load()
    except ValueError as e:
        print(f"::error::{e}")
        return 1
    for note in notes:
        for warning in advice(note):
            print(f"::warning file={note.path}::{warning}")

    changed = git("diff", "--name-only", f"{base}...HEAD").split()
    touched = [p for p in changed if p.startswith(NOTES_DIR + "/")]
    if not touched and any(not EXEMPT.match(p) for p in changed):
        print(
            f"::error::Add a release note: changes/<slug>.<type>.md, type one of "
            f"{', '.join(TYPES)}. See {NOTES_DIR}/README.md. A change with "
            f"nothing to tell users can take the 'no changelog' label."
        )
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("draft", help="print the upcoming section")
    draft.add_argument("--ref", default="HEAD")
    build_cmd = sub.add_parser("build", help="write the release to CHANGELOG.md")
    build_cmd.add_argument("--version", required=True)
    build_cmd.add_argument("--date", default=datetime.date.today().isoformat())
    build_cmd.add_argument("--ref", default="HEAD")
    check_cmd = sub.add_parser("check", help="validate notes for CI")
    check_cmd.add_argument("--base", required=True)
    args = parser.parse_args(argv)

    if args.command == "draft":
        print(render(load(args.ref), "## Upcoming"), end="")
    elif args.command == "build":
        build(args.version.lstrip("v"), args.date, args.ref)
    else:
        return check(args.base)
    return 0


if __name__ == "__main__":
    sys.exit(main())

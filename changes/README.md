# Release notes

Every pull request that changes what users see adds one file here, named
`<slug>.<type>.md`:

- `<slug>` is a few lowercase words joined by dashes, naming the change (`gen3-per-unit-targets`).
- `<type>` is `feature`, `architecture` or `bugfix`.

The file holds the changelog line, written for people who install and run
TWCManager:

- One or two sentences.
- Start with a verb in the present tense: "Add …", "Fix …", "Show …", "Remove …".
- Say what changed for the user, not how it was built. Put cadences, edge cases and log formats in the docs.
- Name a setting when the change adds, removes or renames one.

```
Show a picture of each car, in its model, paint and wheels, on the Vehicles pages.
```

Leave out the PR number and your name: they are read from the merge that
added the file when a release is cut. A file moved from elsewhere, or one that
should credit someone else, can set them in front matter:

```
---
pr: 702
authors: MikeBishop, evheros
---

Send commands for a `command_signing: off` car over the free Owner API ahead of the Fleet API.
```

To change a note that has not been released yet, edit or delete its file.
A pull request with nothing to tell users (tests, CI, refactoring) can take
the `no changelog` label instead.

`python3 contrib/release_notes.py draft` prints the notes as the next
release's changelog section; `release_notes.py build --version X.Y.Z` writes
it into CHANGELOG.md and removes the files.

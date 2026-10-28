# Development Guide

## Introduction

Welcome to TWCManager development, and a very big thank you for your contribution to the project. This guide is intended to kick-start development efforts by acting as a knowledgebase for useful details about developing for TWCManager.

## Testing & Developing

The easiest way to maintan and test a TWCManager source tree is to run the TWCManager.py script directly from the repository directory. For example:

### Cloning Git Repository

```
git clone https://github.com/ngardiner/TWCManager
cd TWCManager
```

### Make changes

This is your working branch of the repository.

### Running the script locally

```
./TWCManager.py
```

### Adding new python dependencies

Python dependencies are documented in the setup.py script.

### Release notes

A pull request that changes what users see adds its changelog line as a file in [changes/](../changes/README.md), rather than editing CHANGELOG.md, so pull requests never conflict over the changelog. The PR number and author are filled in when a release is cut.

### Cutting a release

1. Set the version in `setup.py`, `lib/TWCManager/__init__.py` and `lib/TWCManager/TWCMaster.py`.
2. Run `python3 contrib/release_notes.py build --version X.Y.Z`. It writes the release's section into CHANGELOG.md from the files in `changes/` and removes them. With the [GitHub CLI](https://cli.github.com/) logged in, authors come from each pull request; without it, from the branch owner in the merge commit.
3. Commit, then tag `vX.Y.Z` to publish the PyPI package and Docker image.

### Cutting a post release

A post release (`X.Y.Z.postN`) ships fixes from the `release/X.Y.Z` branch. Each fix reaches that branch as a pull request that cherry-picks the fix from main, note file included.

1. On `release/X.Y.Z`, run `python3 contrib/release_notes.py build --version X.Y.Z.postN`. It lists the notes under the `vX.Y.Z` section, after that release's own entries, as `### vX.Y.Z.postN`, and removes the note files. Commit only this change.
2. Cherry-pick that commit onto main. Main's changelog then records the post release, and the fixes it shipped are not listed again in the next release.
3. On `release/X.Y.Z`, set the version, commit and tag `vX.Y.Z.postN`.

## Conventions

### Debug Levels

Currently, there are inconsistent debug levels used throughout the project. This has been flagged as a high priority area of improvement. The following tables aim to clarify 

#### Core (TWCManager, TWCMaster, TWCSlave)

| Debug Level | Used for |
| ----------- | -------- |
| 1           | Notification to users, initialization messages and errors |
| 1           | Confirmation of policy selection |
| 2           | Internal error/parameter issue eg missing value for *internal* function call |
| 7           | Policy selection, module loaded |
| 8           | Policy parameter comparison and non-selection of policy |
| 10          | Loop entry, loop exit debugs |
| 11          | Developer-defined debug checkpoints/output/etc |

#### Modules (Control, EMS, Status)

| Debug Level | Used for |
| ----------- | -------- |
| 1           | Critical error which prevents module functionality (eg. not configured / incorrect config |
| 10          | Loop entry, loop exit debugs |
| 11          | Developer-defined debug checkpoints/output/etc |

### When working with persistent values (config/settings)

The values which are stored in the config and settings dicts are interpreted from JSON storage after each restart. This can cause an issue, in that whilst they are a true representation of the data 

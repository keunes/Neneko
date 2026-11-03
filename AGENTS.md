# Working on Neneko

Notes for coding agents (and people) changing this repository. The README covers what Neneko
does and how to run it; this file covers how changes are committed, reviewed and released.

## Before committing

```sh
pytest -q
ruff check src tests --select F,E9,B   # what CI runs
```

Without kepubify on the PATH the KEPUB tests are skipped; say so if a change touches EPUB output.

## Commits

`main` only takes pull requests. Work on a branch of its own for each task, cut from an
up-to-date `main` (`git fetch origin && git switch -c <short-name> origin/main`), so unrelated
changes never share a pull request. Commit on that branch as you go, without asking first: each
logical change becomes a commit once its tests pass, so an autonomous session leaves a series of
commits to review rather than one large uncommitted diff.

When the task is done, push the branch and open a pull request against `main`
(`gh pr create`). That only runs the tests: the maintainer reviews and merges, and the merge is
what runs CI on `main`, publishes an image when the version changed and closes the issues named
with `Fixes`. Never push to `main`, merge a pull request or delete a branch on GitHub.
Rewording, splitting or squashing commits that aren't pushed yet (`git log @{upstream}..`) is
fine; once pushed, correct things in a new commit rather than a force-push. A commit is the
maintainer's, whoever drafted it, so it has to be something they can stand behind.

- **One logical change per commit**, not one per release. A fix, a related hardening of the
  same weakness elsewhere, and a version bump are three commits. Each commit passes the tests,
  and carries the tests for its own change.
- **Keep unrelated edits out.** No drive-by refactors, reformatting or renames in a fix; if they
  are worth doing, they are their own commit. Stage named paths and read the staged diff
  (`git diff --cached`) before committing; never `git add -A`.
- **If the body needs "Also, …" or "Additionally, …", it is probably two commits.**

### Messages

The diff shows *what* changed; the message is for *why*.

- A subject of at most about 70 characters, in plain words, **then a blank line**, then the body.
  Without the blank line git treats the whole message as the subject.
- The body explains the problem and why this is the fix. For a bug, show the evidence: the
  markup or log line that went wrong and what it turned into, not only an issue link.
- Say what was actually verified and how, e.g. "seen in the kepub on the device" versus
  "reproduced with html5lib, kepubify not run". Don't claim more certainty than that.
- Record trade-offs and dead ends a reviewer would otherwise ask about, and say when a commit is
  groundwork for a later one.
- `Fixes #N` on its own line when the change closes an issue, in the commit and in the pull
  request description (GitHub closes the issue when the pull request is merged); `(#N)` in a
  subject only links it.
- Mark AI assistance per commit with a trailer such as
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, so it is clear which changes had
  help and from what model.
- Wrap the body at about 95 characters, like the existing history.

Example:

```
Keep inline version numbers inside their paragraph

trafilatura turns <code> into <pre>, and _restore_inline_code turns the short ones back.
But it ran on a tree from lxml's HTML parser, which closes a <p> where a <pre> starts, so
"Librera Reader was updated to <code>9.6.17-fdroid</code> and ..." became a paragraph
ending before the version. trafilatura's output is XML, so it is now read as XML.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

### Pull requests

Pull requests are squash-merged: the title becomes the subject of the one commit on `main`, and
the description its body. So the same rules apply as for a commit message, for the change as a
whole: a plain title of at most about 70 characters, then why, the evidence, what was verified
and how (and what wasn't), and trade-offs. Add which cached articles need removing to see the
change, if any. Keep it in sync when later commits on the branch change the story.

## Releases

The version lives only in `src/neneko/__init__.py` (`__version__`). CI
(`.github/workflows/ci.yml`) builds an image only when a merge into `main` brings a version
that has no `v<version>` tag yet; pull requests just run the tests.

- **A version bump is its own commit**, after the changes it releases, with a subject such as
  `Release 0.4.0-dev.4` and a body listing what's in it. When a task changes what Neneko does,
  end its branch with such a commit for the next `-dev.N` version, so merging the pull request
  releases it. One bump per pull request: if the branch has a release commit already, drop it
  (or, once pushed, change the version again in a new commit) and bump once at the end. Leave
  stable versions to the maintainer.
- **Pick a version no one else has**: the next `-dev.N` with no tag (`git tag -l`) that no other
  open pull request uses (`gh pr list`). If another pull request is merged first with the same
  version, merge `main` into the branch and bump again.
- **Pre-releases** have a `-dev.N` suffix: `0.4.0-dev.4`, `0.4.0-dev.5`, … and finally `0.4.0`.
  Use them for anything not yet tried on a Kobo, which is everything an agent releases. CI tags their image with the version and
  `latest-dev`, and makes the GitHub release a pre-release; `latest` doesn't move.
- **Stable releases** (no suffix) are tagged with the version, `latest-dev` and `latest`.
- Version tags never change after they are pushed; a fix gets a new version.

In `docker-compose.yml` the image tag picks what a server runs:

```yaml
image: ghcr.io/keunes/neneko:latest-dev   # every release, pre-releases included: for testing
image: ghcr.io/keunes/neneko:latest       # stable releases only
image: ghcr.io/keunes/neneko:0.4.0-dev.4  # one exact version, e.g. to stay on or go back to it
```

then `docker compose pull && docker compose up -d`.

A new release doesn't rebuild articles that are already built: the cache in `/data` is keyed by
the article's content, not by Neneko's version. When a change affects article output, say which
articles need their cached files removed (or the article removed from the Kobo) to see it.

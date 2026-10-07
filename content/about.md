---
title: "About the archive"
---

The archive runs [neosloc](https://neosloc.github.io/neosloc/) on public repositories and keeps
every result. Each repository is re-evaluated weekly; a new entry is added to its history
whenever its commit or the neosloc version changes.

**What the scores are.** Each of the ten dimensions is a ladder of checkable requirements,
judged per surface (service, CLI, library, desktop app, frontend). A surface reaches level N
when it meets every requirement up to N; the dimension takes the best surface's level.
Dimensions that can't apply are `n/a` and are left out of the index.
[How a level is decided](https://neosloc.github.io/neosloc/dimensions/).

**What they aren't.** The thresholds and the value and make-or-buy coefficients are not
calibrated yet: treat the numbers as rankings, and read the evidence and the next missing
requirement rather than the score. [Calibration and limits](https://neosloc.github.io/neosloc/calibration/).

**How it works.** The repositories are listed in
[`targets.txt`](https://github.com/neosloc/archive/blob/main/targets.txt). A GitHub Actions
workflow clones each one (neosloc only reads the code; nothing from it is executed), runs
`neosloc --json`, validates the report against the
[published schema](https://neosloc.github.io/neosloc/schema/report-v2.json), commits it, and
rebuilds this site. Every report can be downloaded as JSON from its page.

**Adding a repository.** [Open an issue](https://github.com/neosloc/archive/issues/new?template=evaluate.yml)
with its URL: the evaluation starts as soon as the issue is created, the summary is posted as
a reply, and the repository joins the weekly re-evaluation. Repositories over 500 MB wait for
a maintainer's approval. A pull request adding the URL to `targets.txt` works too.

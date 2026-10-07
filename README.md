# neosloc archive

[neosloc](https://github.com/neosloc/neosloc) assessments of public repositories, kept over
time and published at **<https://neosloc.github.io/archive/>**.

## How it works

- `targets.txt` lists the repositories, one URL per line.
- `archive.py evaluate` clones each one (files over 1 MB are fetched lazily; nothing from the
  repository is executed), runs `neosloc --json` at the version pinned in
  `requirements.txt`, validates the report against neosloc's JSON Schema, and writes:
  - `data/reports/<slug>.json`: the latest report, with metadata (commit, date, neosloc version);
  - `data/history/<slug>.json`: one entry per evaluated commit or neosloc version;
  - `static/reports/<slug>.json`: the same report, downloadable from the site;
  - `content/repos/<slug>.md`: the repository's page (JSON front matter).
  Unchanged repositories (same commit, same neosloc version) are skipped.
- `layouts/` holds the Hugo templates; `hugo` builds the site.

## Workflows

| Workflow | When | What |
|---|---|---|
| `evaluate` | weekly, on changes to `targets.txt`, `requirements.txt` or `archive.py`, or manually (optionally for one URL, optionally forced) | evaluates, commits the reports, redeploys the site |
| `request` | any new issue containing a repository URL (or the `approved` label, for repositories over 500 MB) | validates the URL, adds it, evaluates it, stores the report, replies with the summary and closes the issue |
| `site` | pushes to `main`, and after the other two | builds with Hugo 0.128 and deploys to GitHub Pages |

## Locally

```bash
pip install -r requirements.txt
python archive.py add https://github.com/OWNER/REPO
python archive.py evaluate --only https://github.com/OWNER/REPO
hugo server
```

## Re-evaluating with a new neosloc

Bump the pin in `requirements.txt`: every repository is re-evaluated (the version is part
of what makes a report current), and each gets a new history entry.

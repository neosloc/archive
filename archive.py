#!/usr/bin/env python3
"""Evaluate repositories with neosloc and store the results for the Hugo site.

    python archive.py evaluate [--only URL] [--force]   evaluate targets.txt (or one URL)
    python archive.py add URL [--max-mb N]              validate and append a target
    python archive.py summary SLUG                      Markdown summary of a stored report
    python archive.py pages                             regenerate content pages from stored reports
    python archive.py list                              print targets and their slugs

For each target the repository is cloned (history included, file contents fetched
on demand), `neosloc --json` runs on it, and the result is written to:

    data/reports/<slug>.json     latest report, for Hugo templates
    data/history/<slug>.json     one entry per evaluated commit / neosloc version
    static/reports/<slug>.json   the same report, downloadable from the site
    content/repos/<slug>.md      the page for the repository

neosloc only reads the code; nothing from the evaluated repository is executed.
Standard library only (jsonschema is used when installed).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGETS = os.path.join(ROOT, "targets.txt")
URL_RE = re.compile(r"^https://(github\.com|gitlab\.com|codeberg\.org|bitbucket\.org)/([\w.-]+)/([\w.-]+?)(\.git)?/?$")
CLONE_TIMEOUT = 600
EVAL_TIMEOUT = 900
HISTORY_LIMIT = 200


def log(msg: str) -> None:
    print("archive: " + msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# targets


def parse_url(url: str) -> Optional[Dict[str, str]]:
    m = URL_RE.match(url.strip())
    if not m:
        return None
    host, owner, repo = m.group(1), m.group(2), m.group(3)
    prefix = "" if host == "github.com" else host.split(".")[0] + "-"
    return {"url": "https://%s/%s/%s" % (host, owner, repo), "host": host, "owner": owner, "repo": repo,
            "name": "%s/%s" % (owner, repo), "slug": (prefix + owner + "-" + repo).lower().replace(".", "-")}


def read_targets() -> List[Dict[str, str]]:
    out = []
    with open(TARGETS) as fh:
        for n, line in enumerate(fh, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            t = parse_url(line)
            if t is None:
                raise SystemExit("targets.txt:%d: not a supported repository URL: %s" % (n, line))
            out.append(t)
    return out


def github_size_mb(t: Dict[str, str]) -> Optional[float]:
    """Repository size from the GitHub API (None when unknown or not on GitHub)."""
    if t["host"] != "github.com":
        return None
    import urllib.request
    req = urllib.request.Request("https://api.github.com/repos/%s/%s" % (t["owner"], t["repo"]),
                                 headers={"Accept": "application/vnd.github+json"})
    if os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp).get("size", 0) / 1024.0
    except Exception:
        return None


def cmd_add(url: str, max_mb: float = 0) -> int:
    """Exit 0 (prints the slug), 2 invalid URL, 3 unreachable, 4 larger than max_mb."""
    t = parse_url(url)
    if t is None:
        log("not a supported repository URL (https://github.com/OWNER/REPO): %s" % url)
        return 2
    if any(x["slug"] == t["slug"] for x in read_targets()):
        log("already a target: %s" % t["url"])
        print(t["slug"])
        return 0
    r = subprocess.run(["git", "ls-remote", "--heads", t["url"]], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    if r.returncode != 0:
        log("not a reachable public repository: %s" % t["url"])
        return 3
    size = github_size_mb(t) if max_mb else None
    if size is not None and size > max_mb:
        log("%s is %.0f MB, over the %.0f MB limit for automatic evaluation" % (t["url"], size, max_mb))
        return 4
    with open(TARGETS, "a") as fh:
        fh.write(t["url"] + "\n")
    print(t["slug"])
    return 0


# ---------------------------------------------------------------------------
# evaluation


def run(cmd: List[str], timeout: int, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                          env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))


def neosloc_version() -> str:
    out = run([sys.executable, "-m", "neosloc", "--version"], 60).stdout.decode().strip()
    return out.split()[-1]


def load_json(path: str, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=False)
        fh.write("\n")


def validator():
    try:
        import jsonschema
    except ImportError:
        return None
    schema = json.loads(run([sys.executable, "-m", "neosloc", "--schema"], 60).stdout)
    return jsonschema.Draft202012Validator(schema)


def evaluate(t: Dict[str, str], version: str, force: bool, check) -> Optional[str]:
    """Evaluate one target; return 'updated', 'unchanged' or None on failure."""
    report_path = os.path.join(ROOT, "data", "reports", t["slug"] + ".json")
    previous = load_json(report_path, None)
    work = tempfile.mkdtemp(prefix="archive-")
    try:
        dest = os.path.join(work, t["repo"])
        log("cloning %s" % t["url"])
        clone = run(["git", "clone", "--quiet", "--filter=blob:limit=1m", t["url"], dest], CLONE_TIMEOUT)
        if clone.returncode != 0:
            log("clone failed for %s: %s" % (t["url"], clone.stderr.decode(errors="replace").strip()[:300]))
            return None
        sha = run(["git", "-C", dest, "rev-parse", "HEAD"], 60).stdout.decode().strip()
        if previous and not force and previous["meta"]["commit"] == sha and previous["meta"]["neosloc"] == version:
            log("unchanged: %s @ %s" % (t["name"], sha[:12]))
            return "unchanged"
        log("evaluating %s @ %s with neosloc %s" % (t["name"], sha[:12], version))
        res = run([sys.executable, "-m", "neosloc", "--json", "-q", dest], EVAL_TIMEOUT)
        try:
            report = json.loads(res.stdout)
        except ValueError:
            log("neosloc produced no JSON for %s (exit %d): %s" % (t["name"], res.returncode,
                                                                   res.stderr.decode(errors="replace")[:300]))
            return None
        if "error" in report:
            log("neosloc error for %s: %s" % (t["name"], report["error"]["message"]))
            return None
        if check is not None:
            errors = list(check.iter_errors(report))
            if errors:
                log("report for %s does not match the schema: %s" % (t["name"], errors[0].message))
                return None
        report["target"] = t["url"]  # instead of the temporary clone path
        committed = run(["git", "-C", dest, "log", "-1", "--format=%cI"], 60).stdout.decode().strip()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    meta = {"name": t["name"], "url": t["url"], "slug": t["slug"], "host": t["host"], "commit": sha,
            "committed_at": committed, "evaluated_at": now, "neosloc": version}
    doc = {"meta": meta, "report": report}
    write_json(report_path, doc)
    write_json(os.path.join(ROOT, "static", "reports", t["slug"] + ".json"), doc)

    est, mb = report["estimate"], report.get("make_or_buy") or {}
    history_path = os.path.join(ROOT, "data", "history", t["slug"] + ".json")
    history = load_json(history_path, {"entries": []})
    history["entries"].append({
        "evaluated_at": now, "commit": sha, "neosloc": version,
        "index": est["integrability_index"], "assessed": est.get("assessed_dimensions"),
        "levels": {d["key"]: d["level"] for d in report["dimensions"]},
        "verdict": mb.get("verdict"), "make_buy_ratio": mb.get("make_buy_ratio"),
    })
    history["entries"] = history["entries"][-HISTORY_LIMIT:]
    write_json(history_path, history)

    write_page(meta, report)
    return "updated"


def write_page(meta: Dict, report: Dict) -> None:
    est = report["estimate"]
    mb = report.get("make_or_buy") or {}
    front = {
        "title": meta["name"],
        "slug": meta["slug"],
        "date": meta["evaluated_at"],
        "repo_url": meta["url"],
        "index": est["integrability_index"],
        "assessed": est.get("assessed_dimensions"),
        "surfaces": (report.get("surfaces") or {}).get("kinds", []),
        "verdict": mb.get("verdict"),
        "make_buy_ratio": mb.get("make_buy_ratio"),
        "languages": list((report["inventory"].get("languages") or {}).keys())[:4],
        # sloccount's classic COCOMO next to neosloc's value (neoCOCOMO), both in USD
        "cocomo_cost": (report.get("value") or {}).get("classic", {}).get("cost"),
        "cocomo_pm": (report.get("value") or {}).get("classic", {}).get("person_months"),
        "value_cost": (report.get("value") or {}).get("value_cost"),
        "value_pm": (report.get("value") or {}).get("value_pm"),
    }
    path = os.path.join(ROOT, "content", "repos", meta["slug"] + ".md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(json.dumps(front, indent=1) + "\n")  # Hugo reads JSON front matter


BOXES = {0: "□□□□", 1: "■□□□", 2: "■■□□", 3: "■■■□", 4: "■■■■"}
SITE = "https://neosloc.github.io/archive/"


def cmd_summary(slug: str) -> int:
    """Print a Markdown summary of a stored report (used for issue replies)."""
    doc = load_json(os.path.join(ROOT, "data", "reports", slug + ".json"), None)
    if doc is None:
        log("no report for %s" % slug)
        return 1
    m, r = doc["meta"], doc["report"]
    est, mb = r["estimate"], r.get("make_or_buy") or {}
    surfaces = ", ".join((r.get("surfaces") or {}).get("kinds") or ["none detected"])
    out = ["### neosloc evaluation of [%s](%s)" % (m["name"], m["url"]), "",
           "**Integrability index:** %.2f over %d applicable dimensions · **Surfaces:** %s"
           % (est["integrability_index"], est.get("assessed_dimensions", 10), surfaces)]
    if mb:
        out.append("**Make or buy:** %s (make costs %.2f× buying over %g years)"
                   % (mb["verdict"].upper(), mb["make_buy_ratio"], mb["horizon_years"]))
    out += ["", "| Dimension | Level | Next requirement |", "|---|---|---|"]
    for d in r["dimensions"]:
        level = "n/a" if d["level"] is None else "%s %d %s" % (BOXES[d["level"]], d["level"], d["level_name"])
        nxt = d["gaps"][0] if d["gaps"] else ("–" if d["level"] is not None else d["rationale"])
        out.append("| %s | %s | %s |" % (d["title"], level, nxt.replace("|", "\\|")))
    out += ["", "[Full report](%srepos/%s/) · [JSON](%sreports/%s.json) · commit `%s`, neosloc %s. "
            "Scores are uncalibrated rankings: read the next requirement, not the number."
            % (SITE, slug, SITE, slug, m["commit"][:12], m["neosloc"])]
    print("\n".join(out))
    return 0


def cmd_evaluate(only: Optional[str], force: bool, jobs: int = 1) -> int:
    targets = read_targets()
    if only:
        t = parse_url(only)
        if t is None:
            log("not a supported repository URL: %s" % only)
            return 2
        targets = [x for x in targets if x["slug"] == t["slug"]] or [t]
    version = neosloc_version()
    check = validator()
    results = {"updated": 0, "unchanged": 0, "failed": 0}

    def one(t):
        try:
            return evaluate(t, version, force, check)
        except subprocess.TimeoutExpired:
            log("timed out: %s" % t["url"])
            return None

    # Each target writes only its own files, so targets can run in parallel.
    with concurrent.futures.ThreadPoolExecutor(max(1, jobs)) as pool:
        for outcome in pool.map(one, targets):
            results[outcome or "failed"] += 1
    log("%(updated)d updated, %(unchanged)d unchanged, %(failed)d failed" % results)
    return 1 if results["failed"] and not (results["updated"] or results["unchanged"]) else 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="archive.py", description="Evaluate repositories with neosloc for the archive.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("evaluate", help="evaluate targets.txt")
    ev.add_argument("--only", help="evaluate a single repository URL")
    ev.add_argument("--force", action="store_true", help="re-evaluate even if commit and neosloc version are unchanged")
    ev.add_argument("--jobs", type=int, default=1, help="repositories to evaluate in parallel (default: 1)")
    ad = sub.add_parser("add", help="validate and append a repository URL to targets.txt")
    ad.add_argument("url")
    ad.add_argument("--max-mb", type=float, default=0, help="refuse GitHub repositories larger than this (0: no limit)")
    sub.add_parser("pages", help="regenerate content/repos pages from data/reports (no evaluation)")
    su = sub.add_parser("summary", help="Markdown summary of a stored report")
    su.add_argument("slug")
    sub.add_parser("list", help="print targets")
    args = ap.parse_args(argv)
    if args.cmd == "evaluate":
        return cmd_evaluate(args.only, args.force, args.jobs)
    if args.cmd == "add":
        return cmd_add(args.url, args.max_mb)
    if args.cmd == "summary":
        return cmd_summary(args.slug)
    if args.cmd == "pages":
        n = 0
        for name in sorted(os.listdir(os.path.join(ROOT, "data", "reports"))):
            doc = load_json(os.path.join(ROOT, "data", "reports", name), None)
            if doc:
                write_page(doc["meta"], doc["report"])
                n += 1
        log("%d pages written" % n)
        return 0
    for t in read_targets():
        print("%-45s %s" % (t["slug"], t["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

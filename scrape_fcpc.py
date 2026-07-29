#!/usr/bin/env python3
"""
Franklin County Probate Court -- forms harvester and change monitor.

Builds a structured corpus of every court form: metadata, the PDF itself,
extracted AcroForm field names, and content hashes for change detection.

Designed to be re-run on a schedule. On every run after the first it emits a
diff report (added / removed / changed forms) so you have a human review gate
before any change propagates into document assembly.

Usage:
    pip install requests beautifulsoup4 pypdf
    python scrape_fcpc.py --out ./corpus
    python scrape_fcpc.py --out ./corpus --no-download      # metadata only, fast
    python scrape_fcpc.py --out ./corpus --diff-only        # re-check, don't refetch PDFs

Output layout:
    corpus/
      manifest.json          canonical record of every form
      forms.csv              flat table for eyeballing / import
      fields.json            form_number -> PDF field names & types
      pdfs/<category>/<form_number>__<slug>.pdf
      diffs/diff_<timestamp>.json
      raw_html/<category>.html
"""

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://probate.franklincountyohio.gov"

# Discovered from the site's Forms navigation. Verify against /Forms on each run;
# the script warns if the nav exposes a category not listed here.
CATEGORY_PAGES = {
    "adoption": "/Forms/Adoption-Forms",
    "advance-directives": "/Forms/Advance-Directives-Forms",
    "birth-records": "/Forms/Birth-Records-Forms",
    "civil": "/Forms/Civil-Forms",
    "custodial-account": "/Forms/Custodial-Account-Forms",
    "disinterment": "/Forms/Disinterment-Forms",
    "guardianship": "/Forms/Guardianship-Forms",
    "large-estate": "/Forms/Large-Estate-Forms",
    "marriage": "/Forms/Marriage-Forms",
    "minors-settlement": "/Forms/Minors-Settlement-Forms",
    "miscellaneous": "/Forms/Miscellaneous-Forms",
    "name-change": "/Forms/Name-Change-Forms",
    "civil-commitment": "/Forms/Involuntary-Civil-Commitment-Forms",
    "small-estate": "/Forms/Small-Estate-Forms",
    "successor-custodian": "/Forms/Successor-Custodian-Forms",
    "trust": "/Forms/Trust-Forms",
    "unclaimed-funds": "/Forms/Unclaimed-Funds-Forms",
    "wrongful-death-trust": "/Forms/Wrongful-Death-Trust-Declaration-Forms",
}

# Non-form pages worth capturing for the knowledge base. Local rules in
# particular drive deadlines and are the authority your engine should key on.
REFERENCE_PAGES = {
    "local-rules": "/Local-Rules-Administrative-Orders",
    "court-costs": "/About/Court-Information/Court-Costs",
    "efiling": "/About/e-Filing",
    "court-pamphlets": "/About/Court-Information/Court-Pamphlets",
}

USER_AGENT = (
    "GasperLegal-FormsMonitor/1.0 (+chris@gasperlegal.com) "
    "python-requests"
)
DELAY_SECONDS = 1.0  # be a polite citizen; this is a county server

SIZE_SUFFIX = re.compile(r"\s*\(PDF,\s*[\d.]+\s*[KMG]B\)\s*$", re.I)
VERSION_TOKEN = re.compile(r"/v/(\d+)/")


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def check_robots(session):
    """Read robots.txt and report. Do not skip this step."""
    rp = urllib.robotparser.RobotFileParser()
    url = f"{BASE}/robots.txt"
    try:
        resp = session.get(url, timeout=30)
        rp.parse(resp.text.splitlines())
        print(f"[robots] fetched {url}")
        blocked = [
            p for p in list(CATEGORY_PAGES.values()) + list(REFERENCE_PAGES.values())
            if not rp.can_fetch(USER_AGENT, BASE + p)
        ]
        if blocked:
            print(f"[robots] WARNING -- disallowed paths: {blocked}", file=sys.stderr)
            return rp, False
        return rp, True
    except Exception as e:  # noqa: BLE001
        print(f"[robots] could not read robots.txt ({e}); proceeding cautiously",
              file=sys.stderr)
        return None, True


def get(session, url, **kw):
    time.sleep(DELAY_SECONDS)
    r = session.get(url, timeout=60, **kw)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def slugify(text, maxlen=80):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:maxlen]


SKIP_HEADINGS = {"site footer", "menu", "search", "quick links",
                 "share & connect", "contact us"}

STEP_PATTERN = re.compile(r"^\s*step\s+\d+[A-Za-z]?\s*[:.\-]", re.I)


def main_content(soup):
    """Isolate the article body so nav and footer headings don't pollute
    the track/step context."""
    for sel in [{"id": "main-content"}, {"role": "main"}, {"class": "content"}]:
        node = soup.find(attrs=sel)
        if node:
            return node
    return soup


def parse_category(html, category, path):
    """Walk the page in document order, tracking the procedural context each
    table sits under.

    Franklin's pages -- Small Estate especially -- organize forms into
    procedural TRACKS (h2, e.g. 'Summary Release', 'No Administration') and
    STEPS within each track (bold paragraphs, e.g. 'Step 1: Initial Forms
    (Required to Open a Case)', 'Step 2B: Additional Forms Required if a
    Commissioner is Necessary').

    That structure is the court's own filing packet definition and is the most
    valuable thing on the site. It is NOT expressed as headings, so a naive
    heading walk loses it. Capture track, step, and any prose note between the
    step label and the table -- the notes carry conditions and attachment
    requirements ('A copy of the will must be attached...').
    """
    soup = BeautifulSoup(html, "html.parser")
    body = main_content(soup)
    rows = []

    track = None      # h2/h3 -- procedural track or form group
    step = None       # bold 'Step N:' label
    note = None       # prose immediately preceding the table
    order = 0

    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "strong", "b",
                             "table"]):
        if el.name in ("h1", "h2", "h3", "h4"):
            txt = el.get_text(" ", strip=True)
            if txt and txt.lower() not in SKIP_HEADINGS:
                track, step, note = txt, None, None
            continue

        if el.name in ("p", "strong", "b"):
            txt = el.get_text(" ", strip=True)
            if not txt:
                continue
            if STEP_PATTERN.match(txt):
                step, note = txt, None
            elif el.name == "p" and len(txt) < 400:
                # candidate condition / attachment note
                note = txt
            continue

        # el is a table
        subcat = track
        for tr in el.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            form_number = cells[0].get_text(" ", strip=True)
            link = cells[1].find("a", href=True)
            if not link:
                continue
            if form_number.lower().startswith("form number"):
                continue  # header row

            name = SIZE_SUFFIX.sub("", link.get_text(" ", strip=True)).strip()
            href = urllib.parse.urljoin(BASE + path, link["href"])
            vm = VERSION_TOKEN.search(href)

            rows.append({
                "form_number": form_number,
                "form_name": name,
                "category": category,
                "subcategory": subcat,
                "pdf_url": href,
                # The court's CDN embeds a /v/N/ token that increments when a
                # document is republished. Cheap, reliable change signal --
                # but it also means hardcoded URLs go stale. Never hardcode.
                "url_version": int(vm.group(1)) if vm else None,
                # Observed convention: some numbers carry a leading "e"
                # (ePC-E-4.0 vs PC-E-4.0A). This appears to mark forms tied to
                # e-filing / proposed entries. UNVERIFIED -- confirm with the
                # Clerk before relying on it for routing logic.
                "efile_prefix_flag": form_number.startswith("e"),
                "source_page": BASE + path,
                # procedural context -- the packet structure
                "track": track,
                "step": step,
                "context_note": note,
                "page_order": order,
            })
            order += 1
        note = None
    return rows


def check_nav_for_new_categories(html):
    """The nav is the source of truth for what categories exist. If the court
    adds one, we want to know rather than silently miss it."""
    soup = BeautifulSoup(html, "html.parser")
    found = set()
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if "/Forms/" in h and h.rstrip("/").count("/") >= 2:
            found.add(urllib.parse.urlparse(h).path.rstrip("/"))
    known = {p.rstrip("/") for p in CATEGORY_PAGES.values()}
    unknown = {p for p in found if p not in known}
    if unknown:
        print(f"[nav] NEW/UNMAPPED category pages detected: {sorted(unknown)}",
              file=sys.stderr)
    return sorted(unknown)


# --------------------------------------------------------------------------
# PDF handling
# --------------------------------------------------------------------------

def extract_pdf_fields(pdf_path):
    """Pull AcroForm field names/types. This is the payload your assembly layer
    needs. Expect gaps: some court PDFs are flat scans or use XFA, in which
    case there are no fields to extract and you must template the document
    yourself rather than fill theirs."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return {"error": "pypdf not installed"}

    try:
        reader = PdfReader(str(pdf_path))
        fields = reader.get_fields() or {}
        out = {}
        for name, spec in fields.items():
            out[name] = {
                "type": spec.get("/FT"),
                "alt_name": spec.get("/TU"),
                "default": spec.get("/DV"),
                "options": spec.get("/Opt"),
            }
        return {
            "page_count": len(reader.pages),
            "field_count": len(out),
            "fillable": len(out) > 0,
            "fields": out,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "fillable": None}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------

def diff_manifests(old, new):
    def index(m):
        # A form can legitimately appear in several tracks/steps (PC-E-1.0 shows
        # up in most small-estate packets). Key on packet position so those do
        # not silently collapse into one another.
        return {
            (r["category"], r.get("track"), r.get("step"), r["form_number"]): r
            for r in m["forms"]
        }

    o, n = index(old), index(new)
    added = [n[k] for k in n.keys() - o.keys()]
    removed = [o[k] for k in o.keys() - n.keys()]
    changed = []
    for k in o.keys() & n.keys():
        deltas = {}
        for field in ("form_name", "pdf_url", "url_version", "sha256",
                      "field_count", "subcategory"):
            if o[k].get(field) != n[k].get(field):
                deltas[field] = {"was": o[k].get(field), "now": n[k].get(field)}
        if deltas:
            changed.append({"category": k[0], "track": k[1], "step": k[2],
                            "form_number": k[3], "changes": deltas})
    return {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "added": added,
        "removed": removed,
        "changed": changed,
        "requires_attorney_review": bool(added or removed or changed),
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="./corpus")
    ap.add_argument("--no-download", action="store_true",
                    help="metadata only; skip PDF download and field extraction")
    ap.add_argument("--diff-only", action="store_true",
                    help="rebuild metadata and diff, reuse cached PDFs")
    args = ap.parse_args()

    out = Path(args.out)
    (out / "pdfs").mkdir(parents=True, exist_ok=True)
    (out / "raw_html").mkdir(parents=True, exist_ok=True)
    (out / "diffs").mkdir(parents=True, exist_ok=True)

    session = make_session()
    check_robots(session)

    # sanity-check the category list against live nav
    index_html = get(session, BASE + "/Forms").text
    unmapped = check_nav_for_new_categories(index_html)

    all_rows = []
    for category, path in CATEGORY_PAGES.items():
        print(f"[page] {category}")
        try:
            html = get(session, BASE + path).text
        except Exception as e:  # noqa: BLE001
            print(f"  !! failed: {e}", file=sys.stderr)
            continue
        (out / "raw_html" / f"{category}.html").write_text(html, encoding="utf-8")
        rows = parse_category(html, category, path)
        print(f"  {len(rows)} forms")
        all_rows.extend(rows)

    # reference / rules pages -- keep the HTML, these feed the RAG side
    for name, path in REFERENCE_PAGES.items():
        try:
            html = get(session, BASE + path).text
            (out / "raw_html" / f"_ref_{name}.html").write_text(html, encoding="utf-8")
            print(f"[ref]  {name}")
        except Exception as e:  # noqa: BLE001
            print(f"  !! {name} failed: {e}", file=sys.stderr)

    # download + field extraction
    if not args.no_download:
        for row in all_rows:
            cat_dir = out / "pdfs" / row["category"]
            cat_dir.mkdir(parents=True, exist_ok=True)
            fname = f"{slugify(row['form_number'])}__{slugify(row['form_name'])}.pdf"
            dest = cat_dir / fname

            if not (args.diff_only and dest.exists()):
                try:
                    r = get(session, row["pdf_url"])
                    dest.write_bytes(r.content)
                except Exception as e:  # noqa: BLE001
                    print(f"  !! {row['form_number']}: {e}", file=sys.stderr)
                    row["download_error"] = str(e)
                    continue

            row["local_path"] = str(dest.relative_to(out))
            row["sha256"] = sha256_file(dest)
            info = extract_pdf_fields(dest)
            row["page_count"] = info.get("page_count")
            row["field_count"] = info.get("field_count")
            row["fillable"] = info.get("fillable")
            row["_fields"] = info.get("fields", {})
            if "error" in info:
                row["pdf_error"] = info["error"]

    # split fields out of the manifest to keep it readable
    fields_map = {}
    for row in all_rows:
        f = row.pop("_fields", None)
        if f:
            fields_map[f"{row['category']}:{row['form_number']}"] = f

    manifest = {
        "source": BASE,
        "harvested": dt.datetime.now().isoformat(timespec="seconds"),
        "category_count": len(CATEGORY_PAGES),
        "form_count": len(all_rows),
        "unmapped_nav_categories": unmapped,
        "forms": sorted(all_rows, key=lambda r: (r["category"], r["form_number"])),
    }

    manifest_path = out / "manifest.json"
    prior = None
    if manifest_path.exists():
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / "fields.json").write_text(json.dumps(fields_map, indent=2), encoding="utf-8")

    cols = ["category", "subcategory", "form_number", "form_name", "pdf_url",
            "url_version", "efile_prefix_flag", "fillable", "field_count",
            "page_count", "sha256", "local_path"]
    with open(out / "forms.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(manifest["forms"])

    if prior:
        d = diff_manifests(prior, manifest)
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        (out / "diffs" / f"diff_{stamp}.json").write_text(
            json.dumps(d, indent=2), encoding="utf-8")
        print(f"\n[diff] +{len(d['added'])} -{len(d['removed'])} "
              f"~{len(d['changed'])}")
        if d["requires_attorney_review"]:
            print("[diff] CHANGES DETECTED -- review before promoting to production.")

    print(f"\nDone. {len(all_rows)} forms across {len(CATEGORY_PAGES)} categories "
          f"-> {out.resolve()}")


if __name__ == "__main__":
    main()

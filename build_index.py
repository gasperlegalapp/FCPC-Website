#!/usr/bin/env python3
"""
Transform the raw harvest into the published corpus index.

    manifest.json  --(taxonomy.yaml + deadline_rules.yaml)-->  index.json

index.json is the ONLY artifact your workflow app should read. It is stable,
versioned, and queryable four ways:

    by matter type + filing stage
    by trigger event / deadline rule
    by task template (the court's own packet steps)
    by form number

Usage:
    pip install pyyaml
    python build_index.py --corpus ./corpus --version 2026.08.0
"""

import argparse
import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path

import yaml

SERIES_RE = re.compile(r"(?:^|-)(\d+)(?:\.|$)")


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


def parse_series(form_number):
    """PC-E-13.8e -> '13'; ePC-EGT-1.R2 -> '1'; PC-45D -> '45'."""
    m = SERIES_RE.search(form_number.replace("ePC", "").replace("PC", ""))
    return m.group(1) if m else None


def classify_role(name, patterns):
    for p in patterns:
        if re.search(p["pattern"], name, re.I):
            return p["role"]
    return "other"


def infer_companions(form_number, all_numbers, overrides):
    """An entry usually shares the base number with its application
    (ePC-E-4.0 / ePC-E-4.0e). Convention-derived, with explicit overrides."""
    if form_number in overrides:
        return list(overrides[form_number])
    if form_number.endswith(("e", "j")):
        return []
    guesses = [form_number + suffix for suffix in ("e", "j")]
    return [g for g in guesses if g in all_numbers]


def build(corpus_dir, version):
    corpus = Path(corpus_dir)
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    tax = yaml.safe_load(Path("taxonomy.yaml").read_text(encoding="utf-8"))
    deadlines = yaml.safe_load(Path("deadline_rules.yaml").read_text(encoding="utf-8"))

    in_scope = set(tax["scope"]["categories"])
    rows = [r for r in manifest["forms"] if r["category"] in in_scope]

    # form_number -> deadline rule ids
    form_to_rules = defaultdict(list)
    for rule in deadlines["rules"]:
        for fn in rule.get("forms", []):
            form_to_rules[fn].append(rule["id"])
        for fn in rule.get("extendable_via", []):
            form_to_rules[fn].append(rule["id"])

    all_numbers = {r["form_number"] for r in rows}

    # ---- forms: one record per unique form ---------------------------------
    forms = {}
    packets = defaultdict(lambda: {"forms": [], "notes": set()})

    for r in rows:
        fn = r["form_number"]
        key = f"{tax['county']}.{fn}"

        if key not in forms:
            series = parse_series(fn)
            forms[key] = {
                "id": key,
                "form_number": fn,
                "form_name": r["form_name"],
                "county": tax["county"],
                "matter_types": set(),
                "filing_stage": tax["series_to_stage"].get(series, "unclassified"),
                "series": series,
                "doc_role": classify_role(r["form_name"], tax["doc_role_patterns"]),
                "efile_prefix_flag": r["efile_prefix_flag"],
                "deadline_rule_ids": form_to_rules.get(fn, []),
                "task_template_keys": set(),
                "companions": infer_companions(
                    fn, all_numbers, tax.get("companion_overrides", {})),
                "source_url": r["pdf_url"],
                "url_version": r.get("url_version"),
                "pdf": {
                    "path": r.get("local_path"),
                    "sha256": r.get("sha256"),
                    "fillable": r.get("fillable"),
                    "field_count": r.get("field_count"),
                    "page_count": r.get("page_count"),
                },
            }

        forms[key]["matter_types"].add(
            tax["category_to_matter_type"][r["category"]])

        # ---- packets: the court's published track/step structure -----------
        if r.get("track"):
            mt = tax["category_to_matter_type"][r["category"]]
            tkey = ".".join(filter(None, [mt, slug(r["track"]), slug(r["step"])]))
            p = packets[tkey]
            p["key"] = tkey
            p["matter_type"] = mt
            p["track"] = r["track"]
            p["step"] = r["step"]
            p["source"] = "court_published"
            p["conditional"] = bool(r.get("step") and
                                    re.search(r"\bif\b", r["step"], re.I))
            if r["form_number"] not in p["forms"]:
                p["forms"].append(r["form_number"])
            if r.get("context_note"):
                p["notes"].add(r["context_note"])
            forms[key]["task_template_keys"].add(tkey)

    # hand-authored packets (large estate has no published step structure)
    for pkey, spec in (tax.get("manual_packets") or {}).items():
        packets[pkey] = {
            "key": pkey,
            "label": spec.get("label"),
            "matter_type": pkey.rsplit(".", 1)[0],
            "source": "hand_authored",
            "verified": spec.get("verified", False),
            "forms": spec.get("forms", []),
            "conditional_forms": spec.get("conditional", {}),
            "notes": [spec["note"]] if spec.get("note") else [],
        }
        for fn in spec.get("forms", []):
            k = f"{tax['county']}.{fn}"
            if k in forms:
                forms[k]["task_template_keys"].add(pkey)

    # normalize sets -> sorted lists
    for f in forms.values():
        f["matter_types"] = sorted(f["matter_types"])
        f["task_template_keys"] = sorted(f["task_template_keys"])
    packet_list = []
    for p in packets.values():
        p["notes"] = sorted(p["notes"]) if isinstance(p["notes"], set) else p["notes"]
        packet_list.append(p)

    form_list = sorted(forms.values(), key=lambda f: f["form_number"])

    # ---- lookup indices ----------------------------------------------------
    def group(keyfn):
        out = defaultdict(list)
        for f in form_list:
            for k in keyfn(f):
                out[k].append(f["id"])
        return dict(out)

    unclassified = [f["form_number"] for f in form_list
                    if f["filing_stage"] == "unclassified"]
    unroled = [f["form_number"] for f in form_list if f["doc_role"] == "other"]
    unverified_rules = [r["id"] for r in deadlines["rules"]
                        if not r.get("verified")]

    index = {
        "schema_version": tax["schema_version"],
        "corpus_version": version,
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "county": tax["county"],
        "harvest_timestamp": manifest.get("harvested"),
        "counts": {
            "forms": len(form_list),
            "packets": len(packet_list),
            "deadline_rules": len(deadlines["rules"]),
        },
        "warnings": {
            "unclassified_stage": unclassified,
            "unclassified_doc_role": unroled,
            "UNVERIFIED_DEADLINE_RULES": unverified_rules,
            "notice": (
                "Deadline rules marked unverified must not drive any date "
                "surfaced to a user. Attorney verification required."
            ),
        },
        "forms": form_list,
        "packets": sorted(packet_list, key=lambda p: p["key"]),
        "deadline_rules": deadlines["rules"],
        "lookup": {
            "by_matter_type": group(lambda f: f["matter_types"]),
            "by_filing_stage": group(lambda f: [f["filing_stage"]]),
            "by_doc_role": group(lambda f: [f["doc_role"]]),
            "by_deadline_rule": group(lambda f: f["deadline_rule_ids"]),
            "by_task_template": group(lambda f: f["task_template_keys"]),
            "by_form_number": {f["form_number"]: f["id"] for f in form_list},
        },
    }

    out = corpus / "index.json"
    out.write_text(json.dumps(index, indent=2), encoding="utf-8")

    print(f"index.json  v{version}")
    print(f"  forms:   {len(form_list)}")
    print(f"  packets: {len(packet_list)}")
    if unclassified:
        print(f"  ! unclassified stage: {len(unclassified)} -> {unclassified[:8]}")
    if unroled:
        print(f"  ! unclassified doc_role: {len(unroled)} -> {unroled[:8]}")
    print(f"  ! UNVERIFIED deadline rules: {len(unverified_rules)}")
    return index


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="./corpus")
    ap.add_argument("--version", required=True, help="e.g. 2026.08.0")
    args = ap.parse_args()
    build(args.corpus, args.version)

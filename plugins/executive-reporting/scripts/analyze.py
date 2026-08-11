#!/usr/bin/env python3
"""
executive-reporting — Layer 2. raw/*.json -> findings.json.

Audit first, then report. The order is deliberate: a beautiful pack built on a CRM
that cannot support it just publishes the mess faster, and whoever's name is on the
chart owns the mess. So this computes a Reporting Readiness Score before it computes
a single headline, and it refuses to publish any rate it cannot defend.

Three defects it is specifically built to catch, because all three look like
performance and none of them are:

  1. An under-stamped stage. If a later stage holds MORE records than an earlier
     one, conversion into it exceeds 100%. That is a missing stamp, not a funnel.
     We report the counts and withhold the rate.
  2. A swapped stage label. Stage display names are not load-bearing anywhere in
     here — everything routes through the operator-confirmed stage_map.
  3. An unripe cohort. A created-date cohort that is still mostly open produces a
     conversion rate that flatters or damns at random. Suppressed until it ripens.

Offline, stdlib only. Reads and writes local files. Never mutates raw/.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import (  # noqa: E402
    Finding,
    FindingsDoc,
    RunManifest,
    Score,
    SourceEmptyError,
    apply_deltas,
    fill_rate,
    load_plugin_config,
    load_profile,
    median,
    normalize_records,
    parse_dt,
    pct,
)
from lib.crmutil import to_number  # noqa: E402  (not re-exported from the package)

PLUGIN = "executive-reporting"

DEFAULTS = {
    "window_months": 13,
    "conversion_basis": "created_cohort",
    "cohort_ripeness_days": 180,
    "headline_conversions": [["sql", "won"], ["mql", "sql"]],
    "stage_map": {},
    "pipeline_stage": "sql",
    "goals": {},
    "goal_level": "executive",
    "segments": [],
    "expansion_owner": "sales",
    "believed_conversion": None,
    "believed_metrics": {},
    "min_sample": 12,
    "concentration_top_n": 10,
    "amount_field": "Amount",
    "recurring_cadence": "annual",
    "include_filters": False,
}

CANON_ORDER = ["lead", "mql", "sal", "sql", "won", "lost"]


# --------------------------------------------------------------------------- io
def load_raw(raw_dir: Path, name: str, required: bool = True) -> List[Dict[str, Any]]:
    path = raw_dir / f"{name}.json"
    if not path.exists():
        if required:
            raise SourceEmptyError(
                f"{name}.json is missing from {raw_dir}. The run skill must fetch it before "
                f"analyze.py runs — see skills/run/SKILL.md section 2.")
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise SourceEmptyError(f"{name}.json is not a list of records.")
    if required and not records:
        raise SourceEmptyError(
            f"{name}.json returned 0 records. Stopping rather than publishing an empty pack: "
            f"a report that says 'no pipeline' because auth died is worse than a crash. "
            f"Re-run /executive-reporting:setup to re-probe the connector.")
    return normalize_records(records)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_label(key: str) -> str:
    y, m = key.split("-")
    return f"{['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(m)-1]} {y[2:]}"


def add_months(d: date, n: int) -> date:
    y, m = divmod((d.year * 12 + (d.month - 1)) + n, 12)
    return date(y, m + 1, 1)


def money(v: Any) -> str:
    try:
        return "${:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


# ------------------------------------------------------------------- stage logic
def build_stage_lookup(cfg: Dict[str, Any]) -> Dict[str, str]:
    """value -> canonical key. Built ONLY from the operator-confirmed map."""
    lookup: Dict[str, str] = {}
    for canon, values in (cfg.get("stage_map") or {}).items():
        for v in values or []:
            lookup[str(v).strip().lower()] = canon
    return lookup


def canon_of(record: Dict[str, Any], lookup: Dict[str, str]) -> Optional[str]:
    raw = record.get("stage") or record.get("StageName") or record.get("dealstage")
    if raw is None:
        return None
    return lookup.get(str(raw).strip().lower())


# ------------------------------------------------------------------ readiness
def readiness(deals: Sequence[Dict[str, Any]], stage_counts: Dict[str, int],
              cfg: Dict[str, Any], have_goals: bool, unmapped: Dict[str, int],
              entered: Dict[str, int]) -> Dict[str, Any]:
    """
    Scored 0-100. Every component is measurable from raw/, weighted, and printed
    with the sentence that produced it. A score nobody can audit is a vibe.
    """
    rows: List[Dict[str, Any]] = []

    def comp(key: str, label: str, weight: int, value: Optional[float], sentence: str):
        rows.append({"Component": label, "Weight": weight,
                     "Subscore": "—" if value is None else f"{value * 100:.0f}",
                     "What it measures": sentence,
                     "Measured": "no" if value is None else "yes",
                     "_key": key, "_w": weight, "_v": value})

    n = len(deals)
    created_cov = sum(1 for d in deals if d.get("_created")) / n if n else None
    comp("created_date", "Created date present", 20, created_cov,
         "Cohort conversion is impossible without it — this is the gate on the whole method.")

    closed = [d for d in deals if d.get("_canon") in ("won", "lost")]
    close_cov = (sum(1 for d in closed if d.get("_closed")) / len(closed)) if closed else None
    comp("close_date", "Close date on resolved deals", 12, close_cov,
         "Without it a deal cannot be placed in a period, so trend charts silently drop it.")

    amt = cfg.get("amount_field", "Amount")
    amt_cov = (sum(1 for d in deals if to_number(d.get("_amount")) is not None) / n) if n else None
    comp("amount", f"Amount populated ({amt})", 14, amt_cov,
         "Every dollar headline in the pack sums this field.")

    mapped = sum(stage_counts.values())
    total_stage = mapped + sum(unmapped.values())
    stage_cov = (mapped / total_stage) if total_stage else None
    comp("stage_map", "Stage values mapped", 16, stage_cov,
         "Unmapped stage values are invisible to every funnel number.")

    # monotonicity — only observable from ENTERED counts, never from a snapshot
    seq = [k for k in CANON_ORDER[:4] if entered.get(k)]
    if len(seq) < 2:
        mono = None
    else:
        inversions = sum(1 for i in range(1, len(seq)) if entered[seq[i]] > entered[seq[i - 1]])
        mono = max(0.0, 1.0 - inversions / max(1, len(seq) - 1))
    comp("monotonic", "Funnel is monotonic", 18, mono,
         "A stage that more records entered than the stage above it means a missing stamp. "
         "Only measurable when entered-stage counts are supplied.")

    chan_cov = fill_rate(deals, "_channel") / 100.0 if n else None
    comp("channel", "Channel / source populated", 10, chan_cov,
         "Decides whether any channel view in the pack can be published at all.")

    owner_cov = (sum(1 for d in deals if d.get("_owner")) / n) if n else None
    comp("owner", "Owner populated", 5, owner_cov,
         "Needed for the per-rep cut and the concentration check.")

    comp("goals", "Targets configured", 5, 1.0 if have_goals else 0.0,
         "Every number in this pack is supposed to land against a goal.")

    measurable = sum(r["_w"] for r in rows if r["_v"] is not None)
    raw_score = (sum(r["_w"] * r["_v"] for r in rows if r["_v"] is not None) / measurable) if measurable else 0.0
    coverage_mult = measurable / sum(r["_w"] for r in rows)
    score = round(raw_score * coverage_mult * 100)
    band = ("publishable" if score >= 75 else
            "publishable with caveats" if score >= 55 else
            "not publishable — fix the data first")
    for r in rows:
        r.pop("_key", None), r.pop("_w", None), r.pop("_v", None)
    return {
        "score": score, "band": band, "rows": rows,
        "raw_score": round(raw_score * 100), "coverage_multiplier": round(coverage_mult, 2),
        "measurable_weight": measurable,
        "formula": "Each component scored 0-100, weighted, then multiplied by the share of "
                   "weight that could be measured at all.",
        "bands": ["75+ publishable", "55-74 caveats", "<55 fix the data first"],
    }


# -------------------------------------------------------------------- analysis
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Analyze CRM extract into an executive reporting pack")
    ap.add_argument("--run", required=True, help="run directory (findings.json is written here)")
    ap.add_argument("--raw", help="raw/ directory to read from, if not <run>/raw")
    ap.add_argument("--config", help="override plugin config path (testing)")
    ap.add_argument("--profile", help="override profile path (testing)")
    ap.add_argument("--force", action="store_true",
                    help="publish headline numbers even if readiness is below the band")
    args = ap.parse_args(argv)

    run_dir = Path(args.run)
    raw_dir = Path(args.raw) if args.raw else run_dir / "raw"
    if not raw_dir.exists():
        print(f"No raw directory at {raw_dir}.", file=sys.stderr)
        return 2
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.profile:
        profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    else:
        profile = load_profile(required=False) or {}
    if args.config:
        cfg = dict(DEFAULTS)
        cfg.update(json.loads(Path(args.config).read_text(encoding="utf-8")))
    else:
        cfg = load_plugin_config(PLUGIN, defaults=DEFAULTS)

    manifest = RunManifest(PLUGIN, run_dir)

    opportunities = load_raw(raw_dir, "opportunities", required=True)
    manifest.record("opportunities", tool="crm.query", count=len(opportunities), required=True,
                    query="deals created or closed in the rolling window, with stage, amount, "
                          "owner, source, created and close dates",
                    diagnosis="The CRM returned no deals at all. Either the connected identity "
                              "cannot see the Opportunity/Deal object, or the date filter in the "
                              "run skill excluded everything. Re-run /executive-reporting:setup.")
    stage_meta = load_raw(raw_dir, "stage_metadata", required=False)
    manifest.record("stage_metadata", tool="crm.describe", count=len(stage_meta), required=False,
                    query="stage picklist values with labels and sort order")
    accounts = load_raw(raw_dir, "accounts", required=False)
    manifest.record("accounts", tool="crm.query", count=len(accounts), required=False,
                    query="customer accounts with recurring revenue, for concentration")
    goals_raw = load_raw(raw_dir, "goals", required=False)
    manifest.record("goals", tool="crm.query", count=len(goals_raw), required=False,
                    query="targets by period, if held in the CRM")
    funnel_raw = load_raw(raw_dir, "funnel_stages", required=False)
    manifest.record("funnel_stages", tool="crm.query", count=len(funnel_raw), required=False,
                    query="records that ENTERED each stage in the window (not a current snapshot)")

    lookup = build_stage_lookup(cfg)
    if not lookup:
        raise SourceEmptyError(
            "stage_map is empty in your config. This plugin will not guess which of your stage "
            "values is an MQL or an SQL — a wrong guess produces a confident, wrong conversion "
            "rate. Run /executive-reporting:setup, which reads your real picklist and proposes "
            "the map for you to confirm.")

    amount_field = cfg.get("amount_field", "Amount")
    today = date.today()
    window_start = add_months(date(today.year, today.month, 1), -(int(cfg["window_months"]) - 1))

    # ---- normalise deals
    deals: List[Dict[str, Any]] = []
    unmapped: Dict[str, int] = defaultdict(int)
    for r in opportunities:
        canon = canon_of(r, lookup)
        if canon is None:
            raw_stage = r.get("stage") or r.get("StageName") or r.get("dealstage")
            if raw_stage:
                unmapped[str(raw_stage)] += 1
        created = parse_dt(r.get("created_date") or r.get("CreatedDate") or r.get("createdate"))
        closed = parse_dt(r.get("close_date") or r.get("CloseDate") or r.get("closedate"))
        d = dict(r)
        d["_canon"] = canon
        d["_created"] = created.date() if created else None
        d["_closed"] = closed.date() if closed else None
        d["_amount"] = r.get(amount_field) or r.get("amount") or r.get("Amount")
        d["_owner"] = r.get("owner_name") or r.get("OwnerName") or r.get("owner") or None
        d["_channel"] = (r.get("channel") or r.get("lead_source") or r.get("LeadSource")
                         or r.get("hs_analytics_source") or None)
        d["_segment"] = r.get("segment") or r.get("Segment") or None
        d["_account"] = r.get("account_name") or r.get("AccountName") or r.get("account") or None
        d["_type"] = (r.get("type") or r.get("Type") or "new").strip().lower()
        deals.append(d)

    stage_counts: Dict[str, int] = defaultdict(int)
    for d in deals:
        if d["_canon"]:
            stage_counts[d["_canon"]] += 1

    entered = {}
    for r in funnel_raw:
        key = str(r.get("stage") or r.get("Stage") or "").strip().lower()
        canon = key if key in CANON_ORDER else lookup.get(key)
        n = to_number(r.get("entered") or r.get("count") or r.get("n"))
        if canon and n is not None:
            entered[canon] = entered.get(canon, 0) + int(n)

    have_goals = bool(cfg.get("goals"))
    ready = readiness(deals, stage_counts, cfg, have_goals, unmapped, entered)

    findings: List[Finding] = []
    sections: Dict[str, Any] = {}

    # ---- funnel ------------------------------------------------------------
    # Two very different data shapes arrive here and confusing them produces
    # nonsense:
    #   ENTERED counts  — how many records entered each stage in the period.
    #                     Monotonic by nature; an inversion is a missing stamp.
    #   SNAPSHOT counts — each record's CURRENT stage. Not monotonic by nature:
    #                     won/lost accumulate while open stages drain, so
    #                     comparing wins to a snapshot of SQL is meaningless.
    # If entered counts are supplied we use them and run the inversion detector.
    # Otherwise we derive a cumulative "reached" funnel from the snapshot and
    # say so, because under-stamping simply is not observable from a snapshot.
    funnel_rows: List[Dict[str, Any]] = []
    withheld: List[str] = []
    derived = not entered
    if entered:
        counts = entered
        basis_note = ("Entered-stage counts supplied by the source — how many records entered "
                      "each stage in the window. A rate above 100% here is a missing stamp.")
    else:
        # cumulative: a resolved deal necessarily passed through every prior stage
        resolved_all = stage_counts.get("won", 0) + stage_counts.get("lost", 0)
        # only stages the operator actually mapped — an unmapped stage would
        # otherwise emit a phantom row identical to the one below it
        mapped_canons = {c for c, v in (cfg.get("stage_map") or {}).items() if v}
        counts, running = {}, resolved_all
        for key in reversed(CANON_ORDER[:4]):        # sql, sal, mql, lead
            if key not in mapped_canons:
                continue
            running += stage_counts.get(key, 0)
            counts[key] = running
        counts["won"] = stage_counts.get("won", 0)
        counts = {k: v for k, v in counts.items() if v}
        basis_note = ("Derived from a current-stage snapshot by assuming linear progression — a "
                      "resolved deal is counted as having reached every earlier stage. Under-"
                      "stamping cannot be detected from a snapshot; supply entered-stage counts "
                      "or stage history if you need that check.")

    prev_key, prev_n = None, None
    for key in CANON_ORDER[:4] + ["won"]:
        n = counts.get(key, 0)
        if not n:
            continue
        row = {"Stage": key.upper(), "Count": n, "Conversion from prior": "—", "Usable?": "—"}
        if prev_n:
            rate = n / prev_n
            usable = rate <= 1.0
            row["Conversion from prior"] = f"{rate * 100:.1f}%"
            row["Usable?"] = "yes" if usable else "NO — exceeds 100%"
            if not usable:
                withheld.append(f"{prev_key.upper()}\u2192{key.upper()}")
                findings.append(Finding(
                    id=f"understamped-{prev_key}",
                    severity="critical",
                    title=f"{prev_key.upper()} is under-stamped — {prev_key.upper()}"
                          f"\u2192{key.upper()} reads {rate * 100:.0f}%",
                    what=f"{n:,} records entered {key.upper()} while only {prev_n:,} were ever "
                         f"stamped {prev_key.upper()}.",
                    evidence={"count": n, "prior_count": prev_n, "rate": round(rate, 4),
                              "query": "entered-stage counts by mapped stage over the window"},
                    why_it_matters="A conversion rate above 100% is arithmetically impossible on a "
                                   "real funnel, so this is a missing stamp, not performance. Any "
                                   "goal set against this rate is unmeasurable, and any board slide "
                                   "quoting it is wrong.",
                    recommended_fix=f"Make {prev_key.upper()} an automatic stamp on stage entry "
                                    f"rather than a manual field. Until then this pack reports the "
                                    f"counts and withholds the rate.",
                    effort="medium", owner_hint="RevOps"))
        funnel_rows.append(row)
        prev_key, prev_n = key, n
    sections["funnel"] = {"rows": funnel_rows, "withheld": withheld,
                          "derived": derived, "basis": basis_note}

    # ---- cohort conversion -------------------------------------------------
    cohorts: Dict[str, Dict[str, int]] = defaultdict(lambda: {"won": 0, "lost": 0, "open": 0})
    for d in deals:
        if not d["_created"] or d["_created"] < window_start:
            continue
        mk = month_key(d["_created"])
        if d["_canon"] == "won":
            cohorts[mk]["won"] += 1
        elif d["_canon"] == "lost":
            cohorts[mk]["lost"] += 1
        else:
            cohorts[mk]["open"] += 1

    ripeness = int(cfg["cohort_ripeness_days"])
    cohort_rows, ripe_won, ripe_resolved, suppressed = [], 0, 0, 0
    for mk in sorted(cohorts):
        c = cohorts[mk]
        resolved = c["won"] + c["lost"]
        total = resolved + c["open"]
        y, m = (int(x) for x in mk.split("-"))
        age = (today - date(y, m, 1)).days
        is_ripe = age >= ripeness
        rate = (c["won"] / resolved) if resolved else None
        if is_ripe and resolved:
            ripe_won += c["won"]
            ripe_resolved += resolved
        if not is_ripe:
            suppressed += 1
        cohort_rows.append({
            "Cohort (created)": month_label(mk), "Created": total, "Won": c["won"],
            "Lost": c["lost"], "Still open": c["open"],
            "Conversion": "suppressed — not ripe" if not is_ripe else (
                f"{rate * 100:.1f}%" if rate is not None else "—"),
            "Ripe?": "yes" if is_ripe else f"no ({ripeness - age}d to go)",
        })
    blended = (ripe_won / ripe_resolved) if ripe_resolved else None
    sections["cohorts"] = {
        "rows": cohort_rows, "blended": round(blended, 4) if blended is not None else None,
        "basis": cfg["conversion_basis"], "ripeness_days": ripeness,
        "suppressed": suppressed, "resolved": ripe_resolved,
        "definition": "Deals grouped by the date they were CREATED. Conversion = won ÷ (won + lost). "
                      "Deals still open are excluded until they resolve — they are neither a win nor "
                      "a loss yet, and counting them as losses is what makes a healthy quarter look "
                      "broken.",
    }
    if blended is not None and ripe_resolved < int(cfg["min_sample"]):
        findings.append(Finding(
            id="low-sample-conversion", severity="medium",
            title=f"Blended conversion rests on only {ripe_resolved} resolved deals",
            what=f"{ripe_resolved} resolved deals across ripe cohorts is below the configured "
                 f"minimum of {cfg['min_sample']}.",
            evidence={"count": ripe_resolved, "rate": round(blended, 4), "query": "ripe created-date cohorts"},
            why_it_matters="A rate on a sample this small moves several points on one deal. Report "
                           "it with the sample size attached or not at all.",
            recommended_fix="Widen the window or wait for cohorts to ripen before setting a target "
                            "against this number.",
            effort="quick", owner_hint="RevOps"))

    # ---- monthly spine -----------------------------------------------------
    months: List[str] = []
    cur = window_start
    while cur <= date(today.year, today.month, 1):
        months.append(month_key(cur))
        cur = add_months(cur, 1)

    booked: Dict[str, float] = defaultdict(float)
    logos: Dict[str, int] = defaultdict(int)
    created_pipe: Dict[str, float] = defaultdict(float)
    pipeline_stage = cfg.get("pipeline_stage", "sql")
    for d in deals:
        amt = to_number(d["_amount"]) or 0.0
        if d["_canon"] == "won" and d["_closed"]:
            mk = month_key(d["_closed"])
            if mk in booked or mk in months:
                booked[mk] += amt
                logos[mk] += 1
        if d["_created"] and d["_canon"] in (pipeline_stage, "won", "lost"):
            mk = month_key(d["_created"])
            if mk in months:
                created_pipe[mk] += amt

    month_rows = [{"Month": month_label(m), "Bookings": round(booked.get(m, 0.0), 2),
                   "New logos": logos.get(m, 0),
                   "Created pipeline": round(created_pipe.get(m, 0.0), 2)} for m in months]
    sections["months"] = {"rows": month_rows, "window_months": len(months),
                          "note": f"Rolling {len(months)} months so the current month sits beside "
                                  f"the same month last year."}

    zero_run = 0
    for m in reversed(months[:-0] if len(months) else months):
        if booked.get(m, 0.0) == 0:
            zero_run += 1
        else:
            break
    if zero_run >= 2:
        findings.append(Finding(
            id="zero-booking-months", severity="critical",
            title=f"{zero_run} consecutive months with zero bookings",
            what=f"The most recent {zero_run} months in the window closed nothing.",
            evidence={"count": zero_run,
                      "sample_ids": [month_label(m) for m in months[-zero_run:]],
                      "query": "sum of amount by close month where stage is won"},
            why_it_matters="Two or more consecutive empty months is a trend, not a slow patch. "
                           "Check created pipeline in the preceding months — bookings lag it, so "
                           "the cause is usually already visible upstream.",
            recommended_fix="Confirm these months are genuinely empty rather than unbooked, then "
                            "work backwards to the pipeline-creation month that produced them.",
            effort="quick", owner_hint="Sales leadership"))

    # ---- open pipeline & coverage -----------------------------------------
    open_deals = [d for d in deals if d["_canon"] not in ("won", "lost") and d["_canon"]]
    open_amount = sum(to_number(d["_amount"]) or 0.0 for d in open_deals)
    goals = (cfg.get("goals") or {})
    goal_level = cfg.get("goal_level", "executive")

    def goal_for(metric: str) -> Optional[float]:
        g = goals.get(metric)
        if isinstance(g, dict):
            for v in g.values():
                n = to_number(v)
                if n is not None:
                    return n
            return None
        return to_number(g)

    total_booked = sum(booked.values())
    total_created = sum(created_pipe.values())
    plan_rows = []
    for metric, actual in (("bookings", total_booked), ("created_pipeline", total_created),
                           ("open_pipeline", open_amount)):
        g = goal_for(metric)
        plan_rows.append({
            "Metric": metric.replace("_", " ").title(),
            "Actual": money(actual),
            "Plan": money(g) if g else "not configured",
            "Attainment": f"{actual / g * 100:.1f}%" if g else "—",
            "Gap": money(actual - g) if g else "—",
        })
    sections["plan"] = {"rows": plan_rows, "level": goal_level, "configured": have_goals}
    if not have_goals:
        findings.append(Finding(
            id="no-goals", severity="high",
            title="No targets configured — every headline is an absolute number",
            what="goals is empty in the plugin config, so nothing in the pack lands against a plan.",
            evidence={"count": 0, "query": "config.goals"},
            why_it_matters="An executive cannot act on a number without a target beside it. "
                           "'$240k of bookings' is trivia; '$240k against $290k' is a decision.",
            recommended_fix="Capture board, executive and field targets in config.goals — they are "
                            "usually three different numbers — and set goal_level to the one the "
                            "headline should compare against.",
            effort="quick", owner_hint="Finance / RevOps"))

    # ---- channel matrix ----------------------------------------------------
    by_channel: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"book": 0.0, "logos": 0, "pipe_ct": 0, "resolved": 0})
    for d in deals:
        ch = d["_channel"] or "Unallocated"
        amt = to_number(d["_amount"]) or 0.0
        if d["_canon"] == "won":
            by_channel[ch]["book"] += amt
            by_channel[ch]["logos"] += 1
            by_channel[ch]["resolved"] += 1
        elif d["_canon"] == "lost":
            by_channel[ch]["resolved"] += 1
        if d["_canon"] in (pipeline_stage, "won", "lost"):
            by_channel[ch]["pipe_ct"] += 1

    live = {c: v for c, v in by_channel.items() if v["resolved"] >= 1}
    mean_book = (sum(v["book"] for v in live.values()) / len(live)) if live else 0.0
    chan_rows = []
    for ch, v in sorted(by_channel.items(), key=lambda kv: -kv[1]["book"]):
        conv = (v["logos"] / v["resolved"]) if v["resolved"] else None
        chan_rows.append({
            "Channel": ch, "Bookings": money(v["book"]), "Logos": int(v["logos"]),
            "Resolved deals": int(v["resolved"]),
            "Conversion": f"{conv * 100:.0f}%" if conv is not None else "—",
            "Low sample?": "yes" if v["resolved"] < int(cfg["min_sample"]) else "",
        })
    sections["channels"] = {
        "rows": chan_rows,
        "blended_conversion": round(blended, 4) if blended is not None else None,
        "mean_bookings": round(mean_book, 2),
        "divider_note": "Quadrant dividers are this portfolio's own blended conversion rate and its "
                        "mean bookings per channel — never a fixed 50%. A fixed split routinely puts "
                        "the best channel in the 'low quality' quadrant.",
    }
    unalloc = by_channel.get("Unallocated", {"book": 0.0})
    if total_booked and unalloc["book"] / total_booked > 0.2:
        findings.append(Finding(
            id="unattributed-bookings", severity="high",
            title=f"{unalloc['book'] / total_booked * 100:.0f}% of bookings carry no channel",
            what=f"{money(unalloc['book'])} of {money(total_booked)} closed with an empty source field.",
            evidence={"count": int(unalloc.get("logos", 0)), "amount": unalloc["book"],
                      "query": "sum of won amount grouped by channel where channel is null"},
            why_it_matters="Every channel view in the pack is wrong by this much, and it is always "
                           "the largest 'channel' on the chart — which reads as a finding when it is "
                           "actually a gap.",
            recommended_fix="Make source required at creation and backfill the closed-won set; a "
                            "channel view is not publishable until this is under ~10%.",
            effort="medium", owner_hint="Marketing ops"))

    # ---- rep concentration -------------------------------------------------
    by_rep: Dict[str, Dict[str, float]] = defaultdict(lambda: {"book": 0.0, "logos": 0, "resolved": 0})
    for d in deals:
        if not d["_owner"]:
            continue
        if d["_canon"] == "won":
            by_rep[d["_owner"]]["book"] += to_number(d["_amount"]) or 0.0
            by_rep[d["_owner"]]["logos"] += 1
            by_rep[d["_owner"]]["resolved"] += 1
        elif d["_canon"] == "lost":
            by_rep[d["_owner"]]["resolved"] += 1
    rep_rows = []
    for name, v in sorted(by_rep.items(), key=lambda kv: -kv[1]["book"]):
        conv = (v["logos"] / v["resolved"]) if v["resolved"] else None
        rep_rows.append({"Owner": name, "Bookings": money(v["book"]), "Logos": int(v["logos"]),
                         "Resolved": int(v["resolved"]),
                         "Conversion": f"{conv * 100:.0f}%" if conv is not None else "—",
                         "Share of bookings": f"{v['book'] / total_booked * 100:.0f}%" if total_booked else "—"})
    sections["reps"] = {"rows": rep_rows}
    if rep_rows and total_booked:
        top = max(by_rep.values(), key=lambda v: v["book"])
        share = top["book"] / total_booked
        if share > 0.6 and len(by_rep) > 1:
            top_name = [k for k, v in by_rep.items() if v is top][0]
            findings.append(Finding(
                id="rep-concentration", severity="high",
                title=f"One owner closed {share * 100:.0f}% of bookings",
                what=f"{top_name} accounts for {money(top['book'])} of {money(total_booked)}.",
                evidence={"count": int(top["logos"]), "share": round(share, 3),
                          "query": "sum of won amount grouped by owner"},
                why_it_matters="Concentration this high is a single point of failure, and it also "
                               "means the team-level conversion rate is really one person's rate.",
                recommended_fix="Report per-rep alongside the headline so the concentration is "
                                "visible, and check whether it is recent or structural before "
                                "treating it as a coverage problem.",
                effort="quick", owner_hint="Sales leadership"))

    # ---- revenue concentration --------------------------------------------
    if accounts:
        rows = []
        for a in accounts:
            rev = to_number(a.get("recurring_revenue") or a.get("mrr") or a.get("arr"))
            if rev is None:
                continue
            rows.append((a.get("name") or a.get("Name") or "(unnamed)", rev))
        rows.sort(key=lambda r: -r[1])
        book_total = sum(r[1] for r in rows)
        top_n = int(cfg["concentration_top_n"])
        top_sum = sum(r[1] for r in rows[:top_n])
        negatives = [r for r in rows if r[1] < 0]
        sections["concentration"] = {
            "rows": [{"#": i + 1, "Account": n, "Recurring revenue": money(v),
                      "% of book": f"{v / book_total * 100:.1f}%" if book_total else "—"}
                     for i, (n, v) in enumerate(rows[:max(top_n, 12)])],
            "total": round(book_total, 2), "top_n": top_n,
            "top_share": round(top_sum / book_total, 4) if book_total else None,
            "accounts": len(rows), "negatives": len(negatives),
            "cadence": cfg.get("recurring_cadence", "annual"),
        }
        if negatives:
            findings.append(Finding(
                id="negative-balances", severity="medium",
                title=f"{len(negatives)} accounts carry a negative recurring-revenue balance",
                what="Churn or an adjustment was credited beyond what was ever booked to the account.",
                evidence={"count": len(negatives),
                          "sample_ids": [n for n, _ in negatives[:5]],
                          "query": "accounts where recurring revenue < 0"},
                why_it_matters="Excluding them makes the per-account list disagree with the balance "
                               "sheet; including them makes it reconcile but looks like an error to "
                               "anyone reading the account list. Either way somebody asks.",
                recommended_fix="Correct the underlying adjustments at source so the account list "
                                "and the total agree without a footnote.",
                effort="medium", owner_hint="Finance / RevOps"))

    # ---- reconciliation ----------------------------------------------------
    recon_rows = []
    believed_conv = cfg.get("believed_conversion")
    if believed_conv is not None and blended is not None:
        delta = blended - float(believed_conv)
        recon_rows.append({
            "Number": "Conversion rate",
            "They quote": f"{float(believed_conv) * 100:.1f}%",
            "This pack measures": f"{blended * 100:.1f}%",
            "Delta": f"{delta * 100:+.1f} pts",
            "Why it differs": "Cohorted by created date with open deals excluded; a closed-period "
                              "definition usually reads higher.",
        })
    for metric, quoted in (cfg.get("believed_metrics") or {}).items():
        actual = {"bookings_ytd": total_booked, "bookings": total_booked,
                  "created_pipeline": total_created, "open_pipeline": open_amount}.get(metric)
        if actual is None:
            continue
        q = to_number(quoted)
        if q is None:
            continue
        recon_rows.append({"Number": metric.replace("_", " ").title(),
                           "They quote": money(q), "This pack measures": money(actual),
                           "Delta": money(actual - q),
                           "Why it differs": "Check the window and whether post-closing adjustments "
                                             "are included."})
    sections["reconciliation"] = {
        "rows": recon_rows,
        "note": "Do this before the pack goes out, never in the meeting. A number that moves "
                "without warning costs more trust than a number that was simply wrong.",
    }
    if recon_rows:
        big = [r for r in recon_rows if r["Number"] == "Conversion rate"]
        if big and abs(blended - float(believed_conv)) > 0.05:
            findings.append(Finding(
                id="conversion-gap", severity="high",
                title=f"Measured conversion differs from the quoted rate by "
                      f"{abs(blended - float(believed_conv)) * 100:.0f} points",
                what=f"Leadership quotes {float(believed_conv) * 100:.0f}%; the cohort method "
                     f"measures {blended * 100:.0f}%.",
                evidence={"measured": round(blended, 4), "believed": float(believed_conv),
                          "count": ripe_resolved, "query": "ripe created-date cohorts"},
                why_it_matters="This gap will surface in the first executive review. Surfacing it "
                               "yourself, with the definition attached, is the difference between a "
                               "methodology conversation and a credibility problem.",
                recommended_fix="Walk the sponsor through both definitions privately before the "
                                "pack is circulated, and agree which one becomes canonical.",
                effort="quick", owner_hint="RevOps"))

    if unmapped:
        top_unmapped = sorted(unmapped.items(), key=lambda kv: -kv[1])[:8]
        findings.append(Finding(
            id="unmapped-stages", severity="high",
            title=f"{len(unmapped)} stage values are not mapped",
            what=f"{sum(unmapped.values()):,} records sit in stage values with no canonical mapping.",
            evidence={"count": sum(unmapped.values()),
                      "sample_ids": [f"{k} ({v})" for k, v in top_unmapped],
                      "query": "distinct stage values not present in config.stage_map"},
            why_it_matters="Unmapped records are invisible to every funnel number in this pack, so "
                           "the funnel understates reality by exactly this much.",
            recommended_fix="Add each value to config.stage_map under the canonical stage it "
                            "represents, or confirm it is a dead stage that should be excluded.",
            effort="quick", owner_hint="RevOps"))

    # ---- assemble ----------------------------------------------------------
    publishable = ready["score"] >= 55 or args.force
    sections["readiness"] = ready
    sections["scope"] = {
        "rows": [
            {"Setting": "Window", "Value": f"rolling {len(months)} months "
                                           f"({month_label(months[0])} → {month_label(months[-1])})"},
            {"Setting": "Conversion basis", "Value": cfg["conversion_basis"]},
            {"Setting": "Cohort ripeness", "Value": f"{ripeness} days"},
            {"Setting": "Pipeline stage", "Value": pipeline_stage.upper()},
            {"Setting": "Amount field", "Value": amount_field},
            {"Setting": "Recurring cadence", "Value": cfg.get("recurring_cadence")},
            {"Setting": "Expansion owned by", "Value": cfg.get("expansion_owner")},
            {"Setting": "Goal level", "Value": goal_level if have_goals else "none configured"},
            {"Setting": "Viewer filters", "Value": "date only" if not cfg.get("include_filters")
                                                   else "date + configured filters"},
            {"Setting": "Deals in scope", "Value": f"{len(deals):,}"},
            {"Setting": "Publishable", "Value": "yes" if publishable else
                                                "no — readiness below 55, headlines withheld"},
        ]
    }

    scores = [
        Score(key="reporting_readiness", label="Reporting Readiness", value=ready["score"],
              unit="score_0_100"),
    ]
    if blended is not None and publishable:
        scores.append(Score(key="blended_conversion", label="Blended conversion (cohort)",
                            value=round(blended * 100, 1), unit="percent"))
    scores.append(Score(key="bookings_window", label=f"Bookings · rolling {len(months)}mo",
                        value=round(total_booked, 2), unit="currency"))
    scores.append(Score(key="open_pipeline", label="Open pipeline", value=round(open_amount, 2),
                        unit="currency"))

    if not publishable:
        findings.insert(0, Finding(
            id="not-publishable", severity="critical",
            title=f"Reporting readiness is {ready['score']}/100 — headlines withheld",
            what="The CRM cannot currently support the pack's headline numbers.",
            evidence={"count": ready["score"], "query": "readiness components, see the score table"},
            why_it_matters="Publishing on top of this would make the reporting look authoritative "
                           "while being wrong, and whoever's name is on the chart owns it. This is a "
                           "data-infrastructure project before it is a reporting project.",
            recommended_fix="Fix the critical and high findings below, re-run, and the headlines "
                            "publish themselves. To override deliberately, re-run with --force.",
            effort="project", owner_hint="RevOps"))

    fdoc = FindingsDoc(
        plugin=PLUGIN,
        window={"start": window_start.isoformat(), "end": today.isoformat()},
        scores=scores,
        sections=sections,
        org_name=str(profile.get("org_name") or ""),
    )
    for f in findings:                      # add() validates each one; a finding the
        fdoc.add(f)                         # customer cannot verify never ships
    doc = apply_deltas(fdoc.to_dict(), PLUGIN)

    manifest.finalize()
    out = run_dir / "findings.json"
    out.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    print(f"readiness {ready['score']}/100 ({ready['band']}) · {len(findings)} findings · "
          f"{len(deals):,} deals")
    if withheld:
        print(f"withheld conversion rates: {', '.join(withheld)} (exceed 100% — under-stamped stage)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SourceEmptyError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        raise SystemExit(3)

#!/usr/bin/env python3
"""
taxonomy.py — normalisation, conservative clustering, and canonical-mapping
proposals for CRM source / channel values.

Python 3.9+, standard library only. No network. No CRM knowledge beyond string
shape: this module never decides anything about a customer's business, it only
says "these two strings look like the same channel, and here is exactly why".

THE POSTURE THAT MATTERS
------------------------
Every cluster this module emits is a CANDIDATE FOR HUMAN CONFIRMATION. Nothing
here asserts a merge, nothing here rewrites a value, and every cluster carries
(a) the record count behind each member so a human can judge the blast radius,
(b) the tier of evidence that linked them, and (c) a sentence naming the rule
that fired. If we cannot explain why two values were linked, we do not link them.

Auto-merging source values is how a marketing team loses a year of history in an
afternoon. We propose; they decide.

THE FOUR LINK TIERS, weakest first
----------------------------------
  subset   'Referral' is a strict one-token subset of 'Customer Referral'.
           Could be the same channel at two levels of detail, could be two
           deliberately distinct channels. Confidence: low. Always.
  synonym  Both values map to the same entry in the built-in channel lexicon
           ('PPC', 'SEM', 'Google Ads' -> paid_search). Confidence: medium.
  similar  The normalised strings match at >= the similarity threshold
           (difflib SequenceMatcher ratio, or identical token sets in a
           different order). Confidence: high at >= 0.93, medium below.
  exact    The values reduce to the same normalisation key. 'Webinar',
           'webinar', 'Webinars' and 'Web Inar' all key to 'webinar'.
           Confidence: high.

A cluster is labelled with its WEAKEST link, never its strongest.

THE GUARD THAT KEEPS THIS HONEST
-------------------------------
A SEMANTIC link (synonym or subset) is never allowed to put two values that are
both already in the customer's intended taxonomy into one cluster — not even
transitively. If they deliberately blessed both 'Referral' and 'Partner', we do
not get to decide that 'Partner Referral' proves they are the same thing.

TYPOGRAPHIC links (exact or similar) are exempt, because when two spellings of
the same string are BOTH sitting in the picklist, that is the single most
damning thing this module can find, and hiding it would be cowardice.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- normalisation

_PUNCT = re.compile(r"[^a-z0-9]+")

# Dropped only when a value has 3+ tokens, so 'N/A' -> 'n a' survives intact.
_NOISE_TOKENS = {"the", "a", "an", "of", "via", "from", "our", "and"}

# Words a naive trailing-'s' rule would mangle. 'ads' is deliberately absent:
# we WANT 'Google Ads' and 'Google Ad' to collide.
_NEVER_SINGULARIZE = {
    "sales", "news", "sms", "aws", "gas", "press", "status", "bonus", "plus",
    "cross", "class", "business", "process", "series", "analysis",
}


def _singular(token: str) -> str:
    if token in _NEVER_SINGULARIZE:
        return token
    if len(token) >= 3 and token.endswith("s") and not token.endswith(("ss", "us", "is", "as")):
        return token[:-1]
    return token


def normalize(value: Any) -> str:
    """
    Lowercase, punctuation-to-space, de-plural, collapse. Returns a SPACED key.

        'Web Inar'   -> 'web inar'
        'Webinars'   -> 'webinar'
        'Trade-Show' -> 'trade show'
        'N/A'        -> 'n a'
        '---'        -> ''            (empty == no usable value)
    """
    if value is None:
        return ""
    text = str(value).strip().lower().replace("&", " and ")
    text = _PUNCT.sub(" ", text)
    toks = [t for t in text.split() if t]
    if len(toks) >= 3:
        toks = [t for t in toks if t not in _NOISE_TOKENS] or toks
    return " ".join(_singular(t) for t in toks)


def key(value: Any) -> str:
    """
    The spaceless collision key. This is what makes 'Web Inar' == 'Webinar'
    and 'Trade Show' == 'Tradeshow' without any fuzzy matching at all.
    """
    return normalize(value).replace(" ", "")


def tokens(value: Any) -> frozenset:
    return frozenset(normalize(value).split())


# --------------------------------------------------------------------------- placeholders

# Values that occupy the field without attributing anything. 'Other' in a
# picklist is not a channel; it is the absence of a channel wearing a hat.
DEFAULT_PLACEHOLDERS: Tuple[str, ...] = (
    "other", "unknown", "n/a", "na", "none", "null", "tbd", "test", "misc",
    "miscellaneous", "not applicable", "not specified", "unspecified",
    "no source", "undefined", "default", "blank", "legacy", "imported",
    "data import", "migration", "conversion", "unattributed", "not set",
)


def placeholder_keys(extra: Iterable[str] = ()) -> frozenset:
    return frozenset(key(v) for v in list(DEFAULT_PLACEHOLDERS) + list(extra or []) if key(v))


def is_blank_value(value: Any) -> bool:
    """No usable characters at all: None, '', '   ', '-', '---', '?'."""
    return normalize(value) == ""


def is_placeholder(value: Any, keys: Optional[frozenset] = None) -> bool:
    """True for 'Other' / 'Unknown' / 'N/A' style values. Blanks are NOT placeholders."""
    k = key(value)
    if not k:
        return False
    return k in (keys if keys is not None else placeholder_keys())


def is_attributed(value: Any, keys: Optional[frozenset] = None) -> bool:
    """The only definition of 'this record actually told us where it came from'."""
    return (not is_blank_value(value)) and (not is_placeholder(value, keys))


# --------------------------------------------------------------------------- channel lexicon

# Deliberately conservative. Bare vendor names that are ambiguous across paid and
# organic ('google', 'facebook' on its own) are NOT listed — an unresolvable value
# is excluded from agreement maths rather than guessed at.
SYNONYM_GROUPS: Dict[str, List[str]] = {
    "paid_search": [
        "paid search", "ppc", "sem", "cpc", "adwords", "google ads", "google adwords",
        "bing ads", "microsoft ads", "search ads", "paid search ads", "sea",
    ],
    "paid_social": [
        "paid social", "facebook ads", "fb ads", "meta ads", "linkedin ads", "li ads",
        "social ads", "instagram ads", "twitter ads", "x ads", "paidsocial", "social paid",
    ],
    "organic_search": ["organic search", "seo", "google organic", "natural search", "organic"],
    "organic_social": [
        "organic social", "social", "social media", "linkedin", "twitter", "facebook",
        "instagram", "youtube", "reddit",
    ],
    "email": [
        "email", "email campaign", "email marketing", "newsletter", "nurture", "drip",
        "email blast", "marketing email",
    ],
    "webinar": ["webinar", "webcast", "virtual event", "online event"],
    "event": [
        "event", "conference", "trade show", "field event", "booth", "roadshow",
        "in person event", "summit", "expo",
    ],
    "referral": ["referral", "word of mouth", "wom", "referred", "referrals"],
    "partner": ["partner", "channel", "reseller", "var", "alliance", "si", "affiliate", "isv"],
    "outbound": [
        "outbound", "cold call", "cold email", "cold outreach", "sdr", "bdr",
        "prospecting", "sales generated", "outbound prospecting", "sales outreach",
    ],
    "content": [
        "content syndication", "syndication", "ebook", "whitepaper", "white paper",
        "gated content", "content download", "content",
    ],
    "website": ["website", "web", "web form", "inbound web", "direct traffic", "direct", "web site"],
    "review_site": [
        "g2", "g2 crowd", "capterra", "trustradius", "review site", "gartner peer insights",
        "software advice", "getapp",
    ],
    "paid_other": ["display", "retargeting", "remarketing", "programmatic", "sponsorship", "paid media"],
    "community": ["community", "slack community", "forum", "user group", "meetup"],
    "podcast": ["podcast", "podcast ad", "podcast sponsorship"],
    "pr": ["pr", "press", "media", "analyst", "earned media"],
}

GROUP_LABELS: Dict[str, str] = {
    "paid_search": "Paid Search",
    "paid_social": "Paid Social",
    "organic_search": "Organic Search",
    "organic_social": "Organic Social",
    "email": "Email",
    "webinar": "Webinar",
    "event": "Event",
    "referral": "Referral",
    "partner": "Partner",
    "outbound": "Outbound",
    "content": "Content Syndication",
    "website": "Website",
    "review_site": "Review Site",
    "paid_other": "Display / Paid Other",
    "community": "Community",
    "podcast": "Podcast",
    "pr": "PR / Analyst",
}


def build_lexicon(extra_groups: Optional[Dict[str, List[str]]] = None) -> Dict[str, str]:
    """
    key -> group name. Built-ins load first and win ties, so a customer's extra
    group can add terms but cannot silently re-point a built-in one.
    """
    lex: Dict[str, str] = {}
    for source in (SYNONYM_GROUPS, extra_groups or {}):
        for group, terms in source.items():
            for term in terms:
                k = key(term)
                if k and k not in lex:
                    lex[k] = group
    return lex


def lexicon_conflicts(extra_groups: Optional[Dict[str, List[str]]] = None) -> List[str]:
    """Terms claimed by more than one group. Used by the self-test; should be empty."""
    seen: Dict[str, str] = {}
    dupes: List[str] = []
    for source in (SYNONYM_GROUPS, extra_groups or {}):
        for group, terms in source.items():
            for term in terms:
                k = key(term)
                if not k:
                    continue
                if k in seen and seen[k] != group:
                    dupes.append(f"{term!r}: {seen[k]} vs {group}")
                seen.setdefault(k, group)
    return dupes


def channel_group(value: Any, lexicon: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Resolve a single value to a channel group, or None if it is ambiguous."""
    lex = lexicon if lexicon is not None else build_lexicon()
    k = key(value)
    return lex.get(k) if k else None


def resolve_channel(parts: Sequence[Any], lexicon: Optional[Dict[str, str]] = None) -> Optional[str]:
    """
    Resolve a channel from several fragments, most specific first.

        resolve_channel(['cpc', 'google'])                -> 'paid_search'
        resolve_channel(['linkedin', 'paid social'])       -> 'paid_social'
        resolve_channel(['google'])                        -> None  (ambiguous)

    Tries the whole fragment first, then its individual tokens. Order matters:
    pass utm_medium before utm_source, because medium describes the channel and
    source only names the vendor.
    """
    lex = lexicon if lexicon is not None else build_lexicon()
    fragments = [p for p in parts if not is_blank_value(p)]
    for part in fragments:
        hit = lex.get(key(part))
        if hit:
            return hit
    # Combined string, e.g. utm_source='google' + utm_medium='ads' -> 'google ads'.
    if len(fragments) > 1:
        hit = lex.get(key(" ".join(str(p) for p in fragments)))
        if hit:
            return hit
    for part in fragments:
        for token in normalize(part).split():
            hit = lex.get(token)
            if hit:
                return hit
    return None


# --------------------------------------------------------------------------- similarity


def similarity(a_norm: str, b_norm: str) -> float:
    """
    0.0-1.0. The max of character-level ratio and token-set overlap, so
    'Search Paid' and 'Paid Search' score 1.0 while 'Outbound' and 'Inbound'
    score 0.67 and stay apart.
    """
    if not a_norm or not b_norm:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a_norm, b_norm).ratio()
    ta, tb = set(a_norm.split()), set(b_norm.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    return max(ratio, jaccard)


# --------------------------------------------------------------------------- clustering

_TIER_ORDER = ("subset", "synonym", "similar", "exact")  # weakest first
_TIER_CONFIDENCE = {"subset": "low", "synonym": "medium", "similar": "medium", "exact": "high"}


@dataclass
class ClusterMember:
    value: str
    records: int
    in_taxonomy: bool
    normalized: str


@dataclass
class Cluster:
    id: str
    tier: str
    confidence: str
    proposed_canonical: str
    reason: str
    members: List[ClusterMember] = field(default_factory=list)
    total_records: int = 0
    non_canonical_records: int = 0
    requires_human_confirmation: bool = True

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["members"] = [asdict(m) for m in self.members]
        return payload


class _DSU:
    def __init__(self, items: Iterable[str]):
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def cluster_values(
    counts: Dict[str, int],
    *,
    intended_taxonomy: Sequence[str] = (),
    similarity_threshold: float = 0.88,
    subset_min_records: int = 10,
    extra_groups: Optional[Dict[str, List[str]]] = None,
    placeholder_values: Iterable[str] = (),
    include_placeholder_cluster: bool = True,
) -> List[Cluster]:
    """
    counts: raw source value -> record count. Returns proposed clusters, largest
    blast radius first. Values that cluster with nothing are simply absent.
    """
    lex = build_lexicon(extra_groups)
    ph_keys = placeholder_keys(placeholder_values)
    # Exact membership: 'Webinars' is a typo of a blessed value, not a blessed value.
    tax_exact = {str(t).strip() for t in intended_taxonomy if str(t).strip()}
    tax_keys = {key(t) for t in intended_taxonomy if key(t)}

    live: Dict[str, int] = {}
    placeholders: Dict[str, int] = {}
    for value, count in counts.items():
        if is_blank_value(value):
            continue
        (placeholders if key(value) in ph_keys else live)[str(value)] = int(count)

    norms = {v: normalize(v) for v in live}
    toks = {v: frozenset(norms[v].split()) for v in live}
    keys = {v: norms[v].replace(" ", "") for v in live}

    dsu = _DSU(live.keys())
    links: List[Tuple[str, str, str, str]] = []  # a, b, tier, detail

    ordered = sorted(live.keys(), key=lambda v: (-live[v], v.lower()))
    blessed = {v for v in live if v in tax_exact}

    def semantic_union_allowed(a: str, b: str) -> bool:
        """A synonym or subset link may not put two blessed picklist values together."""
        ra, rb = dsu.find(a), dsu.find(b)
        merged = sum(1 for v in blessed if dsu.find(v) in (ra, rb))
        return merged < 2

    # exact: identical collision key. Typographic — never guarded.
    by_key: Dict[str, List[str]] = {}
    for v in ordered:
        by_key.setdefault(keys[v], []).append(v)
    for k, group in by_key.items():
        for other in group[1:]:
            dsu.union(group[0], other)
            links.append((group[0], other, "exact", f"both reduce to the key '{k}'"))

    # similar (typographic, unguarded) / subset (semantic, guarded)
    reps = [group[0] for group in by_key.values()]
    for i, a in enumerate(reps):
        for b in reps[i + 1 :]:
            if dsu.find(a) == dsu.find(b):
                continue
            score = similarity(norms[a], norms[b])
            if score >= similarity_threshold:
                dsu.union(a, b)
                links.append((a, b, "similar", f"'{a}' vs '{b}' match at {score:.2f} string similarity"))
                continue
            ta, tb = toks[a], toks[b]
            short, long_ = (a, b) if len(ta) < len(tb) else (b, a)
            ts, tl = (ta, tb) if len(ta) < len(tb) else (tb, ta)
            if ts and ts < tl and len(tl - ts) == 1 and live[short] >= subset_min_records:
                if not semantic_union_allowed(a, b):
                    continue
                dsu.union(a, b)
                links.append(
                    (a, b, "subset",
                     f"'{short}' is '{long_}' minus one word — same channel recorded at two "
                     f"levels of detail, or two deliberately different channels")
                )

    # synonym: same entry in the channel lexicon. Semantic — guarded.
    by_group: Dict[str, List[str]] = {}
    for v in ordered:
        group = lex.get(keys[v])
        if group:
            by_group.setdefault(group, []).append(v)
    for group, members in by_group.items():
        anchor = members[0]
        for other in members[1:]:
            if dsu.find(anchor) == dsu.find(other):
                continue
            if not semantic_union_allowed(anchor, other):
                continue
            dsu.union(anchor, other)
            links.append(
                (anchor, other, "synonym",
                 f"'{anchor}' and '{other}' are both known names for {GROUP_LABELS.get(group, group)}")
            )

    grouped: Dict[str, List[str]] = {}
    for v in ordered:
        grouped.setdefault(dsu.find(v), []).append(v)

    clusters: List[Cluster] = []
    for root, members in grouped.items():
        if len(members) < 2:
            continue
        member_set = set(members)
        my_links = [l for l in links if l[0] in member_set and l[1] in member_set]
        tiers = {l[2] for l in my_links}
        tier = next(t for t in _TIER_ORDER if t in tiers)
        confidence = _TIER_CONFIDENCE[tier]
        if tier == "similar":
            scores = [float(m.group(1)) for m in
                      (re.search(r"match at (\d\.\d+)", l[3]) for l in my_links if l[2] == "similar") if m]
            confidence = "high" if scores and min(scores) >= 0.93 else "medium"

        in_tax = [v for v in members if v in tax_exact]
        note = ""
        if len(in_tax) >= 2:
            note = (" NOTE: " + " and ".join(repr(v) for v in in_tax[:3]) + " are in your own "
                    "picklist. The picklist itself carries more than one spelling of the same value, "
                    "so no amount of rep training fixes this — the list has to change.")

        canonical = _choose_canonical(members, live, keys, tax_exact, lex)
        reason = "; ".join(dict.fromkeys(l[3] for l in my_links))[:600] + note

        cluster_members = [
            ClusterMember(value=v, records=live[v], in_taxonomy=v in tax_exact, normalized=norms[v])
            for v in sorted(members, key=lambda x: (-live[x], x.lower()))
        ]
        total = sum(live[v] for v in members)
        clusters.append(
            Cluster(
                id=f"cluster-{keys[canonical] or root}",
                tier=tier,
                confidence=confidence,
                proposed_canonical=canonical,
                reason=reason,
                members=cluster_members,
                total_records=total,
                non_canonical_records=total - live.get(canonical, 0),
            )
        )

    clusters.sort(key=lambda c: (-c.non_canonical_records, -c.total_records, c.proposed_canonical.lower()))

    if include_placeholder_cluster and len(placeholders) >= 2:
        members = sorted(placeholders.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        clusters.append(
            Cluster(
                id="cluster-placeholders",
                tier="placeholder",
                confidence="high",
                proposed_canonical="— treat as unattributed —",
                reason=("These values occupy the source field without attributing anything. They are "
                        "not a channel to merge into; they are the size of the hole in the report."),
                members=[ClusterMember(value=v, records=c, in_taxonomy=key(v) in tax_keys, normalized=normalize(v))
                         for v, c in members],
                total_records=sum(placeholders.values()),
                non_canonical_records=sum(placeholders.values()),
                requires_human_confirmation=False,
            )
        )
    return clusters


def _choose_canonical(
    members: Sequence[str],
    counts: Dict[str, int],
    keys: Dict[str, str],
    tax_exact: set,
    lexicon: Dict[str, str],
) -> str:
    """
    Deterministic, and biased towards what the customer already decided:
      1. a member already in the intended taxonomy, highest record count first
      2. a member whose spelling matches the lexicon's display name for the group
      3. the highest record count
    Ties break alphabetically so the same data always proposes the same canonical
    value — otherwise the baseline diff churns for no reason.
    """
    def rank(value: str) -> Tuple[int, int, int, str]:
        group = lexicon.get(keys[value])
        display_match = 1 if group and key(GROUP_LABELS.get(group, "")) == keys[value] else 0
        return (0 if value in tax_exact else 1, -display_match, -counts[value], value.lower())

    return sorted(members, key=rank)[0]


def proposed_mapping(clusters: Sequence[Cluster]) -> List[Dict[str, Any]]:
    """
    The table a customer can actually act on: one row per value that would move.
    Rows where current == proposed are dropped — nobody needs a migration row
    that says 'leave this alone'.
    """
    rows: List[Dict[str, Any]] = []
    for cluster in clusters:
        for member in cluster.members:
            if member.value == cluster.proposed_canonical:
                continue
            rows.append({
                "Current value": member.value,
                "Records": member.records,
                "Proposed canonical": cluster.proposed_canonical,
                "Evidence tier": cluster.tier,
                "Confidence": cluster.confidence,
                "In your taxonomy?": "yes" if member.in_taxonomy else "no",
                "Why": cluster.reason,
            })
    rows.sort(key=lambda r: (-r["Records"], str(r["Current value"]).lower()))
    return rows


def off_taxonomy_values(
    counts: Dict[str, int],
    intended_taxonomy: Sequence[str],
    placeholder_values: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    """
    Attributed values that are not literally in the customer's stated taxonomy.

    Splits into the two kinds, because they have different fixes:
      variant_of set   -> a spelling of a value you DO have ('webinar' for 'Webinar').
                          Fix at the picklist / validation layer.
      variant_of None  -> a value you never defined at all. Fix at the capture
                          layer, or admit the taxonomy is out of date.
    """
    tax_exact = {str(t).strip(): str(t).strip() for t in intended_taxonomy if str(t).strip()}
    by_key: Dict[str, str] = {}
    for t in tax_exact:
        by_key.setdefault(key(t), t)
    ph_keys = placeholder_keys(placeholder_values)

    out: List[Dict[str, Any]] = []
    for value, count in counts.items():
        text = str(value)
        if not is_attributed(text, ph_keys) or text.strip() in tax_exact:
            continue
        out.append({"value": text, "records": int(count), "variant_of": by_key.get(key(text))})
    out.sort(key=lambda r: (-r["records"], r["value"].lower()))
    return out


# --------------------------------------------------------------------------- cli


def _cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cluster CRM source values and propose a canonical taxonomy mapping.",
        epilog="Input is a JSON object of {\"source value\": record_count}.",
    )
    parser.add_argument("--counts-file", required=True, help="JSON file: {value: count}")
    parser.add_argument("--taxonomy", default="", help="Comma-separated intended taxonomy")
    parser.add_argument("--threshold", type=float, default=0.88)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text table")
    args = parser.parse_args(argv)

    counts = json.loads(Path(args.counts_file).read_text(encoding="utf-8"))
    taxonomy = [t.strip() for t in args.taxonomy.split(",") if t.strip()]
    clusters = cluster_values(counts, intended_taxonomy=taxonomy, similarity_threshold=args.threshold)

    if args.json:
        print(json.dumps([c.to_dict() for c in clusters], indent=2))
        return 0

    if not clusters:
        print("No duplicate or near-duplicate source values found.")
        return 0

    print(f"{len(clusters)} candidate cluster(s) — ALL require human confirmation.\n")
    for cluster in clusters:
        print(f"  {cluster.proposed_canonical}   [{cluster.tier} / {cluster.confidence} confidence]")
        for member in cluster.members:
            mark = "*" if member.value == cluster.proposed_canonical else " "
            tax = " (in taxonomy)" if member.in_taxonomy else ""
            print(f"    {mark} {member.value!r:<34} {member.records:>6,} records{tax}")
        print(f"    why: {cluster.reason}\n")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())

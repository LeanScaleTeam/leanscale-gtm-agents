#!/usr/bin/env python3
"""
transcripts.py — turn whatever your recording tool exports into one shape.

Only about four in ten teams this plugin ships to own a conversation-intelligence
platform. So there is no privileged source here: Gong, Fireflies, Chorus, Grain,
Otter, Zoom, Google Meet/Drive and a plain folder of exported files all land in
the same normalized structure, and everything downstream reads only that.

    Normalized call
    ---------------
    {
      "call_id": "call-1042",
      "source": "zoom",
      "title": "Fabrik Robotics <> Acme Data — Discovery",
      "started_at": "2026-07-14T15:02:00Z",
      "duration_sec": 2712,
      "call_type": "discovery",
      "account": "Fabrik Robotics",
      "deal_id": "0061",
      "rep": "Dana Whitfield",
      "attribution": {"confidence": "high", "method": "email_domain",
                      "unresolved_speakers": [], "unresolved_word_share": 0.0},
      "turns": [{"idx": 0, "speaker": "Dana Whitfield", "speaker_email": "...",
                 "is_internal": true, "start_sec": 12.4, "end_sec": 31.0,
                 "text": "...", "words": 24}]
    }

SPEAKER ATTRIBUTION IS THE TRAP. Half of these export formats carry no email and
no internal/external flag — just a display name, and sometimes only "Speaker 2".
A coaching report that quietly credits a customer's discovery question to a rep
is worse than no report at all. So this module resolves internal speakers in a
fixed order of trust, records which method won, and where it cannot resolve a
speaker it says so and marks the call degraded. Downstream, degraded calls are
excluded from every talk-time and question-rate number and listed by name.

Python 3.9+, standard library only. No network. Reads and writes local files.

    python3 transcripts.py normalize --raw <run>/raw --out <run>/raw/normalized_calls.json
    python3 transcripts.py inspect <file> [--internal-domain acme.com]
    python3 transcripts.py selftest --fixtures <plugin>/fixtures
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Speaking-time fallback when a format gives no end timestamps. 150 wpm is the
# conversational-English average; it is an ESTIMATE and every report that uses
# it says so out loud.
WORDS_PER_MINUTE = 150.0

# Adjacent cues from the same speaker inside this gap are one utterance. VTT in
# particular chops a sentence into three cues, which would otherwise shred every
# quote you try to verify.
MERGE_GAP_SEC = 3.0

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "protonmail.com", "live.com", "msn.com", "me.com", "mail.com",
}

GENERIC_SPEAKER = re.compile(r"^(speaker|participant|attendee|unknown)[\s_-]*\d*$", re.I)


# --------------------------------------------------------------------- time


def format_ts(seconds: Optional[float]) -> str:
    """Seconds -> mm:ss (or h:mm:ss). This is the timestamp format every quote uses."""
    if seconds is None:
        return "--:--"
    seconds = max(0, int(round(float(seconds))))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_ts(value: Any) -> Optional[float]:
    """'11:42' / '01:11:42' / '00:00:12.400' / 702 -> seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().strip("[]()")
    if not text:
        return None
    text = text.replace(",", ".")
    parts = text.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None
    return None


def _seconds(value: Any, unit_hint: Optional[str] = None) -> Optional[float]:
    """
    Normalize a numeric offset to seconds. Gong emits milliseconds, Fireflies
    seconds, Otter sometimes either.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        if ":" in value:
            return parse_ts(value)
        try:
            value = float(value)
        except ValueError:
            return None
    value = float(value)
    if unit_hint == "ms":
        return value / 1000.0
    if unit_hint == "s":
        return value
    return value / 1000.0 if value > 18000 else value


def _decide_time_unit(values: Sequence[Any]) -> str:
    """
    Decide milliseconds vs seconds once for a whole transcript, from its largest
    offset. Deciding per value is how the first sentence of a Gong call — 8200,
    meaning 8.2 seconds — becomes a two-hour timestamp. A sales call over five
    hours does not exist, so a maximum above 18,000 means milliseconds.
    """
    numeric: List[float] = []
    for value in values:
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)):
            numeric.append(float(value))
        elif isinstance(value, str) and ":" not in value:
            try:
                numeric.append(float(value))
            except ValueError:
                continue
    if not numeric:
        return "s"
    return "ms" if max(numeric) > 18000 else "s"


# ------------------------------------------------------------------- quotes


def normalize_quote(text: Any) -> str:
    """Lowercase alphanumeric skeleton used to verify a quote really was said."""
    if text is None:
        return ""
    cleaned = re.sub(r"[^a-z0-9\s]", " ", str(text).lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def find_quote(quote: str, turns: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Locate a quoted line in the transcript. Returns the matching turn, or None.

    Tolerates an ellipsis-joined quote ("we're drowning ... every single week"):
    every fragment must appear, in order, inside one turn. Long quotes match on
    their first eight words so light trimming at the tail does not fail a real
    quote — but an invented one still will, which is the point.
    """
    fragments = [normalize_quote(f) for f in re.split(r"\.{3}|…", str(quote or ""))]
    fragments = [f for f in fragments if len(f.split()) >= 2]
    if not fragments:
        return None
    probes = []
    for frag in fragments:
        words = frag.split()
        probes.append(" ".join(words[:8]) if len(words) > 8 else frag)
    for turn in turns:
        hay = turn.get("_norm") or normalize_quote(turn.get("text"))
        cursor, ok = 0, True
        for probe in probes:
            found = hay.find(probe, cursor)
            if found < 0:
                ok = False
                break
            cursor = found + len(probe)
        if ok:
            return turn
    return None


# ------------------------------------------------------------------- parsers


def _looks_like_speaker(candidate: str, known: Optional[set] = None) -> bool:
    """
    Guard against reading 'Note: the recording starts late' as a speaker turn.
    A speaker label is short, mostly letters, and not a sentence.
    """
    text = (candidate or "").strip()
    if not text or len(text) > 48:
        return False
    if known and text.lower() in known:
        return True
    words = text.split()
    if len(words) > 6:
        return False
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 .'’\-_()&/]*$", text):
        return False
    if text.endswith((",", ";")):
        return False
    letters = sum(1 for ch in text if ch.isalpha())
    return letters >= 2


def _merge_turns(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fold consecutive same-speaker fragments into one utterance."""
    merged: List[Dict[str, Any]] = []
    for row in rows:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        if merged:
            last = merged[-1]
            same = (last.get("speaker") or "") == (row.get("speaker") or "")
            gap = None
            if last.get("end_sec") is not None and row.get("start_sec") is not None:
                gap = row["start_sec"] - last["end_sec"]
            if same and (gap is None or gap <= MERGE_GAP_SEC):
                last["text"] = (last["text"] + " " + text).strip()
                if row.get("end_sec") is not None:
                    last["end_sec"] = row["end_sec"]
                continue
        merged.append(
            {
                "speaker": row.get("speaker"),
                "speaker_email": row.get("speaker_email"),
                "start_sec": row.get("start_sec"),
                "end_sec": row.get("end_sec"),
                "text": text,
                "affiliation": row.get("affiliation"),
            }
        )
    return merged


_CUE_TIME = re.compile(
    r"(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\s*-->\s*"
    r"(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)"
)
_VOICE = re.compile(r"<v\s+([^>]+?)\s*>(.*?)(?:</v>)?\s*$", re.S)
_SPEAKER_COLON = re.compile(r"^([^:]{1,48}):\s*(.*)$", re.S)


def parse_vtt(text: str, known_speakers: Optional[set] = None) -> List[Dict[str, Any]]:
    """
    WebVTT / SRT. Covers Zoom cloud recordings ('Name: text' inside the cue),
    Microsoft Teams and Google Meet ('<v Name>text</v>'), and hand-exported
    subtitle files with no speaker labels at all (speaker carries forward).
    """
    rows: List[Dict[str, Any]] = []
    lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i, last_speaker = 0, None
    while i < len(lines):
        match = _CUE_TIME.search(lines[i])
        if not match:
            i += 1
            continue
        start, end = parse_ts(match.group(1)), parse_ts(match.group(2))
        i += 1
        payload: List[str] = []
        while i < len(lines) and lines[i].strip() != "":
            if _CUE_TIME.search(lines[i]):
                break
            payload.append(lines[i].strip())
            i += 1
        if not payload:
            continue
        body = " ".join(payload).strip()
        speaker = None
        voice = _VOICE.match(body)
        if voice:
            speaker = voice.group(1).strip()
            body = re.sub(r"</?v[^>]*>", "", voice.group(2)).strip()
        else:
            colon = _SPEAKER_COLON.match(body)
            if colon and _looks_like_speaker(colon.group(1), known_speakers):
                speaker = colon.group(1).strip()
                body = colon.group(2).strip()
        if speaker:
            last_speaker = speaker
        else:
            speaker = last_speaker
        body = re.sub(r"<[^>]+>", "", body).strip()
        if body:
            rows.append({"speaker": speaker, "start_sec": start, "end_sec": end, "text": body})
    return _merge_turns(rows)


# Name (12:04): text        |  [00:12:04] Name: text
# 12:04 Name: text          |  Name: text
_DIA_NAME_PAREN = re.compile(r"^\s*([^():\[\]]{1,48})\s*[\(\[]\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[\)\]]\s*:?\s*(.*)$")
_DIA_TS_FIRST = re.compile(r"^\s*[\[\(]?\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[\]\)]?\s*[-–|]?\s*([^:]{1,48}):\s*(.*)$")
_DIA_NAME_ONLY = re.compile(r"^\s*([^:]{1,48}):\s*(.*)$")
_DIA_HEADER = re.compile(r"^\s*([A-Za-z][^:]{0,46}?)\s+[\[\(]?(\d{1,2}:\d{2}(?::\d{2})?)[\]\)]?\s*$")


def parse_diarized_text(text: str, known_speakers: Optional[set] = None) -> List[Dict[str, Any]]:
    """
    Plain diarized text — what Otter, Fathom, Gemini/Meet notes, Grain and every
    'copy the transcript into a doc' workflow produce. Four layouts, plus
    continuation lines that belong to the turn above them.
    """
    rows: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw_line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.upper().startswith("WEBVTT") or _CUE_TIME.search(stripped):
            continue

        speaker = start = body = None

        m = _DIA_NAME_PAREN.match(line)
        if m and _looks_like_speaker(m.group(1), known_speakers):
            speaker, start, body = m.group(1).strip(), parse_ts(m.group(2)), m.group(3).strip()
        if speaker is None:
            m = _DIA_TS_FIRST.match(line)
            if m and _looks_like_speaker(m.group(2), known_speakers):
                speaker, start, body = m.group(2).strip(), parse_ts(m.group(1)), m.group(3).strip()
        if speaker is None:
            m = _DIA_HEADER.match(line)
            if m and _looks_like_speaker(m.group(1), known_speakers):
                speaker, start, body = m.group(1).strip(), parse_ts(m.group(2)), ""
        if speaker is None:
            m = _DIA_NAME_ONLY.match(line)
            if m and _looks_like_speaker(m.group(1), known_speakers):
                speaker, body = m.group(1).strip(), m.group(2).strip()

        if speaker is not None:
            current = {"speaker": speaker, "start_sec": start, "end_sec": None, "text": body}
            rows.append(current)
        elif current is not None:
            current["text"] = (current["text"] + " " + stripped).strip()
        # A pre-amble line before any speaker label is metadata; drop it.
    return _merge_turns([r for r in rows if (r.get("text") or "").strip()])


_JSON_TURN_KEYS = ("turns", "sentences", "segments", "monologues", "transcript",
                   "entries", "utterances", "results", "items")
_JSON_SPEAKER_KEYS = ("speaker", "speaker_name", "speakerName", "speaker_label",
                      "speakerLabel", "name", "from", "participant", "speaker_id", "speakerId")
_JSON_TEXT_KEYS = ("text", "raw_text", "rawText", "sentence", "content", "value", "transcript")
_JSON_START_KEYS = ("start", "start_sec", "start_time", "startTime", "startTimeMs",
                    "start_ms", "ts", "timestamp", "offset", "begin", "time")
_JSON_END_KEYS = ("end", "end_sec", "end_time", "endTime", "end_ms", "stop", "finish")
_JSON_EMAIL_KEYS = ("email", "speaker_email", "emailAddress", "email_address")


def _pick(obj: Dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def parse_json_transcript(
    payload: Any,
    speaker_index: Optional[Dict[str, Dict[str, Any]]] = None,
    unit_hint: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    JSON exports. Handles the Gong shape (callTranscripts -> transcript ->
    sentences, speakerId referencing the parties list, offsets in ms), the
    Fireflies shape (sentences with speaker_name / raw_text / start_time), and
    the generic 'list of turns' shape everything else emits.
    """
    speaker_index = speaker_index or {}
    rows: List[Dict[str, Any]] = []

    def push(speaker: Any, text: Any, start: Any, end: Any, email: Any = None, affiliation: Any = None) -> None:
        body = str(text or "").strip()
        if not body:
            return
        label = speaker
        meta = speaker_index.get(str(speaker)) if speaker is not None else None
        if meta:
            label = meta.get("name") or speaker
            email = email or meta.get("email")
            affiliation = affiliation or meta.get("affiliation")
        rows.append(
            {
                "speaker": None if label is None else str(label),
                "speaker_email": email,
                "affiliation": affiliation,
                "_raw_start": start,
                "_raw_end": end,
                "text": body,
            }
        )

    def finish() -> List[Dict[str, Any]]:
        unit = unit_hint if unit_hint in ("ms", "s") else _decide_time_unit(
            [r["_raw_start"] for r in rows] + [r["_raw_end"] for r in rows]
        )
        for row in rows:
            row["start_sec"] = _seconds(row.pop("_raw_start"), unit)
            row["end_sec"] = _seconds(row.pop("_raw_end"), unit)
        return _merge_turns(rows)

    # -- Gong: callTranscripts[].transcript[].sentences[]
    if isinstance(payload, dict) and "callTranscripts" in payload:
        for call in payload.get("callTranscripts") or []:
            for block in call.get("transcript") or []:
                speaker = block.get("speakerId") or block.get("speakerName")
                for sentence in block.get("sentences") or []:
                    push(speaker, sentence.get("text"), sentence.get("start"), sentence.get("end"))
        if rows:
            return finish()

    if isinstance(payload, dict):
        for wrapper in ("data", "transcript", "result", "call", "recording", "meeting"):
            inner = payload.get(wrapper)
            if isinstance(inner, dict) and any(k in inner for k in _JSON_TURN_KEYS):
                payload = inner
                break

    items: Any = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in _JSON_TURN_KEYS:
            value = payload.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                items = value
                break

    if not items:
        return []

    for item in items:
        if not isinstance(item, dict):
            continue
        # A nested block with its own sentence list (Gong-like, Chorus-like).
        nested = None
        for key in ("sentences", "words", "turns"):
            value = item.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict) and _pick(value[0], _JSON_TEXT_KEYS):
                nested = value
                break
        speaker = _pick(item, _JSON_SPEAKER_KEYS)
        email = _pick(item, _JSON_EMAIL_KEYS)
        affiliation = item.get("affiliation") or item.get("participantType") or item.get("speaker_type")
        if nested:
            for sub in nested:
                push(
                    _pick(sub, _JSON_SPEAKER_KEYS) or speaker,
                    _pick(sub, _JSON_TEXT_KEYS),
                    _pick(sub, _JSON_START_KEYS),
                    _pick(sub, _JSON_END_KEYS),
                    email,
                    affiliation,
                )
            continue
        push(
            speaker,
            _pick(item, _JSON_TEXT_KEYS),
            _pick(item, _JSON_START_KEYS),
            _pick(item, _JSON_END_KEYS),
            email,
            affiliation,
        )
    return finish()


def parse_transcript_file(
    path: Path,
    fmt: Optional[str] = None,
    speaker_index: Optional[Dict[str, Dict[str, Any]]] = None,
    known_speakers: Optional[set] = None,
    unit_hint: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], str]:
    """Dispatch on declared format, then on extension, then on content sniffing."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    suffix = Path(path).suffix.lower()
    fmt = (fmt or "").lower() or None

    if fmt in ("json", "gong_json", "fireflies_json", "chorus_json", "grain_json", "otter_json"):
        return parse_json_transcript(json.loads(text), speaker_index, unit_hint), "json"
    if fmt in ("vtt", "srt", "webvtt"):
        return parse_vtt(text, known_speakers), "vtt"
    if fmt in ("txt", "text", "md", "diarized", "diarized_text"):
        return parse_diarized_text(text, known_speakers), "diarized_text"

    if suffix == ".json":
        return parse_json_transcript(json.loads(text), speaker_index, unit_hint), "json"
    if suffix in (".vtt", ".srt"):
        return parse_vtt(text, known_speakers), "vtt"
    if suffix in (".txt", ".md", ".markdown", ".text"):
        return parse_diarized_text(text, known_speakers), "diarized_text"

    head = text.lstrip()[:200]
    if head.startswith("{") or head.startswith("["):
        return parse_json_transcript(json.loads(text), speaker_index, unit_hint), "json"
    if head.upper().startswith("WEBVTT") or _CUE_TIME.search(text[:2000] or ""):
        return parse_vtt(text, known_speakers), "vtt"
    return parse_diarized_text(text, known_speakers), "diarized_text"


# ------------------------------------------------------- speaker attribution


def _domain(email: Any) -> Optional[str]:
    if not email:
        return None
    match = re.search(r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", str(email))
    return match.group(1).lower() if match else None


def _key(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


class SpeakerResolver:
    """
    Decides internal vs external, in a fixed order of trust:

      1. the provider's own affiliation flag   (Gong, Chorus) — trusted
      2. the speaker's email domain            — trusted
      3. an exact name match in the call's participant list, which carries emails
      4. an exact name match against the rep roster from config
      5. unresolved — and we say so, loudly, rather than guessing

    Steps 1-2 give a call `high` confidence. Step 3-4 give `medium`. Anything
    unresolved past 5% of spoken words makes the call `low`, and low-confidence
    calls are dropped from every mechanics number downstream.
    """

    def __init__(
        self,
        internal_domains: Sequence[str],
        roster: Sequence[Dict[str, Any]],
        participants: Sequence[Dict[str, Any]],
    ):
        self.internal_domains = {d.lower().lstrip("@") for d in internal_domains if d}
        self.roster = {_key(r.get("name")): r for r in roster if r.get("name")}
        for rep in roster:
            domain = _domain(rep.get("email"))
            if domain and domain not in FREE_EMAIL_DOMAINS:
                self.internal_domains.add(domain)
        self.participants: Dict[str, Dict[str, Any]] = {}
        for person in participants or []:
            if person.get("name"):
                self.participants[_key(person["name"])] = person

    def resolve(self, name: Any, email: Any = None, affiliation: Any = None) -> Tuple[Optional[bool], str, Optional[str]]:
        """-> (is_internal | None, method, resolved_email)"""
        person = self.participants.get(_key(name)) or {}
        email = email or person.get("email")
        affiliation = affiliation or person.get("affiliation")

        if affiliation:
            flag = str(affiliation).strip().lower()
            if flag in ("internal", "host", "rep", "seller", "organizer", "employee"):
                return True, "provider_affiliation", email
            if flag in ("external", "customer", "prospect", "guest", "buyer", "attendee"):
                return False, "provider_affiliation", email

        domain = _domain(email)
        if domain:
            return (domain in self.internal_domains), "email_domain", email

        if _key(name) in self.roster:
            return True, "roster_name", self.roster[_key(name)].get("email")

        if person and person.get("is_internal") is not None:
            return bool(person["is_internal"]), "participant_list", email

        return None, "unresolved", None


# ------------------------------------------------------------- normalization


def normalize_call(
    meta: Dict[str, Any],
    turns: List[Dict[str, Any]],
    internal_domains: Sequence[str],
    roster: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Attach attribution + timing to parsed turns and emit one normalized call."""
    resolver = SpeakerResolver(internal_domains, roster, meta.get("participants") or [])

    out_turns: List[Dict[str, Any]] = []
    methods: Dict[str, int] = {}
    unresolved: Dict[str, int] = {}
    total_words = 0

    for idx, turn in enumerate(turns):
        text = (turn.get("text") or "").strip()
        words = len(text.split())
        total_words += words
        is_internal, method, email = resolver.resolve(
            turn.get("speaker"), turn.get("speaker_email"), turn.get("affiliation")
        )
        methods[method] = methods.get(method, 0) + 1
        if is_internal is None:
            label = str(turn.get("speaker") or "(unlabelled)")
            unresolved[label] = unresolved.get(label, 0) + words
        out_turns.append(
            {
                "idx": idx,
                "speaker": turn.get("speaker"),
                "speaker_email": email,
                "is_internal": is_internal,
                "start_sec": turn.get("start_sec"),
                "end_sec": turn.get("end_sec"),
                "ts": format_ts(turn.get("start_sec")),
                "text": text,
                "words": words,
                "_norm": normalize_quote(text),
            }
        )

    # Timing method: only trust explicit end times if nearly every turn has one,
    # otherwise estimate the whole call the same way. Mixing the two skews a
    # talk ratio by more than the thing you are trying to measure.
    timed = sum(
        1 for t in out_turns
        if t["start_sec"] is not None and t["end_sec"] is not None and t["end_sec"] > t["start_sec"]
    )
    timing_method = "timestamps" if out_turns and timed >= 0.8 * len(out_turns) else "word_estimate"
    for turn in out_turns:
        if timing_method == "timestamps":
            turn["duration_sec"] = round(max(0.0, (turn["end_sec"] or 0) - (turn["start_sec"] or 0)), 2)
        else:
            turn["duration_sec"] = round(turn["words"] / WORDS_PER_MINUTE * 60.0, 2)

    unresolved_words = sum(unresolved.values())
    unresolved_share = round(unresolved_words / total_words, 4) if total_words else 0.0
    if unresolved_share > 0.05:
        confidence = "low"
    elif unresolved_share > 0 or methods.get("roster_name") or methods.get("participant_list"):
        confidence = "medium"
    else:
        confidence = "high"
    if not out_turns:
        confidence = "low"

    primary_method = max(methods.items(), key=lambda kv: kv[1])[0] if methods else "none"

    duration = meta.get("duration_sec")
    if not duration:
        ends = [t["end_sec"] for t in out_turns if t["end_sec"] is not None]
        starts = [t["start_sec"] for t in out_turns if t["start_sec"] is not None]
        if ends:
            duration = max(ends)
        elif starts:
            duration = max(starts) + 30
        else:
            duration = round(sum(t["duration_sec"] for t in out_turns), 1)

    speakers: Dict[str, Dict[str, Any]] = {}
    for turn in out_turns:
        label = turn["speaker"] or "(unlabelled)"
        row = speakers.setdefault(
            label, {"speaker": label, "email": turn["speaker_email"], "is_internal": turn["is_internal"],
                    "turns": 0, "words": 0, "seconds": 0.0}
        )
        row["turns"] += 1
        row["words"] += turn["words"]
        row["seconds"] = round(row["seconds"] + turn["duration_sec"], 1)

    return {
        "call_id": meta.get("call_id"),
        "source": meta.get("source") or "unknown",
        "title": meta.get("title") or meta.get("call_id"),
        "started_at": meta.get("started_at"),
        "duration_sec": round(float(duration or 0), 1),
        "call_type": (meta.get("call_type") or "unknown").lower(),
        "account": meta.get("account"),
        "deal_id": meta.get("deal_id"),
        "deal_amount": meta.get("deal_amount"),
        "rep": meta.get("rep"),
        "rep_email": meta.get("rep_email"),
        "transcript_file": meta.get("transcript_file"),
        "transcript_format": meta.get("_parsed_format"),
        "participants": meta.get("participants") or [],
        "attribution": {
            "confidence": confidence,
            "method": primary_method,
            "methods": methods,
            "unresolved_speakers": sorted(unresolved.keys()),
            "unresolved_word_share": unresolved_share,
            "timing_method": timing_method,
            "note": (
                "Speaking time measured from the transcript's own timestamps."
                if timing_method == "timestamps"
                else f"This export carries no end timestamps, so speaking time is estimated "
                     f"from word count at {WORDS_PER_MINUTE:.0f} wpm."
            ),
        },
        "speakers": sorted(speakers.values(), key=lambda s: -s["words"]),
        "turns": [{k: v for k, v in t.items() if k != "_norm"} for t in out_turns],
    }


def load_call_index(raw_dir: Path) -> Dict[str, Any]:
    path = Path(raw_dir) / "calls.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No call index at {path}. The :run skill writes it after listing calls "
            f"from your transcript source — run /sales-coach:run, or /sales-coach:setup "
            f"if the source was never connected."
        )
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload if isinstance(payload, dict) else {"calls": payload}


def normalize_all(
    raw_dir: Path,
    internal_domains: Sequence[str],
    roster: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse and normalize every call named in raw/calls.json."""
    raw_dir = Path(raw_dir)
    index = load_call_index(raw_dir)
    domains = list(internal_domains) + list(index.get("internal_domains") or [])
    warnings: List[str] = []
    calls: List[Dict[str, Any]] = []

    for meta in index.get("calls") or []:
        meta = dict(meta)
        meta.setdefault("source", index.get("source"))
        rel = meta.get("transcript_file")
        inline = meta.get("transcript")
        speaker_index = {}
        for person in meta.get("participants") or []:
            for key in (person.get("speaker_id"), person.get("speakerId"), person.get("name")):
                if key:
                    speaker_index[str(key)] = person
        known = {str(p.get("name", "")).lower() for p in meta.get("participants") or [] if p.get("name")}
        known |= {str(r.get("name", "")).lower() for r in roster if r.get("name")}

        turns: List[Dict[str, Any]] = []
        if rel:
            path = Path(rel)
            if not path.is_absolute():
                path = raw_dir / rel
            if not path.exists():
                warnings.append(f"{meta.get('call_id')}: transcript file not found at {path}")
                continue
            try:
                turns, fmt = parse_transcript_file(
                    path, meta.get("transcript_format"), speaker_index, known, meta.get("time_unit")
                )
                meta["_parsed_format"] = fmt
            except (ValueError, json.JSONDecodeError) as exc:
                warnings.append(f"{meta.get('call_id')}: could not parse {path.name} ({exc})")
                continue
        elif inline is not None:
            if isinstance(inline, str):
                turns = parse_diarized_text(inline, known)
                meta["_parsed_format"] = "diarized_text"
            else:
                turns = parse_json_transcript(inline, speaker_index, meta.get("time_unit"))
                meta["_parsed_format"] = "json"
        else:
            warnings.append(f"{meta.get('call_id')}: no transcript_file and no inline transcript")
            continue

        if not turns:
            warnings.append(
                f"{meta.get('call_id')}: parsed 0 turns from {rel or 'inline transcript'} — "
                f"the file may be an unsupported layout. Run "
                f"`transcripts.py inspect <file>` to see what the parser found."
            )
            continue

        call = normalize_call(meta, turns, domains, roster)
        if call["attribution"]["confidence"] == "low":
            who = ", ".join(call["attribution"]["unresolved_speakers"]) or "unlabelled speakers"
            warnings.append(
                f"{call['call_id']}: could not tell who is internal for {who} "
                f"({call['attribution']['unresolved_word_share']:.0%} of spoken words). "
                f"Mechanics suppressed for this call."
            )
        calls.append(call)

    return calls, warnings


# --------------------------------------------------------------------- cli


def _cmd_normalize(args: argparse.Namespace) -> int:
    roster: List[Dict[str, Any]] = []
    domains: List[str] = list(args.internal_domain or [])
    if args.config:
        with open(args.config, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        roster = cfg.get("reps") or []
        domains += (cfg.get("transcript_source") or {}).get("internal_domains") or []
    calls, warnings = normalize_all(Path(args.raw), domains, roster)
    out = Path(args.out) if args.out else Path(args.raw) / "normalized_calls.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        json.dump({"calls": calls, "warnings": warnings}, fh, indent=2, default=str)
        fh.write("\n")
    print(f"normalized {len(calls)} call(s) -> {out}")
    for call in calls:
        att = call["attribution"]
        print(
            f"  {call['call_id']:<14} {len(call['turns']):>3} turns  "
            f"{call['duration_sec']/60:>5.1f} min  attribution={att['confidence']} "
            f"({att['method']}, timing={att['timing_method']})"
        )
    for warning in warnings:
        print(f"  ! {warning}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    turns, fmt = parse_transcript_file(Path(args.file))
    resolver = SpeakerResolver(args.internal_domain or [], [], [])
    print(f"format: {fmt}   turns: {len(turns)}")
    speakers: Dict[str, int] = {}
    for turn in turns:
        speakers[str(turn.get("speaker"))] = speakers.get(str(turn.get("speaker")), 0) + 1
    print("speakers:")
    for name, count in sorted(speakers.items(), key=lambda kv: -kv[1]):
        internal, method, _ = resolver.resolve(name)
        verdict = {True: "internal", False: "external", None: "UNRESOLVED"}[internal]
        print(f"  {name:<28} {count:>3} turns   {verdict} ({method})")
    for turn in turns[:6]:
        print(f"  [{format_ts(turn.get('start_sec'))}] {turn.get('speaker')}: {turn.get('text')[:90]}")
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    """
    Exercises both hand-export parsers plus both JSON shapes against the bundled
    fixtures. This runs inside the setup smoke test, so a customer whose export
    layout is unusual finds out during setup and not three weeks later.
    """
    fixtures = Path(args.fixtures)
    raw = fixtures / "raw"
    fails: List[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if condition else 'FAIL'} {label}" + (f"   {detail}" if not condition and detail else ""))
        if not condition:
            fails.append(label)

    print("\nvtt parser (Zoom 'Name: text' cues)")
    turns = parse_vtt((raw / "transcripts/call-1042.vtt").read_text(encoding="utf-8"))
    check("turns parsed", len(turns) >= 12, f"got {len(turns)}")
    check("speaker labels lifted out of the cue", all(t["speaker"] for t in turns))
    check("timestamps ascending", all(
        (turns[i]["start_sec"] or 0) <= (turns[i + 1]["start_sec"] or 0) for i in range(len(turns) - 1)))
    check("end times present -> real timing", all(t["end_sec"] is not None for t in turns))
    check("adjacent same-speaker cues merged", len({t["speaker"] for t in turns}) < len(turns))

    print("\nvtt parser (Teams/Meet '<v Name>' cues)")
    turns_v = parse_vtt((raw / "transcripts/call-1046.vtt").read_text(encoding="utf-8"))
    check("turns parsed", len(turns_v) >= 8, f"got {len(turns_v)}")
    check("voice tags stripped", not any("<v" in t["text"] for t in turns_v))

    print("\ndiarized-text parser ('Name (mm:ss):')")
    turns_d = parse_diarized_text((raw / "transcripts/call-1043.txt").read_text(encoding="utf-8"))
    check("turns parsed", len(turns_d) >= 10, f"got {len(turns_d)}")
    check("timestamps read", sum(1 for t in turns_d if t["start_sec"] is not None) >= len(turns_d) - 1)
    check("wrapped continuation lines folded in",
          any(len(t["text"].split()) > 40 for t in turns_d))
    check("no metadata header read as a speaker",
          not any(str(t["speaker"]).lower().startswith(("recorded", "attendees", "duration")) for t in turns_d))

    print("\ndiarized-text parser ('[hh:mm:ss] Name:')")
    turns_b = parse_diarized_text((raw / "transcripts/call-1047.txt").read_text(encoding="utf-8"))
    check("turns parsed", len(turns_b) >= 8, f"got {len(turns_b)}")
    check("generic 'Speaker 2' label preserved, not guessed",
          any(GENERIC_SPEAKER.match(str(t["speaker"]) or "") for t in turns_b))

    print("\njson parser (Gong shape, ms offsets, speakerId lookup)")
    payload = json.loads((raw / "transcripts/call-1044.json").read_text(encoding="utf-8"))
    index = {"1001": {"name": "Priya Raman", "email": "priya@acmedata.io", "affiliation": "Internal"}}
    turns_g = parse_json_transcript(payload, index)
    check("turns parsed", len(turns_g) >= 8, f"got {len(turns_g)}")
    check("speakerId resolved to a name", any(t["speaker"] == "Priya Raman" for t in turns_g))
    check("ms offsets converted to seconds", all((t["start_sec"] or 0) < 7200 for t in turns_g))

    print("\njson parser (Fireflies shape)")
    payload_f = json.loads((raw / "transcripts/call-1045.json").read_text(encoding="utf-8"))
    turns_f = parse_json_transcript(payload_f)
    check("turns parsed", len(turns_f) >= 8, f"got {len(turns_f)}")
    check("raw_text field used", all(t["text"] for t in turns_f))

    print("\nquote verification")
    probe = turns[3]["text"].split(".")[0]
    check("a real quote is found", find_quote(probe, turns) is not None)
    check("an invented quote is not",
          find_quote("we are ready to sign the paperwork this afternoon", turns) is None)

    print("\nspeaker attribution")
    resolver = SpeakerResolver(["acmedata.io"], [{"name": "Dana Whitfield", "email": "dana@acmedata.io"}], [])
    check("internal by email domain", resolver.resolve("X", "dana@acmedata.io")[0] is True)
    check("external by email domain", resolver.resolve("Y", "cto@fabrikrobotics.com")[0] is False)
    check("internal by roster name", resolver.resolve("Dana Whitfield")[0] is True)
    check("unknown stays unresolved — never guessed", resolver.resolve("Speaker 2")[0] is None)
    check("provider affiliation trusted first",
          resolver.resolve("Someone", None, "External")[0] is False)

    print("\nend-to-end normalize")
    calls, warnings = normalize_all(
        raw, ["acmedata.io"],
        [{"name": "Dana Whitfield", "email": "dana@acmedata.io"},
         {"name": "Marcus Oyelaran", "email": "marcus@acmedata.io"},
         {"name": "Priya Raman", "email": "priya@acmedata.io"}],
    )
    check("every fixture call normalized", len(calls) == 6, f"got {len(calls)}")
    check("degraded call flagged, not silently attributed",
          any(c["attribution"]["confidence"] == "low" for c in calls))
    check("a degradation warning is raised", any("could not tell who is internal" in w for w in warnings))
    check("high-confidence calls exist", any(c["attribution"]["confidence"] == "high" for c in calls))

    print("")
    if fails:
        print(f"{len(fails)} FAILED: {', '.join(fails)}\n")
        return 1
    print("transcript parsers: all checks passed\n")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize call transcripts from any source.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_norm = sub.add_parser("normalize", help="parse raw/calls.json + raw/transcripts/ into one shape")
    p_norm.add_argument("--raw", required=True, help="the run's raw/ directory")
    p_norm.add_argument("--out", help="output path (default <raw>/normalized_calls.json)")
    p_norm.add_argument("--config", help="~/.leanscale-gtm/sales-coach.json, for the rep roster + domains")
    p_norm.add_argument("--internal-domain", action="append", help="repeatable")
    p_norm.set_defaults(func=_cmd_normalize)

    p_ins = sub.add_parser("inspect", help="parse one file and show what the parser saw")
    p_ins.add_argument("file")
    p_ins.add_argument("--internal-domain", action="append")
    p_ins.set_defaults(func=_cmd_inspect)

    p_self = sub.add_parser("selftest", help="run the parser tests against the bundled fixtures")
    p_self.add_argument("--fixtures", default=str(Path(__file__).resolve().parent.parent / "fixtures"))
    p_self.set_defaults(func=_cmd_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

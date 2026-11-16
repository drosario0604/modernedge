"""Extract structured call-outs from the reception log and area-manager chat."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

import openpyxl
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import get_settings

logger = logging.getLogger(__name__)

REASON_CATEGORIES = frozenset(
    {"sick", "clinic", "bereavement", "jury", "family", "personal", "vacation", "other", "unknown"}
)

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "employee_hint": {"type": "string"},
        "event_date": {"type": "string", "description": "YYYY-MM-DD"},
        "shift": {"type": ["string", "null"], "enum": ["DAY", "NIGHT", "day", "night", "D", "N", None]},
        "reason_category": {
            "type": "string",
            "enum": sorted(REASON_CATEGORIES),
        },
        "notice_time": {
            "type": ["string", "null"],
            "description": "ISO-8601 local datetime when notice was received",
        },
        "building_hint": {"type": ["string", "null"]},
        "is_absence": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "raw_summary": {"type": ["string", "null"]},
    },
    "required": [
        "employee_hint",
        "event_date",
        "reason_category",
        "is_absence",
        "confidence",
    ],
    "additionalProperties": True,
}


class CalloutExtractionModel(BaseModel):
    """Pydantic validation of LLM / heuristic extraction output."""

    employee_hint: str
    event_date: date
    shift: Optional[str] = None
    reason_category: str = "unknown"
    notice_time: Optional[datetime] = None
    building_hint: Optional[str] = None
    is_absence: bool = True
    confidence: float = Field(ge=0, le=1, default=0.5)
    raw_summary: Optional[str] = None
    source: str = "reception_log"
    source_ref: str = ""

    @field_validator("reason_category", mode="before")
    @classmethod
    def _norm_reason(cls, v: Any) -> str:
        text = str(v or "unknown").strip().lower()
        aliases = {
            "sick leave": "sick",
            "illness": "sick",
            "ill": "sick",
            "medical appointment": "clinic",
            "doctor": "clinic",
            "dr": "clinic",
            "funeral": "bereavement",
            "death": "bereavement",
            "jury duty": "jury",
            "family emergency": "family",
            "child sick": "family",
            "kid sick": "family",
            "personal day": "personal",
            "pto": "vacation",
            "vacation day": "vacation",
            "planned vacation": "vacation",
        }
        text = aliases.get(text, text)
        if text not in REASON_CATEGORIES:
            return "unknown"
        return text

    @field_validator("shift", mode="before")
    @classmethod
    def _norm_shift(cls, v: Any) -> Optional[str]:
        if v is None or v == "":
            return None
        s = str(v).strip().upper()
        if s in {"D", "DAY", "DAYS", "MANANA", "DITËS", "DITES"}:
            return "DAY"
        if s in {"N", "NIGHT", "NOCHE", "SONTE", "NATËS", "NATES"}:
            return "NIGHT"
        return s if s in {"DAY", "NIGHT"} else None

    @field_validator("event_date", mode="before")
    @classmethod
    def _parse_date(cls, v: Any) -> date:
        if isinstance(v, date) and not isinstance(v, datetime):
            return v
        if isinstance(v, datetime):
            return v.date()
        text = str(v).strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
            try:
                return datetime.strptime(text[:10] if fmt.startswith("%Y-%m") and len(text) >= 10 and text[4] == "-" else text, fmt).date()
            except ValueError:
                continue
        # last resort ISO
        return date.fromisoformat(text[:10])

    @field_validator("notice_time", mode="before")
    @classmethod
    def _parse_notice(cls, v: Any) -> Optional[datetime]:
        if v is None or v == "":
            return None
        if isinstance(v, datetime):
            return v
        text = str(v).strip()
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y %H:%M",
            "%m/%d/%y %H:%M",
            "%m/%d %H:%M",
        ):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None


@dataclass
class ExtractedCallout:
    employee_hint: str
    event_date: date
    shift: Optional[str]
    reason_category: str
    notice_time: Optional[datetime]
    source: str
    source_ref: str
    confidence: float
    building_hint: Optional[str] = None
    is_absence: bool = True
    raw_text: str = ""
    raw_summary: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["event_date"] = self.event_date.isoformat()
        d["notice_time"] = self.notice_time.isoformat() if self.notice_time else None
        return d


# ---------------------------------------------------------------------------
# Heuristic patterns
# ---------------------------------------------------------------------------

_SICK_RE = re.compile(
    r"\b(sick|ill|fiebre|fever|temperatur[eë]|not feeling well|no\s+puedo|nuk\s+vij|"
    r"won'?t be in|out\s+tonight|out\s+today|cannot\s+come|can'?t\s+come)\b",
    re.I,
)
_FAMILY_RE = re.compile(
    r"\b(hijo|hija|kid|child|niñ[oa]|f[eë]mija|son|daughter|family|sitter|"
    r"no sitter|family emergency|familia)\b",
    re.I,
)
_CLINIC_RE = re.compile(
    r"\b(clinic|doctor|dr\.|appointment|cita m[eé]dica|medical appointment)\b",
    re.I,
)
_BRV_RE = re.compile(
    r"\b(bereavement|funeral|passed away|died|death|funeral|velorio)\b",
    re.I,
)
_JURY_RE = re.compile(r"\b(jury(\s+duty)?|summons)\b", re.I)
_PERSONAL_RE = re.compile(r"\b(personal(\s+day)?|errand)\b", re.I)
_VACATION_RE = re.compile(r"\b(vacation|pto|time\s+off|planned\s+leave)\b", re.I)
_NIGHT_RE = re.compile(
    r"(night|noche|sonte|tonight|turni\s+i\s+nat\w*|turno\s+de\s+noche|\bnights?\b)",
    re.I,
)
_DAY_RE = re.compile(
    r"(day\s+shift|\bdays?\b|mañana|manana|turni\s+i\s+dit\w*|turno\s+de\s+d[ií]a|today)",
    re.I,
)

_BUILDING_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"meridian\s*one", re.I), "Meridian One"),
    (re.compile(r"meridian\s*plaza", re.I), "Meridian Plaza"),
    (re.compile(r"bramwell\s*annex", re.I), "Bramwell Annex"),
    (re.compile(r"bramwell\s*house", re.I), "Bramwell House"),
    (re.compile(r"halcyon(\s*tower)?", re.I), "Halcyon Tower"),
    (re.compile(r"corlett(\s*building)?", re.I), "Corlett Building"),
]

_CHAT_LINE_RE = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{2,4}),\s+(\d{1,2}:\d{2}:\d{2}\s*[AP]M)\]\s+([^:]+):\s*(.*)$",
    re.I,
)

_NON_ABSENCE_CHAT = re.compile(
    r"\b(reminder|key for|lockbox|ot approved|freight elevator|new temp|"
    r"floater|paf signed|floor finish|stop texting|posting\b|got it|thx|ok\b|"
    r"encrypted|created group)\b",
    re.I,
)


def _classify_reason(text: str) -> tuple[str, float]:
    if _VACATION_RE.search(text):
        return "vacation", 0.85
    if _JURY_RE.search(text):
        return "jury", 0.9
    if _BRV_RE.search(text):
        return "bereavement", 0.9
    if _CLINIC_RE.search(text) and not _SICK_RE.search(text):
        return "clinic", 0.85
    if _FAMILY_RE.search(text) and (
        _SICK_RE.search(text) or re.search(r"\b(can'?t|cannot|won'?t|nuk|no puedo)\b", text, re.I)
    ):
        # Family illness / child sick → family emergency, not employee sick
        return "family", 0.88
    if _FAMILY_RE.search(text) and not _SICK_RE.search(text):
        return "family", 0.8
    if _PERSONAL_RE.search(text):
        return "personal", 0.85
    if _SICK_RE.search(text):
        return "sick", 0.85
    if _CLINIC_RE.search(text):
        return "clinic", 0.75
    return "unknown", 0.35


def _detect_shift(text: str) -> Optional[str]:
    night = bool(_NIGHT_RE.search(text))
    day = bool(_DAY_RE.search(text))
    if night and not day:
        return "NIGHT"
    if day and not night:
        return "DAY"
    if night and day:
        # "won't be in today" + night building context — prefer night if tonight
        if re.search(r"tonight|sonte|noche|night", text, re.I):
            return "NIGHT"
        return "DAY"
    return None


def _detect_building(text: str) -> Optional[str]:
    for pattern, name in _BUILDING_PATTERNS:
        if pattern.search(text):
            return name
    return None


def _parse_reception_datetime(
    row_date: Any,
    row_time: Any,
    vm_received: Any,
    channel: Any,
) -> tuple[Optional[date], Optional[datetime], str]:
    """
    Return (event_date, notice_time, notice_source_note).

    Voicemails use VM Received for notice; live phone uses Date+Time.
    """
    event_date: Optional[date] = None
    if isinstance(row_date, datetime):
        event_date = row_date.date()
    elif isinstance(row_date, date):
        event_date = row_date
    elif row_date:
        text = str(row_date).strip()
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                event_date = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue

    channel_s = str(channel or "").strip().lower()
    notice: Optional[datetime] = None

    if "voicemail" in channel_s or "vm" in channel_s:
        notice = _parse_vm_received(vm_received, event_date)
        return event_date, notice, "vm_received"

    # Live phone / other: Date + Time from log
    t: Optional[time] = None
    if isinstance(row_time, datetime):
        t = row_time.time()
    elif isinstance(row_time, time):
        t = row_time
    elif row_time:
        text = str(row_time).strip()
        for fmt in ("%I:%M %p", "%H:%M", "%I:%M:%S %p", "%H:%M:%S"):
            try:
                t = datetime.strptime(text, fmt).time()
                break
            except ValueError:
                continue
    if event_date and t:
        notice = datetime.combine(event_date, t)
    return event_date, notice, "log_timestamp"


def _parse_vm_received(vm_received: Any, fallback_date: Optional[date]) -> Optional[datetime]:
    if vm_received is None or vm_received == "":
        return None
    if isinstance(vm_received, datetime):
        return vm_received
    text = str(vm_received).strip()
    # Formats seen: "10/02 00:21", "10/05 22:00"
    for fmt in ("%m/%d %H:%M", "%m/%d/%Y %H:%M", "%m/%d/%y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.year == 1900 and fallback_date is not None:
                # year missing — use 2026, adjust if VM is previous evening
                dt = dt.replace(year=2026)
                # If VM month/day after log date month wrap, still fine for Oct
            elif dt.year == 1900:
                dt = dt.replace(year=2026)
            return dt
        except ValueError:
            continue
    return None


def _is_absence_message(text: str, reason: str) -> bool:
    if reason == "vacation":
        return False  # planned vacation is NOT a call-out
    if reason in {"sick", "clinic", "bereavement", "jury", "family", "personal"}:
        return True
    if _NON_ABSENCE_CHAT.search(text) and not _SICK_RE.search(text):
        return False
    return bool(_SICK_RE.search(text) or _FAMILY_RE.search(text) or _JURY_RE.search(text))


def extract_reception_log(path: Path) -> list[ExtractedCallout]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    header_index = {h.lower(): i for i, h in enumerate(headers)}

    def col(*names: str) -> Optional[int]:
        for n in names:
            if n.lower() in header_index:
                return header_index[n.lower()]
        return None

    i_date = col("Date")
    i_time = col("Time")
    i_channel = col("Channel")
    i_vm = col("VM Received")
    i_caller = col("Caller")
    i_msg = col("Message")

    results: list[ExtractedCallout] = []
    for idx, row in enumerate(rows[1:], start=2):  # 1-based sheet row; header is 1
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue

        def cell(i: Optional[int]) -> Any:
            if i is None or i >= len(row):
                return None
            return row[i]

        caller = str(cell(i_caller) or "").strip()
        message = str(cell(i_msg) or "").strip()
        channel = cell(i_channel)
        text = f"{caller}. {message}".strip()
        event_date, notice, _note = _parse_reception_datetime(
            cell(i_date), cell(i_time), cell(i_vm), channel
        )
        reason, conf = _classify_reason(text)
        shift = _detect_shift(text)
        building = _detect_building(text)
        is_abs = _is_absence_message(text, reason)

        # For voicemail left previous evening about "tomorrow", bump event_date
        if notice and event_date and "voicemail" in str(channel or "").lower():
            if re.search(r"\b(nes[eë]r|mañana|manana|tomorrow)\b", message, re.I):
                # Reception logs the transcription date; absence is often next day
                if notice.date() < event_date:
                    pass  # already next morning transcription for prior-night VM
                elif notice.date() == event_date and notice.hour >= 16:
                    event_date = event_date + timedelta(days=1)

        if event_date is None:
            continue

        results.append(
            ExtractedCallout(
                employee_hint=caller or "unknown",
                event_date=event_date,
                shift=shift,
                reason_category=reason,
                notice_time=notice,
                source="reception_log",
                source_ref=str(idx),  # spreadsheet row number
                confidence=conf if is_abs else min(conf, 0.4),
                building_hint=building,
                is_absence=is_abs,
                raw_text=message,
                raw_summary=text[:500],
                meta={"channel": str(channel or ""), "caller": caller},
            )
        )
    return results


def extract_chat_export(path: Path) -> list[ExtractedCallout]:
    text = path.read_text(encoding="utf-8", errors="replace")
    results: list[ExtractedCallout] = []
    for line in text.splitlines():
        m = _CHAT_LINE_RE.match(line.strip())
        if not m:
            continue
        date_s, time_s, sender, body = m.groups()
        # Skip system / managerial chatter without absences
        reason, conf = _classify_reason(body)
        if not _is_absence_message(body, reason):
            continue
        # Extract employee hint from body (first name-like token before "texted"/"out")
        emp_hint = _chat_employee_hint(body, sender)
        try:
            if len(date_s.split("/")[-1]) == 2:
                dt = datetime.strptime(f"{date_s} {time_s}", "%m/%d/%y %I:%M:%S %p")
            else:
                dt = datetime.strptime(f"{date_s} {time_s}", "%m/%d/%Y %I:%M:%S %p")
        except ValueError:
            continue
        # Normalize 2-digit year: 26 → 2026
        if dt.year < 100:
            dt = dt.replace(year=2000 + dt.year)
        elif dt.year < 2000:
            dt = dt.replace(year=dt.year + 2000)

        event_date = dt.date()
        # "out tonight" → same calendar date; morning messages about tonight stay same day
        shift = _detect_shift(body) or ("NIGHT" if "tonight" in body.lower() else None)
        building = _detect_building(body)

        source_ref = dt.strftime("%Y-%m-%dT%H:%M:%S")
        results.append(
            ExtractedCallout(
                employee_hint=emp_hint,
                event_date=event_date,
                shift=shift,
                reason_category=reason,
                notice_time=dt,
                source="area_manager_chat",
                source_ref=source_ref,
                confidence=conf * 0.95,
                building_hint=building,
                is_absence=True,
                raw_text=body,
                raw_summary=body[:500],
                meta={"sender": sender.strip()},
            )
        )
    return results


def _chat_employee_hint(body: str, sender: str) -> str:
    patterns = [
        r"^([A-Z][a-z]+(?:\s+[A-Z]\.?)?)\s+just texted",
        r"^([A-Z][a-z]+(?:\s+[A-Z]\.?)?)\s+texted",
        r"fyi\s+([A-Z][a-z]+)\s+out",
        r"^([A-Z][a-z]+)\s+out\s+",
    ]
    for pat in patterns:
        m = re.search(pat, body.strip(), re.I)
        if m:
            return m.group(1).strip()
    # fallback: first two capitalized tokens
    tokens = re.findall(r"[A-Z][a-z]+", body)
    if tokens:
        return " ".join(tokens[:2])
    return sender.strip()


def _normalize_hint(name: str) -> str:
    name = name.lower().strip()
    name = re.sub(r"[^a-z\s]", "", name)
    parts = name.split()
    if len(parts) >= 2:
        return f"{parts[0]}|{parts[-1][0]}"  # first + last initial
    return parts[0] if parts else name


def deduplicate_callouts(items: Iterable[ExtractedCallout]) -> list[ExtractedCallout]:
    """
    One call-out per employee+date across channels.

    Prefer reception_log when both channels report the same absence; keep the
    higher-confidence record and merge meta.
    """
    best: dict[tuple[str, date], ExtractedCallout] = {}
    order_pref = {"reception_log": 1, "area_manager_chat": 0}

    for item in items:
        if not item.is_absence or item.reason_category == "vacation":
            # Still keep vacation? No — not a call-out; drop from callout set
            if item.reason_category == "vacation":
                item.is_absence = False
            if not item.is_absence:
                # retain non-absences separately keyed so callers can filter
                key = (_normalize_hint(item.employee_hint) + "|na", item.event_date)
            else:
                key = (_normalize_hint(item.employee_hint), item.event_date)
        else:
            key = (_normalize_hint(item.employee_hint), item.event_date)

        if not item.is_absence:
            # Don't dedupe non-absences into absence slots
            continue

        existing = best.get(key)
        if existing is None:
            best[key] = item
            continue

        # Prefer reception; else higher confidence
        pref_new = order_pref.get(item.source, 0)
        pref_old = order_pref.get(existing.source, 0)
        take_new = False
        if pref_new > pref_old:
            take_new = True
        elif pref_new == pref_old and item.confidence > existing.confidence:
            take_new = True

        if take_new:
            item.meta = {
                **item.meta,
                "duplicate_of": {
                    "source": existing.source,
                    "source_ref": existing.source_ref,
                },
            }
            # Preserve earlier notice if newer channel is later
            if existing.notice_time and (
                item.notice_time is None or existing.notice_time < item.notice_time
            ):
                item.meta["earliest_notice"] = existing.notice_time.isoformat()
                # Notice for coding should be earliest notice across channels
                item.notice_time = existing.notice_time
            best[key] = item
        else:
            existing.meta = {
                **existing.meta,
                "also_reported": {
                    "source": item.source,
                    "source_ref": item.source_ref,
                },
            }
            if item.notice_time and (
                existing.notice_time is None or item.notice_time < existing.notice_time
            ):
                existing.notice_time = item.notice_time

    return sorted(best.values(), key=lambda c: (c.event_date, c.source_ref))


def _llm_extract_batch(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Call OpenAI or Anthropic to structure call-outs; returns list of dicts."""
    from app.llm import chat_text, parse_json_array

    schema_text = json.dumps(EXTRACTION_SCHEMA)
    prompt = (
        "Extract call-out absences from each record. Return a JSON array of objects "
        "matching this schema (one object per absence; omit non-absences and planned vacation):\n"
        f"{schema_text}\n\n"
        "Records:\n"
        f"{json.dumps(raw_items, default=str)[:100000]}"
    )
    content = chat_text(prompt, max_tokens=8192)
    try:
        return parse_json_array(content)
    except ValueError:
        logger.warning("LLM response missing JSON array")
        return []


def validate_extraction(data: dict[str, Any], *, source: str, source_ref: str) -> ExtractedCallout:
    payload = {**data, "source": source, "source_ref": source_ref}
    model = CalloutExtractionModel.model_validate(payload)
    return ExtractedCallout(
        employee_hint=model.employee_hint,
        event_date=model.event_date,
        shift=model.shift,
        reason_category=model.reason_category,
        notice_time=model.notice_time,
        source=model.source,
        source_ref=model.source_ref or source_ref,
        confidence=float(model.confidence),
        building_hint=model.building_hint,
        is_absence=model.is_absence and model.reason_category != "vacation",
        raw_summary=model.raw_summary,
    )


def extract_callouts(
    *,
    reception_path: Path | None = None,
    chat_path: Path | None = None,
    use_llm: bool | None = None,
) -> list[ExtractedCallout]:
    """
    Extract + validate + dedupe call-outs from reception log and chat.

    Uses OpenAI (OPENAI_API_KEY) or Anthropic (ANTHROPIC_API_KEY) when set;
    otherwise falls back to heuristic regex/rules.
    """
    from app.llm import resolve_provider

    settings = get_settings()
    data_dir = settings.data_dir
    reception_path = reception_path or (data_dir / "callouts" / "callout_log_october_2026.xlsx")
    chat_path = chat_path or (data_dir / "callouts" / "night_ops_group_chat_export.txt")

    heuristic: list[ExtractedCallout] = []
    if reception_path.exists():
        heuristic.extend(extract_reception_log(reception_path))
    else:
        logger.warning("Reception log not found: %s", reception_path)
    if chat_path.exists():
        heuristic.extend(extract_chat_export(chat_path))
    else:
        logger.warning("Chat export not found: %s", chat_path)

    provider, _api_key = resolve_provider(settings)
    should_llm = use_llm if use_llm is not None else bool(provider)

    if should_llm and provider:
        raw_items = [
            {
                "source": c.source,
                "source_ref": c.source_ref,
                "raw_text": c.raw_text,
                "raw_summary": c.raw_summary,
                "meta": c.meta,
                "heuristic": c.to_dict(),
            }
            for c in heuristic
        ]
        try:
            llm_rows = _llm_extract_batch(raw_items)
            validated: list[ExtractedCallout] = []
            for row in llm_rows:
                src = row.get("source") or "reception_log"
                ref = str(row.get("source_ref") or "")
                try:
                    validated.append(validate_extraction(row, source=src, source_ref=ref))
                except ValidationError as exc:
                    logger.warning("LLM row failed validation: %s", exc)
            if validated:
                return deduplicate_callouts(validated)
            logger.warning("LLM returned no valid rows; using heuristics")
        except Exception:
            logger.exception("LLM extraction failed; using heuristics")

    return deduplicate_callouts(heuristic)


def load_and_extract(data_dir: Path | None = None) -> list[ExtractedCallout]:
    settings = get_settings()
    root = data_dir or settings.data_dir
    return extract_callouts(
        reception_path=root / "callouts" / "callout_log_october_2026.xlsx",
        chat_path=root / "callouts" / "night_ops_group_chat_export.txt",
    )

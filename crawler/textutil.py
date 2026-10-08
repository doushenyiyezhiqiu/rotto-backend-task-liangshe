"""Pure helpers shared by all parsers: HTML->text, salary, work type, dates. No I/O."""
from __future__ import annotations

import html as htmllib
import json
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
              "tr", "table", "section", "article", "header", "footer", "blockquote", "pre"}


def clean(s) -> str | None:
    if s is None:
        return None
    s = htmllib.unescape(str(s))
    s = re.sub(r"[ \t ​]+", " ", s).strip()
    return s or None


def html_to_text(fragment: str | None) -> str | None:
    """HTML fragment -> plain text with paragraph/list structure kept as newlines."""
    if not fragment:
        return None
    if "<" not in fragment:
        text = htmllib.unescape(fragment)
    else:
        soup = BeautifulSoup(fragment, "lxml")
        for t in soup(["script", "style", "noscript", "button", "svg"]):
            t.decompose()
        for t in soup.find_all(True):
            if t.name == "li":
                t.insert_before("\n• ")
            elif t.name in BLOCK_TAGS:
                t.insert_before("\n")
                t.insert_after("\n")
        text = soup.get_text()
    lines = [re.sub(r"[ \t ​]+", " ", ln).strip() for ln in text.splitlines()]
    out, blank = [], False
    for ln in lines:
        if not ln:
            if out and not blank:
                out.append("")
            blank = True
            continue
        out.append(ln)
        blank = False
    text = "\n".join(out).strip()
    return text or None


def normalize_ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------- salary

_PERIOD_WORDS = [
    (r"\b(per\s+)?(annum|year|yr|p\.?\s?a\.?|annual(ly)?|yearly)\b", "year"),
    (r"\b(per\s+)?(day|daily|p\.?\s?d\.?)\b", "day"),
    (r"\b(per\s+)?(hour|hourly|hr|p\.?\s?h\.?)\b", "hour"),
    (r"\b(per\s+)?(month|monthly|pcm|p\.?c\.?m\.?)\b", "month"),
    (r"\b(per\s+)?(week|weekly|pw|p\.?w\.?)\b", "week"),
]
_UNIT_MAP = {"YEAR": "year", "YEARLY": "year", "ANNUAL": "year", "DAY": "day", "DAILY": "day",
             "HOUR": "hour", "HOURLY": "hour", "MONTH": "month", "MONTHLY": "month",
             "WEEK": "week", "WEEKLY": "week"}


def period_from_text(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    for pat, p in _PERIOD_WORDS:
        if re.search(pat, t):
            return p
    return None


def unit_to_period(unit: str | None) -> str | None:
    if not unit:
        return None
    u = str(unit).upper().replace("PER_", "").replace("PER ", "").strip()
    if u in _UNIT_MAP:
        return _UNIT_MAP[u]
    return period_from_text(unit)


def _num(tok: str) -> float | None:
    tok = tok.lower().replace(",", "").strip()
    mult = 1
    if tok.endswith("k"):
        mult, tok = 1000, tok[:-1]
    try:
        return float(tok) * mult
    except ValueError:
        return None


def parse_salary_text(text: str | None) -> tuple[float | None, float | None, str | None]:
    """'£30,000 - £35,000 per annum' -> (30000, 35000, 'year'). Only GBP amounts are trusted."""
    if not text:
        return None, None, None
    t = text.replace("–", "-").replace("—", "-")
    nums = [_num(m) for m in re.findall(r"£\s?(\d[\d,]*(?:\.\d+)?k?)", t, flags=re.I)]
    nums = [n for n in nums if n is not None]
    if not nums:
        return None, None, None
    period = period_from_text(t)
    lo, hi = min(nums[:2]), max(nums[:2])
    if period is None:
        # Heuristic only when the board omits the unit: UK annual salaries are large numbers.
        period = "year" if lo >= 5000 else ("hour" if lo < 100 else "day")
    if len(nums) == 1:
        # "From £27,293 a year" has no maximum; "Up to £40,000" has no minimum.
        if re.search(r"\b(from|starting|minimum|min\.?)\b", t, re.I):
            return finalize_salary(lo, None, period, fill=False)
        if re.search(r"\b(up to|upto|maximum|max\.?)\b", t, re.I):
            return finalize_salary(None, hi, period, fill=False)
    return finalize_salary(lo, hi, period)


def finalize_salary(lo, hi, period, fill: bool = True) -> tuple[float | None, float | None, str | None]:
    """Schema allows year/day/hour. Monthly/weekly figures are annualised (documented tradeoff).
    A single figure fills both bounds unless fill=False (open-ended "from"/"up to" ranges)."""
    lo = float(lo) if lo is not None and float(lo) > 0 else None   # boards use -1/0 for "none"
    hi = float(hi) if hi is not None and float(hi) > 0 else None
    if lo is None and hi is None:
        return None, None, None
    if fill:
        hi = hi if hi is not None else lo
        lo = lo if lo is not None else hi
    mult = {"month": 12, "week": 52}.get(period)
    if mult:
        lo = lo * mult if lo is not None else None
        hi = hi * mult if hi is not None else None
        period = "year"
    if period not in ("year", "day", "hour"):
        period = None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    if lo == 0 and hi == 0:
        return None, None, None
    return _round(lo), _round(hi), period


def _round(x):
    if x is None:
        return None
    return int(x) if float(x).is_integer() else round(float(x), 2)


# ---------------------------------------------------------------- work type

def normalize_work_type(*texts) -> str | None:
    """Map board labels to: full time, part time, contract, temporary (or None).
    When several apply (e.g. 'Contract, full-time') the contract/temporary nature wins,
    because 'full time' is the default and carries less information."""
    t = " ".join(str(x) for x in texts if x).lower().replace("_", " ").replace("-", " ")
    if not t.strip():
        return None
    if re.search(r"\bcontract(or)?\b|\bfreelance\b", t):
        return "contract"
    if re.search(r"\btemp(orary)?\b|\bseasonal\b", t):
        return "temporary"
    if re.search(r"\bpart\s?time\b|\bparttime\b", t):
        return "part time"
    if re.search(r"\bfull\s?time\b|\bfulltime\b", t):
        return "full time"
    return None


# ---------------------------------------------------------------- dates

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_relative_date(text: str | None, now: datetime | None = None) -> datetime | None:
    """'3 days ago', 'Posted 30+ days ago', 'Just posted', '2 weeks ago' -> datetime estimate."""
    if not text:
        return None
    now = now or now_utc()
    t = text.lower()
    if re.search(r"just posted|today|just now|moments? ago|new\b", t):
        return now.replace(minute=0, second=0, microsecond=0)
    if "yesterday" in t:
        return (now - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    m = re.search(r"(\d+)\+?\s*(minute|min|hour|hr|day|week|month|year)s?", t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    delta = {"minute": timedelta(minutes=n), "min": timedelta(minutes=n),
             "hour": timedelta(hours=n), "hr": timedelta(hours=n),
             "day": timedelta(days=n), "week": timedelta(weeks=n),
             "month": timedelta(days=30 * n), "year": timedelta(days=365 * n)}[unit]
    est = now - delta
    if unit in ("day", "week", "month", "year"):
        est = est.replace(minute=0, second=0, microsecond=0)
    return est


def parse_date(value, naive_tz: str | None = None) -> datetime | None:
    """ISO strings, epoch seconds/ms, '/Date(1700000000000)/' -> aware datetime.
    Timestamps without an offset are read in `naive_tz` (e.g. 'Europe/London' for boards that
    print UK local time), else UTC."""
    tz = timezone.utc
    if naive_tz:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(naive_tz)
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v /= 1000
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(value).strip()
    m = re.match(r"/Date\((\d+)", s)
    if m:
        return parse_date(int(m.group(1)))
    if re.fullmatch(r"\d{10,13}", s):
        return parse_date(int(s))
    try:
        if len(s) == 10:  # YYYY-MM-DD
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=tz)
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- embedded JSON

def extract_json_after(text: str, marker: str):
    """Decode the JSON value that starts right after `marker` (e.g. 'window._initialData=')."""
    i = text.find(marker)
    if i < 0:
        return None
    j = i + len(marker)
    while j < len(text) and text[j] in " \t\n=":
        j += 1
    try:
        obj, _ = json.JSONDecoder().raw_decode(text, j)
        return obj
    except json.JSONDecodeError:
        return None


def walk(obj):
    """Yield every dict nested anywhere in a JSON value."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def first(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None

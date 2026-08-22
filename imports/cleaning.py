"""
Shared cleaning / normalization rules for Excel -> CRM migration.

These encode the mapping rules we derived from analyzing the two source
workbooks (ZA Leads Data Till June 2026.xlsx, Academy Leads for Jul-Aug
2026.xlsx) plus the business rules confirmed by the user:
  - "AD" alone (no platform given) = mixed/unspecified Meta+Google paid ads.
  - WhatsApp, when explicitly mentioned, is always its own distinct source
    (never folded into "AD").
Used by both the interactive Import Leads feature and the one-off
scripts/migrate_excel_data.py loader — so ad-hoc imports and the historical
migration behave identically.
"""
import re
from datetime import datetime, date


def clean_phone(raw):
    """Returns (primary, alternate). Handles '.0' floats, spaces, dashes,
    91-country-code prefixes, and two numbers joined by '/'."""
    if raw is None:
        return "", ""
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return "", ""
    parts = re.split(r"[/,]", s)
    cleaned = []
    for p in parts:
        p = p.strip()
        if p.endswith(".0"):
            p = p[:-2]
        digits = re.sub(r"\D", "", p)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == 10:
            cleaned.append(digits)
    if not cleaned:
        return "", ""
    return cleaned[0], (cleaned[1] if len(cleaned) > 1 else "")


def parse_date(raw):
    """Best-effort flexible date parser. Returns a date or None."""
    if raw is None:
        return None
    try:
        import pandas as pd
        if pd.isna(raw):
            return None
    except (ImportError, TypeError, ValueError):
        pass
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "nat", "-"):
        return None
    fmts = ["%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%y"]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


SOURCE_RULES = [
    (r"^(fb|facebook|face\s*book|meta\s*fb)$", ("Digital Marketing", "Facebook")),
    (r"^(ig|insta|instagram|meta\s*ig)$", ("Digital Marketing", "Instagram")),
    (r"^(meta|meta\s*ads)$", ("Digital Marketing", "Meta Ads")),
    (r"^(google|google\s*ads|g\s*ads)$", ("Digital Marketing", "Google Ads")),
    (r"whats\s*app|wts\s*up|wtsup|wa\b", ("Outreach", "WhatsApp")),
    (r"walk[\s\-_]*in|walk", ("Direct", "Walk-in")),
    (r"chatgpt|chat\s*gpt", ("Other", "ChatGPT Referral")),
    (r"website|landing\s*page|site", ("Digital Marketing", "Website")),
    (r"referen|referr|refference|ref\b|doctor\s*ref", ("Referral", "Doctor Referral")),
    (r"inquiry\s*call|phone|cold\s*call|call", ("Direct", "Phone Call")),
    (r"^ad$|^ads$|paid\s*ads", ("Digital Marketing", "Meta Ads")),
]

CANONICAL_SOURCE_MAP = {
    "ig": "Instagram",
    "insta": "Instagram",
    "instagram": "Instagram",
    "fb": "Facebook",
    "facebook": "Facebook",
    "meta": "Meta Ads",
    "meta ads": "Meta Ads",
    "google": "Google Ads",
    "google ads": "Google Ads",
    "gads": "Google Ads",
    "whatsapp": "WhatsApp",
    "wa": "WhatsApp",
    "walk-in": "Walk-in",
    "walkin": "Walk-in",
    "walk in": "Walk-in",
    "website": "Website",
    "doctor referral": "Doctor Referral",
    "referral": "Doctor Referral",
    "campaign": "Campaign",
    "newspaper": "Newspaper",
}


def normalize_source(raw):
    """Returns (source_category_name, lead_source_name, is_ambiguous)."""
    if raw is None:
        return None, None, True
    s = str(raw).strip().lower()
    if not s or s in ("nan", "-", "origin", "ma'am", "none", "null"):
        return None, None, True
        
    # 1. Exact canonical alias check
    if s in CANONICAL_SOURCE_MAP:
        std_name = CANONICAL_SOURCE_MAP[s]
        cat = "Digital Marketing" if std_name in ["Instagram", "Facebook", "Meta Ads", "Google Ads", "Website"] else ("Outreach" if std_name == "WhatsApp" else ("Direct" if std_name == "Walk-in" else "Referral"))
        return cat, std_name, False

    # 2. Regex rules check
    for pattern, result in SOURCE_RULES:
        if re.search(pattern, s, re.IGNORECASE):
            return result[0], result[1], False
            
    # unknown value — preserve formatted text as the lead source name
    return "Other", str(raw).strip().title()[:100], True


TEMP_RULES = [
    (r"^(avg|average)\s*posi?tive$", "WARM"),
    (r"^posi?tive$", "HOT"),
    (r"medium\s*cold", "COLD"),
    (r"^cold$", "COLD"),
    (r"^hot$", "HOT"),
    (r"^warm$", "WARM"),
    (r"not\s*pick|not\s*receive|switch\s*off|unreachable", "NOT_PICKED"),
    (r"uncontacted|new", "UNCONTACTED"),
]


def normalize_temperature(raw):
    if raw is None:
        return None, True
    s = str(raw).strip().lower()
    if not s or s in ("nan", "-"):
        return None, True
    for pattern, result in TEMP_RULES:
        if re.match(pattern, s):
            return result, False
    # Deal-status words leaking into a "lead state" column — not a temperature at all
    if s in ("open", "closed", "hold", "done"):
        return None, True
    return None, True


# A lead marked "Cold" very often just means the call was never actually
# connected — not that the person is genuinely a cold/uninterested lead.
# These are two different things and get reported separately.
NOT_CONNECTED_PATTERNS = re.compile(
    r"not received|not\s*recdeived|not\s*recieved|not connect|not reach|"
    r"switch(ed)?\s*off|invalid number|number does ?n[o']?t exist|"
    r"out of cover(age)?|line busy|incoming.*not avail|no response|"
    r"not answer(ing)?|not pick|disconnect|call not|busy on another call|"
    r"phone.*off|not available|no answer"
)


def refine_temperature(temperature, comment):
    """If `temperature` came out Cold (or blank) but the follow-up comment
    shows the call was never actually connected, report 'NOT_PICKED'
    instead of Cold — genuine cold/uninterested leads (where a conversation
    happened and the person just wasn't interested) are left as Cold."""
    if comment and NOT_CONNECTED_PATTERNS.search(str(comment).lower()):
        if temperature in ("COLD", "UNCONTACTED", "", None):
            return "NOT_PICKED"
    return temperature or "UNCONTACTED"


def normalize_deal_status(raw, admission_status_raw=""):
    if raw is None:
        return "OPEN"
    s = str(raw).strip().lower()
    if s == "open":
        return "OPEN"
    if s == "hold":
        return "HOLD"
    if s == "done":
        return "WON"
    if s == "closed":
        adm = str(admission_status_raw or "").strip().lower()
        return "WON" if "done" in adm else "LOST"
    return "OPEN"


def normalize_admission_status(raw):
    s = str(raw or "").strip().lower()
    if "done" in s:
        return "ADMISSION_DONE"
    return "NOT_APPLIED"


COURSE_MAP = {
    "data analyst": "Data Analytics", "data analyts": "Data Analytics", "data anlyst": "Data Analytics",
    "data analytcs": "Data Analytics", "data analytics": "Data Analytics", "data analayst/fullstack": "Data Analytics",
    "data science": "Data Science", "data science/full stack": "Data Science",
    "data analytics+ai": "Data Analytics + AI", "data analytics +ai": "Data Analytics + AI",
    "data analyst+ai": "Data Analytics + AI", "data analytics + ai": "Data Analytics + AI",
    "data analytics with ai": "Data Analytics + AI",
    "ai/ml": "AI/ML", "ai": "AI", "ds": "Data Science",
    "python": "Python", "python course": "Python", "advance python": "Advance Python",
    "python-ai": "Python + AI", "python full stack": "Python Full Stack", "python fullstack": "Python Full Stack",
    "full stack": "Full Stack Development", "full stack development": "Full Stack Development",
    "full stack with ai": "Full Stack Development + AI",
    "business analayst": "Business Analyst", "business analyst": "Business Analyst",
    "advance excel": "Advance Excel",
    # website-form slug values (snake_case) — normalized after underscore->space below
    "python programming": "Python", "pyhon programming": "Python",
    "ai & machine learning": "AI/ML", "(ai) & machine learning": "AI/ML",
    "full stack java": "Full Stack Development (Java)", "java+ full stack": "Full Stack Development (Java)",
    "full stack python": "Full Stack Development (Python)",
    "frontend web dewlopmwent": "Frontend Web Development", "web development": "Web Development",
    "digital marketing": "Digital Marketing", "video editing course": "Video Editing",
    "prompt engineering": "Prompt Engineering", "advance ai": "Advance AI",
    "da with ai": "Data Analytics + AI", "da & dm": "Data Analytics + Digital Marketing",
    "data anlytics + ai": "Data Analytics + AI", "data anakytics": "Data Analytics",
    "data anlystics": "Data Analytics", "data anlytics": "Data Analytics",
    "dat analytics": "Data Analytics", "data analyics": "Data Analytics",
    "not sure (need career guidance)": "Not Decided", "course not decided": "Not Decided",
    "course not selected - need counselling": "Not Decided", "not decided": "Not Decided",
    "full stack.": "Full Stack Development",
}


def normalize_course(raw):
    if raw is None:
        return None, True
    s = str(raw).strip()
    if not s or s.lower() == "nan":
        return None, True
    key = s.lower().replace("_", " ").replace("  ", " ").strip()
    if key in COURSE_MAP:
        return COURSE_MAP[key], False
    return s.strip(), True  # unrecognized spelling — keep raw, flag for review


# --- City / location normalization -----------------------------------------
# Built from the actual unique values observed across every sheet in both
# workbooks (city/location columns). Two separate problems, handled
# separately rather than guessed together:
#   1. Same city, different spelling/casing ("Nagpur"/"nagpur"/"NAgpur"/
#      "nagapur"/"nagour"/"Naagpur") -> collapsed to one canonical spelling.
#   2. A Nagpur-city neighbourhood entered alone ("Sadar", "Jaripatka",
#      "Manewada"...) -> split into City="Nagpur" + Area="<neighbourhood>"
#      so it doesn't show up as a fake separate "city".
# Maharashtra's official renames (Aurangabad -> Chhatrapati Sambhaji Nagar,
# Ahmednagar -> Ahilyanagar) are normalized for spelling *within* each name
# but the two names are NOT merged into one — that's a business call the
# data alone can't safely make, so both canonical spellings are kept and
# noted in the review report.

CITY_SPELLING_MAP = {
    # Nagpur
    "nagpur": "Nagpur", "naagpur": "Nagpur", "nagapur": "Nagpur", "nagour": "Nagpur",
    "ngapur": "Nagpur", "ngapur, khanmti": "Nagpur",
    # Amravati
    "amravati": "Amravati", "amaravati": "Amravati", "amrawati": "Amravati",
    # Yavatmal
    "yavatmal": "Yavatmal", "yavtamal": "Yavatmal", "yawtmal": "Yavatmal", "yawtamal": "Yavatmal",
    # Gondia
    "gondia": "Gondia", "gondhia": "Gondia", "gondiya": "Gondia",
    # Latur
    "latur": "Latur", "lathur": "Latur",
    # Dhule
    "dhule": "Dhule", "dhulia": "Dhule", "dhulai": "Dhule",
    # Jalgaon
    "jalgaon": "Jalgaon", "jalagaon": "Jalgaon",
    # Nanded
    "nanded": "Nanded", "naned": "Nanded", "nandel": "Nanded",
    # Others — spelling/casing only
    "chandrapur": "Chandrapur", "wardha": "Wardha", "bhandara": "Bhandara",
    "akola": "Akola", "washim": "Washim", "wasim": "Washim", "buldhana": "Buldhana",
    "nandurbar": "Nandurbar", "kolhapur": "Kolhapur", "kalhapur": "Kolhapur",
    "pune": "Pune", "sangli": "Sangli", "satara": "Satara", "nashik": "Nashik",
    "gadchiroli": "Gadchiroli", "mumbai": "Mumbai", "jalna": "Jalna", "parbhani": "Parbhani",
    "shirdy": "Shirdi", "shirdi": "Shirdi", "buldhana- jalna": "Buldhana",
    "usmanabad": "Dharashiv (Osmanabad)", "sirpur- dule": "Dhule",
    # Aurangabad <-> Chhatrapati Sambhaji Nagar cluster (spelling-only, not merged)
    "chattrapati sambhaji nagar": "Chhatrapati Sambhaji Nagar",
    "chattrapati sambhaji naga": "Chhatrapati Sambhaji Nagar",
    "chhatrapati sambhaji nagar": "Chhatrapati Sambhaji Nagar",
    "sambaji nagar": "Chhatrapati Sambhaji Nagar", "sambhaji nagar": "Chhatrapati Sambhaji Nagar",
    "aurangabad": "Aurangabad",
    # Ahmednagar <-> Ahilyanagar cluster (spelling-only, not merged)
    "ahilya nagar": "Ahilyanagar", "ahmad nagar": "Ahmednagar",
    # Nagpur-district towns (own town, not a Nagpur-city neighbourhood — kept distinct)
    "kamptee": "Kamptee", "katol": "Katol", "umred": "Umred", "umreed": "Umred",
    "buttibori": "Butibori", "butibori": "Butibori", "ramtek": "Ramtek",
    "saoner": "Saoner", "savner": "Saoner", "kalmeshwar": "Kalmeshwar",
    # Out of state — spelling/casing only
    "indore": "Indore", "jabalpur": "Jabalpur", "jamsedhpur": "Jamshedpur",
    "jamnagar": "Jamnagar",
    # A few more high-confidence single-letter-typo merges
    "shegao": "Shegaon", "shegaon": "Shegaon",
    "male gaon": "Malegaon", "malegaon": "Malegaon",
    "ballarsha (chandrapur)": "Ballarshah", "ballarsha": "Ballarshah", "ballarshah": "Ballarshah",
    # correctly-spelled real towns, just missing from the map (identity — stops needless flagging)
    "warud": "Warud", "tiroda": "Tiroda", "shirpur": "Shirpur",
}

# A bare neighbourhood name (no "Nagpur" mentioned) that is unambiguously
# inside Nagpur city -> split into City=Nagpur, Area=<canonical locality>.
NAGPUR_LOCALITY_MAP = {
    "sadar": "Sadar", "jaripatka": "Jaripatka", "jaripatna": "Jaripatka",
    "manewada": "Manewada", "hudkeshwar": "Hudkeshwar", "sakardhara": "Sakardhara",
    "tarodi": "Tarodi", "mahal": "Mahal", "mahal & (it park)": "Mahal / IT Park",
    "narendra nagar": "Narendra Nagar", "medical square": "Medical Square",
    "kamal chaouk": "Kamal Chowk", "koradi": "Koradi", "pardi": "Pardi",
    "pardy": "Pardi", "pratap nagar": "Pratap Nagar", "digohari": "Digohari",
    "jinabai tekdi": "Jinabai Tekdi", "subhash nagar": "Subhash Nagar",
    "anand nagar": "Anand Nagar", "khapar kheda": "Khapri / Khapar Kheda",
    "teka naka": "Teka Naka",
}

# Strip a parenthetical / comma / slash suffix that names a Nagpur locality
# inside an already-explicit "Nagpur" value, e.g. "Nagpur(sadar)",
# "Nagpur ,sadar", "Nagpur digohari", "Nagpur/khapar kheda".
_NAGPUR_SUFFIX_RE = re.compile(
    r"^nagpur\s*[\(,/\-]?\s*(sadar|jaripatka|shanti nagar|anand nagar|digohari|"
    r"jinabai tekdi|subhash nagar|jaripatka\)?|sadar\)?|khapar kheda|nandanwan|"
    r"working professionals)?\)?\s*$"
)


def normalize_city(raw):
    """Returns (city, area, is_ambiguous).

    `area` is populated only when a Nagpur-city neighbourhood was detected
    (either standalone, or appended to an explicit "Nagpur..." value).
    Genuinely compound values ("Nagpur/Chandrapur", "Jalna-Buldhana") or
    state-only entries ("UP", "Bihar") are returned unmapped with
    is_ambiguous=True instead of guessing which part is the real city.
    """
    if raw is None:
        return None, "", True
    s = str(raw).strip()
    if not s or s.lower() in ("nan", "-", "city"):
        return None, "", True
    s = re.sub(r"\s+", " ", s)
    key = s.lower().strip()

    # 1. Direct known spelling/casing fix
    if key in CITY_SPELLING_MAP:
        return CITY_SPELLING_MAP[key], "", False

    # 2. Bare Nagpur-locality name entered alone
    if key in NAGPUR_LOCALITY_MAP:
        return "Nagpur", NAGPUR_LOCALITY_MAP[key], False

    # 3. "Nagpur" + a locality suffix in the same cell
    if key.startswith("nagpur") and len(key) > 6:
        remainder = key[6:].strip(" ,()/-")
        for loc_key, loc_name in NAGPUR_LOCALITY_MAP.items():
            if loc_key in remainder:
                return "Nagpur", loc_name, False
        # "Nagpur" plus *something* we don't recognize — still safe to call it
        # Nagpur, just keep the raw remainder as the area for manual review.
        if remainder:
            return "Nagpur", remainder.title(), True
        return "Nagpur", "", False

    # 4. State names / obviously compound two-place values -> flag, don't guess
    if key in ("up", "u.p.", "bihar", "karnatak"):
        return s.title(), "", True
    if re.search(r"[/,\-]", s) or " and " in key:
        return s.title(), "", True

    # 5. Unrecognized single value — clean casing, flag for review
    return s.title(), "", True

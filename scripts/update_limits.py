#!/usr/bin/env python3
"""Adds each new year's contribution limits to limits.json (run daily by .github/workflows/update-limits.yml).

No AI, no paid service. Every number is read by fixed rules from an official page, and cross-checked:
  * Canada (TFSA, RRSP): the CRA's yearly limits table.
  * US retirement (IRA, 401(k), SIMPLE, SEP, 457(b)): the IRS "COLA increases" page, which has one table column per year,
    plus the one sentence on each page that states the 60-63 "super" catch-up.
  * US health and gift (HSA, health FSA, gift exclusion): one sentence or table row each, from IRS Publication 969 and the
    IRS gift-tax page.
  * Tax brackets for the refund estimate (the "tax" section): CRA payroll tables, the CRA indexation page and the IRS
    inflation-adjustment release. Details and the Quebec exception are in the "Tax brackets" section below.
How it stays safe
  * A new year is only created when every REQUIRED field is found. Missing or reworded pages mean "not ready", never a guess.
  * Numbers that must agree (457(b) = 401(k), 415(c) limit = SEP) are checked against each other, and every number must be a
    plausible step from last year, or nothing is published.
  * Optional fields (HSA, health FSA, gift exclusion) are carried forward from last year if their page has not been updated
    yet; they are listed under "carried" in limits.json and retried on every run. A GitHub issue is opened so you know.
  * Fixed-by-law limits (FHSA, RESP, RDSP, Coverdell, dependent-care FSA, catch-up ages) are copied forward unchanged.
  * If the new year is still missing on January 15 of that year, the run fails and GitHub emails you.

Exit codes: 0 = nothing to do, not published yet, or updated.  1 = something is wrong and a person should look.
"""
import copy
import datetime
import html.parser
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIMITS_PATH = os.path.join(ROOT, "limits.json")
REPORT_PATH = os.path.join(ROOT, ".limits-report.md")

# Official pages. A required page that fails to load stops the run (and is reported); optional ones are treated as "not yet".
PAGES = {
    "cra": "https://www.canada.ca/en/revenue-agency/services/tax/registered-plans-administrators/pspa/mp-rrsp-dpsp-tfsa-limits-ympe.html",
    "irs_cola": "https://www.irs.gov/retirement-plans/cola-increases-for-dollar-limitations-on-benefits-and-contributions",
    "irs_simple": "https://www.irs.gov/retirement-plans/plan-participant-employee/retirement-topics-simple-ira-contribution-limits",
    "irs_p969": "https://www.irs.gov/publications/p969",
    "irs_gift": "https://www.irs.gov/businesses/small-businesses-self-employed/frequently-asked-questions-on-gift-taxes",
}
OPTIONAL_PAGES = {"irs_p969", "irs_gift"}

# key -> (account, json field, required)
FIELDS = {
    "ira_annual": ("IRA", "annualCents", True),
    "ira_catchup": ("IRA", "catchUpCents", True),
    "k401_annual": ("K401", "annualCents", True),
    "k401_catchup": ("K401", "catchUpCents", True),
    "k401_super_catchup": ("K401", "superCatchUpCents", True),
    "simple_annual": ("SIMPLE", "annualCents", True),
    "simple_catchup": ("SIMPLE", "catchUpCents", True),
    "simple_super_catchup": ("SIMPLE", "superCatchUpCents", True),
    "sep_annual": ("SEP", "annualCents", True),
    "hsa_self": ("HSA", "annualCents", False),
    "hsa_family": ("HSA", "familyCents", False),
    "fsa_health": ("FSA", "annualCents", False),
    "gift_exclusion": ("P529", "annualCents", False),
}
DESCRIPTIONS = {
    "ira_annual": "IRA contribution limit",
    "ira_catchup": "IRA catch-up (age 50+)",
    "k401_annual": "401(k)/403(b)/TSP/457(b) deferral limit",
    "k401_catchup": "401(k) catch-up (age 50+)",
    "k401_super_catchup": "401(k) catch-up for ages 60 to 63",
    "simple_annual": "SIMPLE IRA deferral limit",
    "simple_catchup": "SIMPLE IRA catch-up (age 50+)",
    "simple_super_catchup": "SIMPLE IRA catch-up for ages 60 to 63",
    "sep_annual": "SEP IRA / Solo 401(k) total limit",
    "hsa_self": "HSA limit, self-only",
    "hsa_family": "HSA limit, family",
    "fsa_health": "Health FSA salary reduction limit",
    "gift_exclusion": "Annual gift tax exclusion",
}
PLAUSIBLE = (0.85, 1.6)  # new value / last year's value


def say(message, level="notice"):
    """Prints a line. On GitHub it also shows as an annotation at the top of the run page."""
    print(message)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level}::{message}")


class NotReady(Exception):
    """The official sources do not show the new year yet. Normal; try again tomorrow."""


class Failure(Exception):
    """Something is wrong; stop without publishing and tell a person."""


# ---------------------------------------------------------------- HTML helpers

class _Page(html.parser.HTMLParser):
    """Collects visible text and every table as a list of rows of cell texts."""

    SKIP = {"script", "style", "nav", "header", "footer", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.tables = []
        self._skip = 0
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
            self.tables[-1].append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag in ("p", "li", "br", "div", "h1", "h2", "h3", "h4", "tr"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
            self.text.append(" ")
        elif tag == "tr":
            self._row = None

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._cell is not None:
            self._cell.append(data)


def parse_html(markup):
    p = _Page()
    p.feed(markup)
    return re.sub(r"[ \t\r\f\v]+", " ", "".join(p.text)), p.tables


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ContributionRoomLimitsBot/1.0 (+https://contributionroom.wealthboardapp.com)"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(5 * (attempt + 1))
    raise OSError(f"{url}: {last}")


def dollars(cell):
    m = re.search(r"\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", cell or "")
    return int(round(float(m.group(1).replace(",", "")))) if m else None


# ---------------------------------------------------------------- Canada (CRA table, no AI)

def cra_limits(tables, year):
    """Returns {'TFSA': dollars, 'RRSP': dollars}. Raises Failure if the table layout changed, NotReady if the year is absent."""
    out = {}
    for name in ("RRSP", "TFSA"):
        found_table = False
        for rows in tables:
            header = next((r for r in rows if r and re.search(r"year", r[0], re.I) and any(name in c.upper() for c in r)), None)
            if header is None:
                continue
            found_table = True
            col = next(i for i, c in enumerate(header) if name in c.upper() and i > 0)
            row = next((r for r in rows if r and r[0].strip() == str(year)), None)
            if row is None or col >= len(row):
                break
            v = dollars(row[col])
            if v:
                out[name] = v
            break
        if not found_table:
            raise Failure(f"CRA page: could not find the {name} table. The page layout probably changed.")
    if len(out) < 2:
        raise NotReady(f"CRA table has no {year} row for {', '.join(n for n in ('RRSP', 'TFSA') if n not in out)} yet.")
    return out


# ---------------------------------------------------------------- US (fixed rules)

def load_page(name):
    """Returns (text, tables) for a named official page. Tests replace this with saved copies."""
    try:
        return parse_html(fetch(PAGES[name]))
    except OSError:
        if name in OPTIONAL_PAGES:
            say(f"Could not load {PAGES[name]}", "warning")
            return "", []
        raise


def table_value(tables, section, label, year):
    """Value in the table whose first header cell matches `section`, row matching `label`, column headed `year`.
    None when that table has no column for the year yet. Failure when the table or row is missing (layout changed)."""
    table = next((t for t in tables if t and t[0] and re.search(section, t[0][0])), None)
    if table is None:
        raise Failure(f"IRS page: table '{section}' not found. The page layout probably changed.")
    header = table[0]
    if str(year) not in header:
        return None
    col = header.index(str(year))
    row = next((r for r in table[1:] if r and re.search(label, r[0])), None)
    if row is None or col >= len(row):
        raise Failure(f"IRS page: row '{label}' not found in table '{section}'. The page layout probably changed.")
    return dollars(row[col])


def sentence_value(text, pattern, year, groups=1):
    m = re.search(pattern.replace("{year}", str(year)), text, re.I)
    if not m:
        return None
    vals = [int(m.group(i).replace(",", "")) for i in range(1, groups + 1)]
    return vals if groups > 1 else vals[0]


def us_limits(pages, year):
    """pages: {name: (text, tables)}. Returns ({key: dollars}, [required keys not found]). Raises Failure on a layout change
    or when numbers that must agree do not."""
    cola_text, cola = pages["irs_cola"]
    found = {}

    def put(key, value):
        if value:
            found[key] = value

    put("ira_annual", table_value(cola, r"^IRAs", r"^IRA contribution limit", year))
    put("ira_catchup", table_value(cola, r"^IRAs", r"^IRA catch-up", year))
    put("k401_annual", table_value(cola, r"^401\(k\)", r"^Elective deferrals", year))
    put("k401_catchup", table_value(cola, r"^401\(k\)", r"^Catch-up contributions", year))
    put("simple_annual", table_value(cola, r"^SIMPLE", r"^SIMPLE maximum contributions", year))
    put("simple_catchup", table_value(cola, r"^SIMPLE", r"^Catch-up contributions", year))
    put("sep_annual", table_value(cola, r"^SEP$", r"^SEP maximum contribution", year))
    put("k401_super_catchup", sentence_value(
        cola_text, r"these plans\. For {year}, this higher catch-up contribution limit is \$([\d,]+)", year))
    put("simple_super_catchup", sentence_value(
        pages["irs_simple"][0], r"For {year}, this higher catch-up contribution limit is \$([\d,]+)", year))

    # Numbers that must agree with each other.
    k457 = table_value(cola, r"^Other", r"^457 elective deferrals", year)
    if k457 and found.get("k401_annual") and k457 != found["k401_annual"]:
        raise Failure(f"{year}: 457(b) limit {k457:,} differs from the 401(k) limit {found['k401_annual']:,}.")
    dc = table_value(cola, r"^401\(k\)", r"^Defined contribution plan limit", year)
    if dc and found.get("sep_annual") and dc != found["sep_annual"]:
        raise Failure(f"{year}: SEP limit {found['sep_annual']:,} differs from the 415(c) limit {dc:,}.")

    # Optional: health and gift.
    hsa = sentence_value(
        pages["irs_p969"][0],
        r"For {year}, if you have self-only HDHP coverage, you can contribute up to \$([\d,]+)\. "
        r"If you have family HDHP coverage, you can contribute up to \$([\d,]+)", year, groups=2)
    if hsa:
        found["hsa_self"], found["hsa_family"] = hsa
    put("fsa_health", sentence_value(
        pages["irs_p969"][0],
        r"for tax years beginning in {year}, the dollar limitation under (?:Code )?section 125\(i\)[^$]{0,200}?"
        r"Health Flexible Spending Arrangements is \$([\d,]+)", year))
    for t in pages["irs_gift"][1]:
        if t and t[0] and t[0][0] == "Year of gift" and any("exclusion per donee" in c for c in t[0]):
            row = next((r for r in t[1:] if r and r[0].strip() == str(year)), None)
            if row and len(row) > 1:
                put("gift_exclusion", dollars(row[1]))
            break

    missing = [k for k, f in FIELDS.items() if f[2] and k not in found]
    return found, missing


def load_us_pages():
    return {name: load_page(name) for name in PAGES if name != "cra"}


# ---------------------------------------------------------------- merging

def derive(entry):
    """Accounts that always equal another account's numbers."""
    k401, sep, p529 = entry["K401"], entry["SEP"], entry["P529"]
    for acct in ("K457", "SOLO401K"):
        for f in ("catchUpCents", "superCatchUpCents"):
            if f in k401:
                entry[acct][f] = k401[f]
    entry["K457"]["annualCents"] = k401["annualCents"]
    entry["SOLO401K"]["annualCents"] = sep["annualCents"]
    entry["ABLE"]["annualCents"] = p529["annualCents"]


def check_plausible(label, new_cents, old_cents):
    if old_cents and not (PLAUSIBLE[0] <= new_cents / old_cents <= PLAUSIBLE[1]):
        raise Failure(f"{label}: {new_cents / 100:,.0f} versus last year's {old_cents / 100:,.0f} looks wrong. Not publishing.")


def apply_values(entry, prev_entry, cents_by_field, label_year):
    for (acct, field), cents in cents_by_field.items():
        check_plausible(f"{label_year} {acct}.{field}", cents, prev_entry.get(acct, {}).get(field))
        entry.setdefault(acct, {})[field] = cents


def run(today=None, dry_run=False):
    today = today or datetime.date.today()
    with open(LIMITS_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    years = sorted(int(y) for y in data["years"])
    latest = years[-1]
    carried = data.setdefault("carried", {})
    changed = False
    log = []

    # 1) Create the next year when it is due.
    target = latest + 1
    if target <= today.year + 1 and today >= datetime.date(target - 1, 9, 1):
        try:
            cents = gather_new(data, target)
            entry, missing_opt = build_entry(data, target, cents)
            data["years"][str(target)] = entry
            if missing_opt:
                carried[str(target)] = missing_opt
            data["updated"] = today.isoformat()
            data.setdefault("sources", []).append(
                f"{target}: CRA limits table and IRS pages, read automatically on {today.isoformat()}")
            changed = True
            log.append(f"Published {target} limits.")
            if missing_opt:
                log.append(f"Carried forward from {target - 1} (not announced yet): {', '.join(missing_opt)}")
        except NotReady as e:
            log.append(f"{target} not ready yet: {e}")
            if target <= today.year and today >= datetime.date(target, 1, 15):
                raise Failure(f"{target} limits are still not published and it is already {today.isoformat()}. "
                              "Apps are showing last year's limits. Check scripts/update_limits.py and the source pages.")

    # 2) Retry optional fields that were carried forward.
    for y in sorted(list(carried)):
        keys = [k for k in carried[y] if k in FIELDS]
        if not keys:
            carried.pop(y, None)
            continue
        year = int(y)
        found, _ = us_limits(load_us_pages(), year)
        found = {k: v for k, v in found.items() if k in keys}
        if not found:
            continue
        entry = data["years"][y]
        prev = data["years"].get(str(year - 1), {})
        apply_values(entry, prev, {(FIELDS[k][0], FIELDS[k][1]): v * 100 for k, v in found.items()}, year)
        derive(entry)
        carried[y] = [k for k in keys if k not in found]
        if not carried[y]:
            carried.pop(y)
        data["updated"] = today.isoformat()
        changed = True
        log.append(f"{year}: filled in {', '.join(found)}.")

    # 3) Tax brackets for the refund estimate.
    if update_tax(data, today, log):
        changed = True

    if not carried:
        data.pop("carried", None)
    write_report(data)
    if changed and not dry_run:
        with open(LIMITS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    for line in log or ["Nothing to do."]:
        say(line)
    return changed


def gather_new(data, year):
    """Returns {(account, field): cents} for `year`. Raises NotReady until every required figure is published."""
    cra = cra_limits(parse_html(fetch(PAGES["cra"]))[1], year)
    found, missing = us_limits(load_us_pages(), year)
    if missing:
        raise NotReady(f"IRS pages do not state the {year} figure yet for: {', '.join(missing)}.")
    cents = {("TFSA", "annualCents"): cra["TFSA"] * 100, ("RRSP", "annualCents"): cra["RRSP"] * 100}
    for k, v in found.items():
        cents[(FIELDS[k][0], FIELDS[k][1])] = v * 100
    return cents


def build_entry(data, year, cents):
    prev = data["years"].get(str(year - 1)) or data["years"][max(data["years"], key=int)]
    entry = copy.deepcopy(prev)
    apply_values(entry, prev, cents, year)
    derive(entry)
    got = {(a, f) for a, f in cents}
    missing_opt = [k for k, (a, f, req) in FIELDS.items() if not req and (a, f) not in got]
    return entry, missing_opt


def write_report(data):
    carried = data.get("carried") or {}
    if not carried:
        if os.path.exists(REPORT_PATH):
            os.remove(REPORT_PATH)
        return
    lines = ["These limits were copied forward from the previous year because the official page has not published the "
             "new figure yet. The updater retries every day and fills them in automatically.", ""]
    for y, keys in sorted(carried.items()):
        lines.append(f"**{y}:** " + ", ".join(DESCRIPTIONS.get(k, k) for k in keys))
    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def verify(year):
    """Read-only self-check: extract `year` from the live sources and compare with what limits.json already holds."""
    with open(LIMITS_PATH, encoding="utf-8") as fh:
        known = json.load(fh)["years"].get(str(year))
    if not known:
        raise Failure(f"limits.json has no {year} to compare with.")
    cra = cra_limits(parse_html(fetch(PAGES["cra"]))[1], year)
    say(f"CRA {year}: RRSP {cra['RRSP']:,} (file {known['RRSP']['annualCents'] // 100:,}), "
        f"TFSA {cra['TFSA']:,} (file {known['TFSA']['annualCents'] // 100:,})")
    found, _ = us_limits(load_us_pages(), year)
    for k, (acct, field, required) in FIELDS.items():
        want = known[acct][field] // 100
        got = found.get(k)
        status = "MATCH" if got == want else ("not found" if got is None else "DIFFERENT")
        say(f"{k}: found {got} / file {want} -> {status}" + (" (required)" if required and got is None else ""),
            "notice" if status == "MATCH" else "warning")


# ---------------------------------------------------------------- Tax brackets (for the refund estimate)
#
# limits.json "tax" holds one entry per year. A new year is added once its official numbers are out:
#   * Canada, federal and every province except Quebec: brackets and rates from the CRA payroll formulas (T4127, January
#     edition, table 8.1), basic personal amounts and the Ontario surtax from table 8.2, the federal basic personal amount
#     from the CRA indexation page. The T4127 tables come out in November or December for the next January.
#   * Quebec: Revenu Quebec blocks automated readers, so Quebec is carried forward with its thresholds and basic personal
#     amount indexed by the federal indexation rate (Quebec's own rate is usually within a few hundredths of a percent).
#     Rates stay as they were. An issue is opened so a person can glance at it.
#   * US: the IRS news release "tax inflation adjustments for tax year YYYY" (October or November), which states every
#     bracket in one fixed sentence pattern for single filers and married couples filing jointly.
# During the year, the CRA "current year tax rates" page is re-read every day. If a province changes its brackets mid-year
# (a budget), the year's entry is corrected from that page.

TAX_PAGES = {
    "cra_index": "https://www.canada.ca/en/revenue-agency/services/tax/individuals/frequently-asked-questions-individuals/adjustment-personal-income-tax-benefit-amounts.html",
    "cra_year": "https://www.canada.ca/en/revenue-agency/services/tax/individuals/tax-rates-brackets/current-year.html",
    "t4127": "https://www.canada.ca/en/revenue-agency/services/forms-publications/payroll/t4127-payroll-deductions-formulas/t4127-jan.html",
    "t4127_csv": "https://www.canada.ca/content/dam/cra-arc/formspubs/pub/t4127-jan/{kind}-01-{yy}e.csv",
    "irs_brackets": "https://www.irs.gov/filing/federal-income-tax-rates-and-brackets",
    "irs_newsroom": "https://www.irs.gov/newsroom",
    "irs_news_releases": "https://www.irs.gov/newsroom/news-releases-for-current-month",
    "irs_release_guess": "https://www.irs.gov/newsroom/irs-releases-tax-inflation-adjustments-for-tax-year-{year}",
}
T4127_RATES, T4127_OTHER = "rtsncmtrshldcnstnt", "thrrtsmnts"
PROVINCES = [  # code, name as the CRA writes it; the order apps show them in
    ("AB", "Alberta"), ("BC", "British Columbia"), ("MB", "Manitoba"), ("NB", "New Brunswick"),
    ("NL", "Newfoundland and Labrador"), ("NS", "Nova Scotia"), ("NT", "Northwest Territories"), ("NU", "Nunavut"),
    ("ON", "Ontario"), ("PE", "Prince Edward Island"), ("QC", "Quebec"), ("SK", "Saskatchewan"), ("YT", "Yukon"),
]
TAX_DUE = (9, 1)        # start looking for next year's brackets on September 1
TAX_OVERDUE = (2, 1)    # fail (and email) if the year's brackets are still missing on February 1 of that year
TAX_NOTES_PATH = os.path.join(ROOT, ".tax-notes.md")


def _num(cell):
    """'58,523' / '$1,141,275.01' / '142,520*' / '0.205' -> float, or None for blanks and symbols such as BPAF."""
    m = re.fullmatch(r"\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*\*?\s*", cell or "")
    return float(m.group(1).replace(",", "")) if m else None


def _pct(fraction):
    return round(fraction * 100, 4)


def _csv(raw):
    import csv, io
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(raw))]


def t4127_brackets(raw, year):
    """Table 8.1 of the T4127 CSV -> {code: [[from_dollars, rate_pct], ...]} for FED and the provinces (not Quebec)."""
    rows = _csv(raw)
    if not rows or not any("Table 8.1" in c for c in rows[0]):
        raise Failure("T4127 table 8.1: unexpected layout (first row is not the table title).")
    if not re.search(rf"\bfor {year}\b", " ".join(rows[0])):
        raise NotReady(f"T4127 table 8.1 is not for {year} yet.")
    out, code = {}, None
    for i, r in enumerate(rows[1:], 1):
        if len(r) < 3:
            continue
        if r[1] == "A":
            code = "FED" if r[0] == "Federal" else r[0]
            thresholds = [_num(c) for c in r[2:] if c]
            rates_row = rows[i + 1] if i + 1 < len(rows) else []
            if len(rates_row) < 3 or rates_row[1] not in ("R", "V"):
                raise Failure(f"T4127 table 8.1: no rate row after {code}.")
            rates = [_num(c) for c in rates_row[2:2 + len(thresholds)]]
            if None in thresholds or None in rates or thresholds[0] != 0:
                raise Failure(f"T4127 table 8.1: unreadable numbers for {code}.")
            out[code] = [[int(round(t)), _pct(v)] for t, v in zip(thresholds, rates)]
    want = {"FED"} | {c for c, _ in PROVINCES if c != "QC"}
    if set(out) != want:
        raise Failure(f"T4127 table 8.1: expected {sorted(want)}, found {sorted(out)}.")
    return out


def t4127_other(raw, year):
    """Table 8.2 -> ({code: basic personal amount or None when given as a formula}, ontario_surtax, quebec_abatement_pct)."""
    rows = _csv(raw)
    if not rows or not any("Table 8.2" in c for c in rows[0]):
        raise Failure("T4127 table 8.2: unexpected layout (first row is not the table title).")
    if not re.search(rf"\bfor {year}\b", " ".join(rows[0])):
        raise NotReady(f"T4127 table 8.2 is not for {year} yet.")
    header = rows[1]
    try:
        col_basic, col_t4, col_v1, col_ab = (header.index(h) for h in ("Basic amount", "T4 to V1", "V1 rate", "Abatement"))
    except ValueError:
        raise Failure(f"T4127 table 8.2: header changed: {header}")
    bpa, surtax, abatement, code = {}, [], None, None
    for r in rows[2:]:
        if len(r) <= col_ab:
            continue
        if r[0]:
            code = "FED" if r[0] == "Federal" else r[0]
            if code == "Outside Canada":
                code = None
                continue
            v = _num(r[col_basic])
            bpa[code] = int(round(v)) if v else None
            if code == "QC":
                abatement = _num(r[col_ab])
        if code == "ON":
            t, rate = _num(r[col_t4]), _num(r[col_v1])
            if t and rate:
                surtax.append([int(round(t)), _pct(rate)])
    missing = ({"FED"} | {c for c, _ in PROVINCES}) - set(bpa)
    if missing:
        raise Failure(f"T4127 table 8.2: no row for {sorted(missing)}.")
    if not surtax or abatement is None:
        raise Failure("T4127 table 8.2: Ontario surtax or Quebec abatement not found.")
    return bpa, surtax, _pct(abatement)


def cra_index_values(tables, year):
    """From the CRA indexation page: (federal basic personal amount, indexation percent) for `year`."""
    bpa = rate = None
    for t in tables:
        if not t or str(year) not in t[0]:
            continue
        col = t[0].index(str(year))
        for r in t[1:]:
            if len(r) <= col:
                continue
            if re.match(r"Basic personal amount \(maximum\)", r[0]):
                bpa = dollars(r[col])
            elif re.match(r"Indexation increase", r[0]):
                m = re.search(r"([0-9.]+)\s*%", r[col])
                rate = float(m.group(1)) if m else None
    if bpa is None or rate is None:
        raise NotReady(f"The CRA indexation page does not show the {year} basic personal amount and indexation rate yet.")
    return bpa, rate


def cra_year_brackets(text, tables, year):
    """The CRA 'current year tax rates' page -> {code: brackets} when it is for `year`, else None."""
    if not re.search(rf"Current year tax rates and income brackets \({year}\)", text):
        return None
    order = [("FED", "Federal rate")] + [(c, f"{n} rate") for c, n in PROVINCES if c != "QC"]
    pos = [text.find(f"{label}: {year}") for _, label in order]
    if -1 in pos:
        raise Failure("CRA current-year page: a federal or provincial section is missing.")
    order = [o for _, o in sorted(zip(pos, order))]  # the page's own order (alphabetical by English name)
    bracket_tables = [t for t in tables if len(t) > 2 and t[1][:3] == ["from", "up to", "is taxed at"]]
    if len(bracket_tables) != len(order):
        raise Failure(f"CRA current-year page: expected {len(order)} bracket tables, found {len(bracket_tables)}.")
    out = {}
    for (code, _), t in zip(order, bracket_tables):
        rows = []
        for r in t[2:]:
            frm, rate = _num(r[0]), re.fullmatch(r"\s*([0-9.]+)\s*%\s*", r[2] or "")
            if frm is None or not rate:
                raise Failure(f"CRA current-year page: unreadable row {r} for {code}.")
            rows.append([int(frm), float(rate.group(1))])  # "58,523.01" -> 58523, the same threshold the T4127 uses
        if rows[0][0] != 0:
            raise Failure(f"CRA current-year page: {code} does not start at $0.")
        out[code] = rows
    return out


US_RATE = r"(\d+(?:\.\d+)?)% for (?:individual single taxpayers with )?incomes (?:greater than|over) \$([\d,]+) \(\$([\d,]+) for married couples filing jointly\)"


def irs_release_brackets(text, year):
    """The IRS inflation-adjustment release -> (single, joint) brackets, or None when this is not the release for `year`."""
    text = re.sub(r"\s+", " ", text)
    start = text.find(f"For tax year {year}, the top tax rate")
    if start < 0:
        return None
    para = text[start:start + 1500]
    found = [(float(r), int(s.replace(",", "")), int(j.replace(",", ""))) for r, s, j in re.findall(US_RATE, para)]
    low = re.search(r"The lowest rate is (\d+(?:\.\d+)?)%", para)
    if len(found) < 5 or not low:
        raise Failure(f"IRS {year} release: the marginal-rates paragraph changed and could not be read.")
    found.sort(key=lambda x: x[1])
    single = [[0, float(low.group(1))]] + [[s, r] for r, s, _ in found]
    joint = [[0, float(low.group(1))]] + [[j, r] for r, _, j in sorted(found, key=lambda x: x[2])]
    return single, joint


def _links(raw, pattern):
    out = []
    for href in re.findall(r'href="([^"#]+)"', raw or ""):
        if re.search(pattern, href, re.I):
            out.append(href if href.startswith("http") else "https://www.irs.gov" + href)
    return out


def _try_fetch(url):
    try:
        return fetch(url)
    except OSError:
        return None


def us_tax(year):
    """Finds and reads the IRS inflation-adjustment release for `year`. Raises NotReady until it is out."""
    candidates = []
    for name in ("irs_brackets", "irs_newsroom", "irs_news_releases"):
        candidates += _links(_try_fetch(TAX_PAGES[name]), rf"inflation-adjustments?-for-tax-year-{year}")
    candidates.append(TAX_PAGES["irs_release_guess"].format(year=year))
    seen = set()
    for url in candidates:
        if url in seen:
            continue
        seen.add(url)
        raw = _try_fetch(url)
        if not raw:
            continue
        got = irs_release_brackets(parse_html(raw)[0], year)
        if got:
            return got
    raise NotReady(f"The IRS has not published its tax year {year} inflation adjustments yet.")


def t4127_csv(kind, year):
    """Finds a T4127 CSV for January of `year`: the link on the T4127 page if it is there, else the usual file name."""
    links = [u if u.startswith("http") else "https://www.canada.ca" + u
             for u in re.findall(rf'href="([^"]*{kind}-01-{year % 100:02d}e\.csv)"', _try_fetch(TAX_PAGES["t4127"]) or "")]
    for url in links + [TAX_PAGES["t4127_csv"].format(kind=kind, yy=f"{year % 100:02d}")]:
        raw = _try_fetch(url)
        if raw and "Table 8." in raw[:300]:
            return raw
    raise NotReady(f"The CRA has not published the T4127 tables for January {year} yet.")


def check_brackets(label, new, old, notes):
    """Sanity checks: starts at 0, rising thresholds, sane rates. Notes (not failures) when the structure changed."""
    if not new or new[0][0] != 0 or any(b[0] <= a[0] for a, b in zip(new, new[1:])):
        raise Failure(f"{label}: brackets {new} are not in order.")
    if any(not (0 <= r <= 60) for _, r in new):
        raise Failure(f"{label}: a rate in {new} is outside 0-60%.")
    if not old:
        return
    if len(new) != len(old) or any(a[1] != b[1] for a, b in zip(new, old)):
        notes.append(f"{label}: brackets changed shape or rates (was {old}, now {new}). Worth a quick look.")
        return
    for (t_new, _), (t_old, _) in zip(new[1:], old[1:]):
        check_plausible(f"{label} threshold", t_new * 100, t_old * 100)


def _jur(code, name, brackets, bpa, extra=None):
    j = {"code": code, "name": name, "brackets": brackets, "bpa": bpa, "credit": brackets[0][1]}
    if extra:
        j.update(extra)
    return j


def gather_tax(prev, year, notes):
    """Builds the tax entry for `year` from the official sources. `prev` is last year's entry (for checks and Quebec)."""
    rates = t4127_brackets(t4127_csv(T4127_RATES, year), year)
    bpa, surtax, abatement = t4127_other(t4127_csv(T4127_OTHER, year), year)
    fed_bpa, index_pct = cra_index_values(parse_html(fetch(TAX_PAGES["cra_index"]))[1], year)
    single, joint = us_tax(year)
    page = cra_year_brackets(*parse_html(fetch(TAX_PAGES["cra_year"])), year)
    if page:
        rates.update(page)  # the tax-return page wins over the payroll tables when it already shows the year

    prev_by_code = {p["code"]: p for p in prev.get("provinces", [])}
    bpa["FED"] = fed_bpa
    if bpa.get("YT") is None:
        bpa["YT"] = fed_bpa  # Yukon's basic personal amount follows the federal one
    provinces = []
    for code, name in PROVINCES:
        old = prev_by_code.get(code)
        if code == "QC":
            f = 1 + index_pct / 100
            br = [[0, old["brackets"][0][1]]] + [[int(round(t * f / 5) * 5), r] for t, r in old["brackets"][1:]]
            provinces.append(_jur(code, name, br, int(round(old["bpa"] * f))))
            notes.append(f"{year} Quebec: indexed from {year - 1} by {index_pct}% (Revenu Quebec cannot be read "
                         "automatically). Rates unchanged. Compare with Revenu Quebec's 'Income tax rates' page.")
            continue
        value = bpa.get(code)
        if value is None:
            value = old["bpa"] if old else None
            notes.append(f"{year} {name}: the basic personal amount is a formula in the payroll tables; kept {value:,}.")
        extra = {"surtax": surtax} if code == "ON" else None
        check_brackets(f"{year} {name}", rates[code], old and old["brackets"], notes)
        if old and old.get("bpa"):
            check_plausible(f"{year} {name} basic personal amount", value * 100, old["bpa"] * 100)
        provinces.append(_jur(code, name, rates[code], value, extra))

    check_brackets(f"{year} federal", rates["FED"], prev.get("federalCA", {}).get("brackets"), notes)
    check_brackets(f"{year} US single", single, prev.get("usSingle", {}).get("brackets"), notes)
    check_brackets(f"{year} US joint", joint, prev.get("usJoint", {}).get("brackets"), notes)
    entry = {k: v for k, v in prev.items() if k in ("note",)}
    entry.update({
        "sources": [TAX_PAGES["t4127"], TAX_PAGES["cra_index"], f"IRS tax year {year} inflation adjustments release"],
        "quebecAbatementPct": abatement,
        "federalCA": _jur("FED", "Federal", rates["FED"], fed_bpa),
        "provinces": provinces,
        "usSingle": {"code": "US", "name": "Federal", "brackets": single},
        "usJoint": {"code": "US", "name": "Federal", "brackets": joint},
    })
    return entry


def refresh_tax(entry, year, notes):
    """Mid-year: if the CRA current-year page shows `year` with different brackets (a budget change), use them."""
    page = cra_year_brackets(*parse_html(fetch(TAX_PAGES["cra_year"])), year)
    if not page:
        return False
    changed = False
    jurs = [entry["federalCA"]] + [p for p in entry["provinces"] if p["code"] != "QC"]
    for j in jurs:
        new = page[j["code"]]
        if new != j["brackets"]:
            check_brackets(f"{year} {j['name']}", new, None, notes)
            notes.append(f"{year} {j['name']}: brackets updated from the CRA current-year page (was {j['brackets']}, now {new}).")
            j["brackets"], j["credit"] = new, new[0][1]
            changed = True
    return changed


def update_tax(data, today, log):
    """Adds next year's tax brackets when published, and keeps this year's in line with the CRA page. Returns True if changed."""
    tax = data.setdefault("tax", {})
    years = sorted(int(y) for y in tax if y.isdigit())
    if not years:
        return False
    notes, changed = [], False
    target = years[-1] + 1
    if target <= today.year + 1 and today >= datetime.date(target - 1, *TAX_DUE):
        try:
            tax[str(target)] = gather_tax(tax[str(years[-1])], target, notes)
            log.append(f"Published {target} tax brackets.")
            changed = True
        except NotReady as e:
            log.append(f"{target} tax brackets not ready yet: {e}")
            if today >= datetime.date(target, *TAX_OVERDUE):
                raise Failure(f"{target} tax brackets are still not published and it is already {today.isoformat()}. "
                              "Refund estimates are using last year's brackets. Check scripts/update_limits.py.")
    if str(today.year) in tax:
        try:
            if refresh_tax(tax[str(today.year)], today.year, notes):
                log.append(f"Updated {today.year} tax brackets from the CRA current-year page.")
                changed = True
        except OSError as e:
            say(f"Could not check the CRA current-year page: {e}", "warning")
    if notes:
        with open(TAX_NOTES_PATH, "w", encoding="utf-8") as fh:
            fh.write("The daily limits check changed the tax brackets used by the refund estimate. Nothing needs doing "
                     "unless something below looks wrong; edit limits.json on main to correct it.\n\n")
            fh.write("\n".join(f"- {n}" for n in notes) + "\n")
    if changed:
        data["updated"] = today.isoformat()
    return changed


def main(argv):
    if "--verify" in argv:
        try:
            verify(int(argv[argv.index("--verify") + 1]))
            return 0
        except (Failure, NotReady, OSError) as e:
            say(f"VERIFY FAILED: {e}", "error")
            return 1
    try:
        run(dry_run="--dry-run" in argv)
    except Failure as e:
        say(f"ERROR: {e}", "error")
        return 1
    except OSError as e:
        say(f"ERROR: could not reach an official source: {e}", "error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

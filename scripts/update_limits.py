#!/usr/bin/env python3
"""Adds each new year's contribution limits to limits.json (run daily by .github/workflows/update-limits.yml).

No AI, no paid service. Every number is read by fixed rules from an official page, and cross-checked:
  * Canada (TFSA, RRSP): the CRA's yearly limits table.
  * US retirement (IRA, 401(k), SIMPLE, SEP, 457(b)): the IRS "COLA increases" page, which has one table column per year,
    plus the one sentence on each page that states the 60-63 "super" catch-up.
  * US health and gift (HSA, health FSA, gift exclusion): one sentence or table row each, from IRS Publication 969 and the
    IRS gift-tax page.
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

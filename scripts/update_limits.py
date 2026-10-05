#!/usr/bin/env python3
"""Adds each new year's contribution limits to limits.json (run daily by .github/workflows/update-limits.yml).

How it stays safe
  * Canada (TFSA, RRSP): read from the CRA's yearly limits table. No AI involved.
  * US (IRA, 401(k), SIMPLE, SEP, HSA, FSA, ...): the model only reads short snippets of official IRS pages. Every number it
    returns must appear word for word in those snippets and be a plausible step from last year, or nothing is published.
  * A new year is only created when every REQUIRED field is found. Optional fields (HSA, FSA, dependent care, gift
    exclusion) are carried forward from last year if their page has not been updated yet; they are listed under "carried"
    in limits.json and retried on every run until the real number is found. A GitHub issue is opened so you know.
  * Fixed-by-law limits (FHSA, RESP, RDSP, Coverdell, catch-up ages) are copied forward unchanged.

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

CRA_URL = ("https://www.canada.ca/en/revenue-agency/services/tax/registered-plans-administrators/pspa/"
           "mp-rrsp-dpsp-tfsa-limits-ympe.html")

# Official IRS pages the US numbers come from. A page that fails to load is skipped (and reported), so edit this list
# if the IRS moves one. The model only ever sees short snippets that mention the target year and a dollar amount.
IRS_PAGES = [
    "https://www.irs.gov/retirement-plans/cola-increases-for-dollar-limitations-on-benefits-and-contributions",
    "https://www.irs.gov/retirement-plans/plan-participant-employee/retirement-topics-401k-and-profit-sharing-plan-contribution-limits",
    "https://www.irs.gov/retirement-plans/plan-participant-employee/retirement-topics-ira-contribution-limits",
    "https://www.irs.gov/retirement-plans/plan-participant-employee/retirement-topics-457b-contribution-limits",
    "https://www.irs.gov/retirement-plans/plan-participant-employee/retirement-topics-simple-ira-contribution-limits",
    "https://www.irs.gov/publications/p969",
    "https://www.irs.gov/businesses/small-businesses-self-employed/frequently-asked-questions-on-gift-taxes",
]

# key -> (account, json field, required, group). Money is dollars from the model and cents in limits.json.
FIELDS = {
    "ira_annual": ("IRA", "annualCents", True, "retirement"),
    "ira_catchup": ("IRA", "catchUpCents", True, "retirement"),
    "k401_annual": ("K401", "annualCents", True, "retirement"),
    "k401_catchup": ("K401", "catchUpCents", True, "retirement"),
    "k401_super_catchup": ("K401", "superCatchUpCents", True, "retirement"),
    "simple_annual": ("SIMPLE", "annualCents", True, "retirement"),
    "simple_catchup": ("SIMPLE", "catchUpCents", True, "retirement"),
    "simple_super_catchup": ("SIMPLE", "superCatchUpCents", True, "retirement"),
    "sep_annual": ("SEP", "annualCents", True, "retirement"),
    "hsa_self": ("HSA", "annualCents", False, "health"),
    "hsa_family": ("HSA", "familyCents", False, "health"),
    "hsa_catchup": ("HSA", "catchUpCents", False, "health"),
    "fsa_health": ("FSA", "annualCents", False, "health"),
    "dcfsa": ("DCFSA", "annualCents", False, "health"),
    "dcfsa_separate": ("DCFSA", "separateCents", False, "health"),
    "gift_exclusion": ("P529", "annualCents", False, "gift"),
}
DESCRIPTIONS = {
    "ira_annual": "IRA contribution limit (traditional and Roth combined)",
    "ira_catchup": "IRA catch-up contribution for age 50 and over",
    "k401_annual": "401(k)/403(b)/TSP/457(b) employee elective deferral limit",
    "k401_catchup": "401(k) catch-up contribution for age 50 and over",
    "k401_super_catchup": "401(k) catch-up for ages 60 to 63 (the higher 'super' catch-up)",
    "simple_annual": "SIMPLE IRA employee deferral limit (the standard one, not the higher small-employer one)",
    "simple_catchup": "SIMPLE IRA catch-up for age 50 and over (standard)",
    "simple_super_catchup": "SIMPLE IRA catch-up for ages 60 to 63",
    "sep_annual": "Defined contribution plan annual additions limit (section 415(c)), also the SEP IRA and Solo 401(k) total",
    "hsa_self": "HSA contribution limit, self-only coverage",
    "hsa_family": "HSA contribution limit, family coverage",
    "hsa_catchup": "HSA catch-up contribution for age 55 and over",
    "fsa_health": "Health flexible spending arrangement (FSA) salary reduction limit",
    "dcfsa": "Dependent care FSA limit (single or joint return)",
    "dcfsa_separate": "Dependent care FSA limit, married filing separately",
    "gift_exclusion": "Annual gift tax exclusion per recipient",
}
GROUP_KEYWORDS = {
    "retirement": r"401\(k\)|IRA|SIMPLE|SEP|catch-up|catch up|415|403\(b\)|457",
    "health": r"HSA|health savings|FSA|flexible spending|dependent care",
    "gift": r"gift|annual exclusion",
}
SNIPPET_CAP = 9000  # characters per model call, so it fits the free tier's small input limit
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


# ---------------------------------------------------------------- US (IRS pages + free GitHub Models)

def snippets(text, year, group):
    """Short pieces of the page that mention the year, a dollar amount and the group's topic."""
    pieces = re.split(r"(?<=[.!?])\s+|\n+", text)
    keep, total, seen = [], 0, set()
    for s in pieces:
        s = s.strip()
        if (len(s) < 25 or str(year) not in s or "$" not in s or s in seen
                or not re.search(GROUP_KEYWORDS[group], s, re.I)):
            continue
        s = s[:600]
        seen.add(s)
        if total + len(s) > SNIPPET_CAP:
            break
        keep.append(s)
        total += len(s) + 1
    return "\n".join(keep)


def call_model(messages):
    """One call to GitHub Models (free for Actions with the built-in token). Retries on rate limits."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise Failure("GITHUB_TOKEN is not set, so the free GitHub Models service cannot be used.")
    body = json.dumps({
        "model": os.environ.get("LIMITS_MODEL", "openai/gpt-4.1"),
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        "https://models.github.ai/inference/chat/completions", data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = r.read()
                status = r.status
            try:
                return json.loads(raw)["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError):
                last = f"HTTP {status}, unexpected body: {raw[:300]!r}"
                break
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} {e.read()[:200]!r}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as e:
            last = str(e)
        time.sleep(20 * (attempt + 1))
    raise Failure(f"GitHub Models call failed: {last}")


def ask_model(year, keys, text):
    fields = "\n".join(f'- "{k}": {DESCRIPTIONS[k]}' for k in keys)
    system = ("You read official US tax-agency text and report dollar limits. Reply with one JSON object only. "
              "Use whole dollars as plain integers (no $ or commas). Use null when the text does not state that exact "
              "limit for the requested year. Never guess, estimate or use another year's figure.")
    user = f"Tax year: {year}\nKeys to fill:\n{fields}\n\nOfficial text:\n{text}"
    raw = call_model([{"role": "system", "content": system}, {"role": "user", "content": user}])
    try:
        data = json.loads(raw)
    except ValueError:
        raise Failure(f"Model did not return JSON: {raw[:200]!r}")
    return data if isinstance(data, dict) else {}


def irs_limits(pages_text, year, wanted):
    """Returns {key: dollars} for the wanted keys that are stated for `year` and appear word for word in the source."""
    found = {}
    for group in ("retirement", "health", "gift"):
        keys = [k for k in wanted if FIELDS[k][3] == group]
        if not keys:
            continue
        text = snippets(pages_text, year, group)
        if not text:
            continue
        answer = ask_model(year, keys, text)
        for k in keys:
            v = answer.get(k)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or v <= 0:
                raise Failure(f"Model returned a bad value for {k}: {v!r}")
            v = int(v)
            if f"{v:,}" not in text:
                raise Failure(f"{k}={v:,} does not appear in the official text the model was given. Not publishing.")
            found[k] = v
    return found


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
            cents, warns = gather_new(data, target)
            entry, missing_opt = build_entry(data, target, cents)
            data["years"][str(target)] = entry
            if missing_opt:
                carried[str(target)] = missing_opt
            data["updated"] = today.isoformat()
            data.setdefault("sources", []).append(
                f"{target}: CRA limits table and IRS pages, read automatically on {today.isoformat()}")
            changed = True
            log += [f"Published {target} limits."] + warns
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
        try:
            pages = []
            for url in IRS_PAGES:
                try:
                    pages.append(parse_html(fetch(url))[0])
                except OSError:
                    pass
            joined = "\n".join(pages)
            if str(year) not in joined:
                continue
            found = irs_limits(joined, year, keys)
        except Failure:
            raise
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
    prev_key = str(year - 1)
    prev = data["years"].get(prev_key) or data["years"][max(data["years"], key=int)]
    cents, warnings = {}, []
    cra_tables = parse_html(fetch(CRA_URL))[1]
    say(f"CRA page loaded: {len(cra_tables)} tables found.")
    cra = cra_limits(cra_tables, year)
    cents[("TFSA", "annualCents")] = cra["TFSA"] * 100
    cents[("RRSP", "annualCents")] = cra["RRSP"] * 100
    texts = []
    for url in IRS_PAGES:
        try:
            texts.append(parse_html(fetch(url))[0])
        except OSError as e:
            warnings.append(f"Could not load {url} ({e}).")
            say(f"Could not load {url} ({e}).", "warning")
    joined = "\n".join(texts)
    if str(year) not in joined:
        raise NotReady(f"The IRS pages do not mention {year} yet.")
    found = irs_limits(joined, year, list(FIELDS))
    for k, v in found.items():
        cents[(FIELDS[k][0], FIELDS[k][1])] = v * 100
    required_missing = [k for k, f in FIELDS.items() if f[2] and k not in found]
    if required_missing:
        raise NotReady(f"IRS pages do not state the {year} figure yet for: {', '.join(required_missing)}.")
    return cents, warnings


def build_entry(data, year, cents):
    prev = data["years"].get(str(year - 1)) or data["years"][max(data["years"], key=int)]
    entry = copy.deepcopy(prev)
    apply_values(entry, prev, cents, year)
    derive(entry)
    got = {(a, f) for a, f in cents}
    missing_opt = [k for k, (a, f, req, _) in FIELDS.items() if not req and (a, f) not in got]
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
    cra = cra_limits(parse_html(fetch(CRA_URL))[1], year)
    say(f"CRA {year}: RRSP {cra['RRSP']:,} (file {known['RRSP']['annualCents'] // 100:,}), "
        f"TFSA {cra['TFSA']:,} (file {known['TFSA']['annualCents'] // 100:,})")
    texts, loaded = [], 0
    for url in IRS_PAGES:
        try:
            texts.append(parse_html(fetch(url))[0])
            loaded += 1
        except OSError as e:
            say(f"IRS page not loaded: {url} ({e})", "warning")
    joined = "\n".join(texts)
    say(f"IRS pages loaded: {loaded} of {len(IRS_PAGES)}. Mentions {year}: {str(year) in joined}.")
    found = irs_limits(joined, year, list(FIELDS))
    for k, (acct, field, required, _) in FIELDS.items():
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

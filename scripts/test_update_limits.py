"""Offline tests: python3 scripts/test_update_limits.py   (no network; uses saved copies of the real 2026 pages)."""
import copy, datetime, json, os, re, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import update_limits as U


def fixture(name):
    with open(os.path.join(HERE, "fixtures", f"{name}.json"), encoding="utf-8") as f:
        d = json.load(f)
    return d["text"], d["tables"]


def base_data():
    with open(os.path.join(HERE, "..", "limits.json"), encoding="utf-8") as f:
        return json.load(f)


def bump(pages, year):
    """Turns the saved 2026 pages into pages that also publish `year` (every figure +2%, rounded to 50)."""
    def up(cell):
        v = U.dollars(cell)
        return cell if not v else "$" + format(int(round(v * 1.02 / 50) * 50), ",")
    out = {}
    for name, (text, tables) in pages.items():
        tables = copy.deepcopy(tables)
        for t in tables:
            if name == "irs_cola" and t and t[0] and t[0][0] and "2026" in t[0]:
                i = t[0].index("2026")
                t[0].insert(i, str(year))
                for r in t[1:]:
                    if len(r) > i:
                        r.insert(i, up(r[i]))
        if name == "irs_cola":
            text = text.replace("For 2026, this higher catch-up contribution limit is $11,25",
                                f"For {year}, this higher catch-up contribution limit is $11,50", 1)
            text = re.sub(r"(these plans\. )For 2026, (this higher catch-up contribution limit is \$)11,250",
                          rf"\1For {year}, \g<2>11,500", text, count=1)
        if name == "irs_simple":
            text = text.replace("For 2026, this higher catch-up contribution limit is $5,250",
                                f"For {year}, this higher catch-up contribution limit is $5,350")
        out[name] = (text, tables)
    return out


def pages_2026():
    return {n: fixture(n) for n in ("irs_cola", "irs_simple", "irs_p969", "irs_gift")}


class Cra(unittest.TestCase):
    def test_reads_2026(self):
        self.assertEqual(U.cra_limits(fixture("cra")[1], 2026), {"RRSP": 33810, "TFSA": 7000})

    def test_not_ready_when_tfsa_missing(self):
        with self.assertRaises(U.NotReady):
            U.cra_limits(fixture("cra")[1], 2027)  # CRA lists the 2027 RRSP row but not the TFSA one yet

    def test_layout_change(self):
        with self.assertRaises(U.Failure):
            U.cra_limits(U.parse_html("<table><tr><td>x</td></tr></table>")[1], 2027)


class Us(unittest.TestCase):
    def test_2026_matches_limits_json(self):
        found, missing = U.us_limits(pages_2026(), 2026)
        self.assertEqual(missing, [])
        known = base_data()["years"]["2026"]
        for k, (acct, field, _) in U.FIELDS.items():
            if k == "fsa_health":
                continue  # the saved Publication 969 only states 2025 for the health FSA
            self.assertEqual(found[k], known[acct][field] // 100, k)

    def test_future_year_is_not_ready(self):
        found, missing = U.us_limits(pages_2026(), 2027)
        self.assertEqual(found, {})
        self.assertIn("ira_annual", missing)

    def test_cross_check_failure(self):
        pages = bump(pages_2026(), 2027)
        for t in pages["irs_cola"][1]:
            if t[0][0].startswith("Other"):
                for r in t[1:]:
                    if r[0].startswith("457 elective"):
                        r[1] = "$30,000"
        with self.assertRaises(U.Failure):
            U.us_limits(pages, 2027)

    def test_layout_change_is_failure(self):
        pages = pages_2026()
        pages["irs_cola"] = (pages["irs_cola"][0], [])
        with self.assertRaises(U.Failure):
            U.us_limits(pages, 2026)


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "limits.json")
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(base_data(), f)
        self.old = (U.LIMITS_PATH, U.REPORT_PATH, U.load_page, U.fetch)
        U.LIMITS_PATH, U.REPORT_PATH = self.path, os.path.join(self.tmp, "report.md")
        self.pages = bump(pages_2026(), 2027)
        U.load_page = lambda name: self.pages[name]
        cra = copy.deepcopy(fixture("cra")[1])
        cra[1].insert(1, ["2027", "$7,000", "$170,000"])  # TFSA 2027 now published
        self.cra = cra
        U.fetch = lambda url: "CRA"
        self.orig_parse = U.parse_html
        U.parse_html = lambda markup: ("", self.cra)

    def tearDown(self):
        U.LIMITS_PATH, U.REPORT_PATH, U.load_page, U.fetch = self.old
        U.parse_html = self.orig_parse

    def years(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_not_due_before_september(self):
        self.assertFalse(U.run(today=datetime.date(2026, 8, 31)))
        self.assertNotIn("2027", self.years()["years"])

    def test_publishes_next_year(self):
        self.assertTrue(U.run(today=datetime.date(2026, 11, 5)))
        d = self.years()
        y = d["years"]["2027"]
        self.assertEqual(y["TFSA"]["annualCents"], 700000)
        self.assertEqual(y["RRSP"]["annualCents"], 3539000)  # CRA 2027 row in the saved page
        self.assertGreater(y["K401"]["annualCents"], 2450000)
        self.assertEqual(y["K457"]["annualCents"], y["K401"]["annualCents"])
        self.assertEqual(y["SOLO401K"]["annualCents"], y["SEP"]["annualCents"])
        self.assertEqual(y["FHSA"], d["years"]["2026"]["FHSA"])          # fixed by law, copied forward
        self.assertEqual(y["FSA"], d["years"]["2026"]["FSA"])            # not announced, carried
        self.assertIn("fsa_health", d["carried"]["2027"])
        self.assertTrue(os.path.exists(U.REPORT_PATH))

    def test_waits_when_irs_not_ready(self):
        self.pages = pages_2026()
        self.assertFalse(U.run(today=datetime.date(2026, 10, 10)))
        self.assertNotIn("2027", self.years()["years"])

    def test_overdue_fails(self):
        self.pages = pages_2026()
        with self.assertRaises(U.Failure):
            U.run(today=datetime.date(2027, 1, 16))

    def test_next_year_after_that(self):
        U.run(today=datetime.date(2026, 11, 5))
        self.assertFalse(U.run(today=datetime.date(2026, 11, 6)))  # 2028 is not due until Sept 2027


def read(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return f.read()


def page_html(name, year_from=None, year_to=None):
    """Rebuilds simple HTML from a saved page so parse_html returns the same text and tables."""
    text, tables = fixture(name)
    import html as h
    parts = ["<p>" + h.escape(text) + "</p>"]
    for t in tables:
        parts.append("<table>" + "".join("<tr>" + "".join(f"<td>{h.escape(c)}</td>" for c in r) + "</tr>" for r in t) + "</table>")
    out = "".join(parts)
    return out.replace(year_from, year_to) if year_from else out


class Tax(unittest.TestCase):
    def test_payroll_tables_2026(self):
        r = U.t4127_brackets(read("t4127_rates_2026.csv"), 2026)
        self.assertEqual(r["FED"], [[0, 14.0], [58523, 20.5], [117045, 26.0], [181440, 29.0], [258482, 33.0]])
        self.assertEqual(r["PE"][-1], [142520, 19.0])
        self.assertNotIn("QC", r)
        bpa, surtax, abatement = U.t4127_other(read("t4127_other_2026.csv"), 2026)
        self.assertEqual((bpa["AB"], bpa["ON"], bpa["FED"], bpa["QC"]), (22769, 12989, None, None))
        self.assertEqual(surtax, [[5818, 20.0], [7446, 36.0]])
        self.assertEqual(abatement, 16.5)

    def test_payroll_tables_not_ready(self):
        with self.assertRaises(U.NotReady):
            U.t4127_brackets(read("t4127_rates_2026.csv"), 2027)
        with self.assertRaises(U.NotReady):
            U.t4127_other(read("t4127_other_2026.csv"), 2027)

    def test_payroll_layout_change(self):
        with self.assertRaises(U.Failure):
            U.t4127_brackets("Something else,,\n", 2026)

    def test_federal_bpa(self):
        self.assertEqual(U.cra_index_values(fixture("cra_index")[1], 2026), (16452, 2.0))
        with self.assertRaises(U.NotReady):
            U.cra_index_values(fixture("cra_index")[1], 2027)

    def test_current_year_page(self):
        text, tables = fixture("cra_year")
        got = U.cra_year_brackets(text, tables, 2026)
        self.assertEqual(len(got), 13)
        self.assertEqual(got["MB"], [[0, 10.8], [47564, 12.75], [101200, 17.4]])
        self.assertEqual(got["NT"][0], [0, 5.9])
        self.assertEqual(got["YT"][-1], [500000, 15.0])
        self.assertIsNone(U.cra_year_brackets(text, tables, 2027))

    def test_irs_release_matches_limits_json(self):
        single, joint = U.irs_release_brackets(fixture("irs_release")[0], 2026)
        known = base_data()["tax"]["2026"]
        self.assertEqual(single, known["usSingle"]["brackets"])
        self.assertEqual(joint, known["usJoint"]["brackets"])
        self.assertIsNone(U.irs_release_brackets(fixture("irs_release")[0], 2027))

    def test_irs_release_reworded(self):
        with self.assertRaises(U.Failure):
            U.irs_release_brackets("For tax year 2026, the top tax rate is now explained differently.", 2026)

    def test_finds_release_links(self):
        raw = '<a href="/newsroom/irs-releases-tax-inflation-adjustments-for-tax-year-2027-x">x</a><a href="/other">y</a>'
        self.assertEqual(U._links(raw, r"inflation-adjustments?-for-tax-year-2027"),
                         ["https://www.irs.gov/newsroom/irs-releases-tax-inflation-adjustments-for-tax-year-2027-x"])

    def test_bracket_checks(self):
        notes = []
        with self.assertRaises(U.Failure):
            U.check_brackets("x", [[0, 10.0], [500, 12.0], [400, 15.0]], None, notes)
        with self.assertRaises(U.Failure):
            U.check_brackets("x", [[0, 10.0], [50000, 20.0]], [[0, 10.0], [20000, 20.0]], notes)  # 2.5x jump
        U.check_brackets("x", [[0, 9.0], [50000, 20.0]], [[0, 10.0], [49000, 20.0]], notes)  # rate cut: noted, allowed
        self.assertEqual(len(notes), 1)


class TaxRun(unittest.TestCase):
    """update_tax end to end, with the official pages served from the saved copies."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (U.fetch, U.TAX_NOTES_PATH)
        U.TAX_NOTES_PATH = os.path.join(self.tmp, "notes.md")
        self.data = base_data()
        self.pages = {}
        U.fetch = self.fake_fetch

    def tearDown(self):
        U.fetch, U.TAX_NOTES_PATH = self.old

    def fake_fetch(self, url):
        if url in self.pages:
            return self.pages[url]
        raise OSError(f"404 {url}")

    def publish_2027(self):
        """The 2026 sources relabelled as if 2027 were out (same numbers, which passes the plausibility checks)."""
        P = U.TAX_PAGES
        self.pages[P["t4127_csv"].format(kind=U.T4127_RATES, yy="27")] = read("t4127_rates_2026.csv").replace("for 2026", "for 2027")
        self.pages[P["t4127_csv"].format(kind=U.T4127_OTHER, yy="27")] = read("t4127_other_2026.csv").replace("for 2026", "for 2027")
        self.pages[P["cra_index"]] = page_html("cra_index", "<td>2026</td>", "<td>2027</td>")
        self.pages[P["irs_release_guess"].format(year=2027)] = page_html("irs_release", "For tax year 2026", "For tax year 2027")

    def test_waits_then_publishes(self):
        self.pages[U.TAX_PAGES["cra_year"]] = page_html("cra_year")
        log = []
        U.update_tax(self.data, datetime.date(2026, 11, 1), log)
        self.assertNotIn("2027", self.data["tax"])
        self.publish_2027()
        self.assertTrue(U.update_tax(self.data, datetime.date(2026, 12, 1), log))
        y = self.data["tax"]["2027"]
        self.assertEqual(y["federalCA"]["bpa"], 16452)
        self.assertEqual(y["quebecAbatementPct"], 16.5)
        codes = [p["code"] for p in y["provinces"]]
        self.assertEqual(len(codes), 13)
        on = next(p for p in y["provinces"] if p["code"] == "ON")
        self.assertEqual(on["surtax"], [[5818, 20.0], [7446, 36.0]])
        qc = next(p for p in y["provinces"] if p["code"] == "QC")
        self.assertEqual(qc["brackets"][1], [55430, 19.0])  # 54,345 indexed by 2.0%, to the nearest $5
        yt = next(p for p in y["provinces"] if p["code"] == "YT")
        self.assertEqual(yt["bpa"], 16452)
        self.assertEqual(y["usJoint"]["brackets"][-1], [768700, 37.0])
        self.assertTrue(os.path.exists(U.TAX_NOTES_PATH))  # Quebec is always noted

    def test_mid_year_budget_change(self):
        self.pages[U.TAX_PAGES["cra_year"]] = page_html("cra_year")
        self.assertTrue(U.update_tax(self.data, datetime.date(2026, 6, 1), []))
        mb = next(p for p in self.data["tax"]["2026"]["provinces"] if p["code"] == "MB")
        self.assertEqual(mb["brackets"], [[0, 10.8], [47564, 12.75], [101200, 17.4]])
        self.assertFalse(U.update_tax(self.data, datetime.date(2026, 6, 2), []))  # nothing new the next day

    def test_overdue_fails(self):
        with self.assertRaises(U.Failure):
            U.update_tax(self.data, datetime.date(2027, 2, 2), [])


if __name__ == "__main__":
    unittest.main()

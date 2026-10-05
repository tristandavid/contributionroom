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


if __name__ == "__main__":
    unittest.main()

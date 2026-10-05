"""Offline tests: python3 scripts/test_update_limits.py   (no network, the model is faked)."""
import copy, datetime, json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(__file__))
import update_limits as U

CRA_HTML = """<html><body><table><caption>MP, DB, RRSP, DPSP limits, YMPE and the YAMPE</caption>
<tr><th>Year</th><th>MP limit</th><th>DB limit</th><th>RRSP dollar limit</th><th>DPSP limit</th><th>YMPE</th></tr>
<tr><td>2027</td><td>$36,000</td><td>$4,000.00</td><td>$34,400</td><td>$18,000</td><td>$76,000</td></tr>
<tr><td>2026</td><td>$35,390</td><td>$3,932.22</td><td>$33,810</td><td>$17,695</td><td>$74,600</td></tr></table>
<table><tr><th>Year</th><th>TFSA dollar limit</th><th>ALDA dollar limit</th></tr>
<tr><td>2027</td><td>$7,000</td><td>$170,000</td></tr><tr><td>2026</td><td>$7,000</td><td>$160,000</td></tr></table></body></html>"""
CRA_NO_YEAR = CRA_HTML.replace("<td>2027</td>", "<td>2025</td>")
IRS = {
 "retirement": ("For 2027, the 401(k) employee deferral limit is $25,000. The IRA limit for 2027 is $7,750. "
   "For 2027 the catch-up for age 50 and over is $8,000 for 401(k) plans and the IRA catch-up is $1,100. "
   "For 2027 the 401(k) catch-up for ages 60 to 63 is $11,250. For 2027 the SIMPLE IRA limit is $17,500 and the SIMPLE catch-up is $4,000, "
   "ages 60 to 63 $5,250. For 2027 the section 415(c) limit is $73,000."),
}
FAKE_ANSWER = {"ira_annual": 7750, "ira_catchup": 1100, "k401_annual": 25000, "k401_catchup": 8000, "k401_super_catchup": 11250,
               "simple_annual": 17500, "simple_catchup": 4000, "simple_super_catchup": 5250, "sep_annual": 73000}

def base_data():
    with open(os.path.join(os.path.dirname(__file__), "..", "limits.json"), encoding="utf-8") as f:
        return json.load(f)

class Cra(unittest.TestCase):
    def test_reads_year(self):
        t = U.parse_html(CRA_HTML)[1]
        self.assertEqual(U.cra_limits(t, 2027), {"RRSP": 34400, "TFSA": 7000})
        self.assertEqual(U.cra_limits(t, 2026)["RRSP"], 33810)
    def test_not_ready(self):
        with self.assertRaises(U.NotReady): U.cra_limits(U.parse_html(CRA_NO_YEAR)[1], 2027)
    def test_layout_change(self):
        with self.assertRaises(U.Failure): U.cra_limits(U.parse_html("<table><tr><td>x</td></tr></table>")[1], 2027)

class Irs(unittest.TestCase):
    def setUp(self): self.orig = U.call_model
    def tearDown(self): U.call_model = self.orig
    def fake(self, answer): U.call_model = lambda m: json.dumps(answer)
    def test_snippets_filter(self):
        s = U.snippets("Intro. " + IRS["retirement"] + " Unrelated 2027 $5 thing about cats.", 2027, "retirement")
        self.assertIn("$25,000", s); self.assertNotIn("cats", s)
    def test_ok(self):
        self.fake(FAKE_ANSWER)
        got = U.irs_limits(IRS["retirement"], 2027, [k for k in U.FIELDS if U.FIELDS[k][3] == "retirement"])
        self.assertEqual(got["k401_annual"], 25000)
    def test_hallucination_rejected(self):
        self.fake(dict(FAKE_ANSWER, k401_annual=26000))
        with self.assertRaises(U.Failure): U.irs_limits(IRS["retirement"], 2027, ["k401_annual"])
    def test_null_is_fine(self):
        self.fake({"hsa_self": None})
        self.assertEqual(U.irs_limits(IRS["retirement"] + " HSA 2027 $5.", 2027, ["hsa_self"]), {})

class Merge(unittest.TestCase):
    def test_entry(self):
        data = base_data()
        cents = {("TFSA", "annualCents"): 700000, ("RRSP", "annualCents"): 3440000,
                 ("IRA", "annualCents"): 775000, ("K401", "annualCents"): 2500000, ("SEP", "annualCents"): 7300000,
                 ("K401", "catchUpCents"): 800000, ("K401", "superCatchUpCents"): 1125000}
        e, missing = U.build_entry(data, 2027, cents)
        self.assertEqual(e["K457"]["annualCents"], 2500000)       # derived from 401(k)
        self.assertEqual(e["SOLO401K"]["annualCents"], 7300000)   # derived from 415(c)
        self.assertEqual(e["FHSA"], data["years"]["2026"]["FHSA"])  # fixed by law, carried
        self.assertIn("hsa_self", missing)
        self.assertEqual(e["HSA"]["annualCents"], data["years"]["2026"]["HSA"]["annualCents"])
    def test_implausible(self):
        data = base_data()
        with self.assertRaises(U.Failure): U.build_entry(data, 2027, {("K401", "annualCents"): 250000})

class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "limits.json")
        json.dump(base_data(), open(self.path, "w"), indent=2)
        self.o = (U.LIMITS_PATH, U.REPORT_PATH, U.fetch, U.call_model, U.IRS_PAGES)
        U.LIMITS_PATH, U.REPORT_PATH = self.path, os.path.join(self.tmp, "r.md")
        U.IRS_PAGES = ["https://irs.example/a"]
        U.fetch = lambda url: CRA_HTML if "canada" in url else "<p>" + IRS["retirement"] + "</p>"
        U.call_model = lambda m: json.dumps(FAKE_ANSWER)
    def tearDown(self): U.LIMITS_PATH, U.REPORT_PATH, U.fetch, U.call_model, U.IRS_PAGES = self.o
    def test_publishes_and_reports(self):
        # model answers every key it is asked for with FAKE_ANSWER (extra keys ignored by design); health/gift groups find no snippets
        self.assertTrue(U.run(today=datetime.date(2026, 11, 15)))
        d = json.load(open(self.path))
        self.assertEqual(d["years"]["2027"]["RRSP"]["annualCents"], 3440000)
        self.assertEqual(d["updated"], "2026-11-15")
        self.assertIn("hsa_self", d["carried"]["2027"])
        self.assertTrue(os.path.exists(U.REPORT_PATH))
    def test_before_september_does_nothing(self):
        self.assertFalse(U.run(today=datetime.date(2026, 8, 31)))
    def test_not_ready_quiet(self):
        U.fetch = lambda url: CRA_NO_YEAR if "canada" in url else "<p>nothing</p>"
        self.assertFalse(U.run(today=datetime.date(2026, 10, 5)))
    def test_overdue_fails(self):
        U.fetch = lambda url: CRA_NO_YEAR if "canada" in url else "<p>nothing</p>"
        with self.assertRaises(U.Failure): U.run(today=datetime.date(2027, 1, 20))
    def test_bad_number_blocks_publish(self):
        U.call_model = lambda m: json.dumps(dict(FAKE_ANSWER, ira_annual=9999))
        with self.assertRaises(U.Failure): U.run(today=datetime.date(2026, 11, 15))
        self.assertNotIn("2027", json.load(open(self.path))["years"])

if __name__ == "__main__":
    unittest.main(verbosity=1)

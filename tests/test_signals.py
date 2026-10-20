"""Unit tests for `companies signals`: value recovery through filter-only grid queries."""

import csv
import re
import tempfile
import unittest

from clay_cpl.cli import _read_signal_inputs, build_parser
from clay_cpl.search.signals import (
    CompanySignals, SignalInput, growth_grid, norm_domain, norm_linkedin, to_rows, year_grid,
)

COMPANIES = [
    {"id": 1, "name": "Acme", "domain": "acme.de", "linkedin_url": "https://www.linkedin.com/company/acme",
     "estimated_employee_count": 40, "year_founded": 2015, "employee_growth_12mo": 1.32, "employee_growth_6mo": 0.97},
    {"id": 2, "name": "Old Works", "domain": "oldworks.com", "linkedin_url": "https://www.linkedin.com/company/oldworks",
     "estimated_employee_count": 120, "year_founded": 1871, "employee_growth_12mo": 0.42, "employee_growth_6mo": 1.0},
    {"id": 3, "name": "Rocket", "domain": "rocket.ai", "linkedin_url": "https://www.linkedin.com/company/rocket-ai",
     "estimated_employee_count": 15, "year_founded": 2025, "employee_growth_12mo": 3.5, "employee_growth_6mo": 1.5},
    {"id": 4, "name": "Blank", "domain": "blank.de", "linkedin_url": "https://www.linkedin.com/company/blank",
     "estimated_employee_count": 8},
    {"id": 5, "name": "Ancient", "domain": "ancient.it", "linkedin_url": "https://www.linkedin.com/company/ancient",
     "estimated_employee_count": 300, "year_founded": 1702, "employee_growth_12mo": 1.0},
    # Clay knows this one by domain only - its LinkedIn slug does not resolve as an identifier
    {"id": 6, "name": "Macon", "domain": "macon.de", "linkedin_url": "https://www.linkedin.com/company/macon",
     "estimated_employee_count": 30, "year_founded": 1996, "li_unresolvable": True},
    # parent and subsidiary share a domain
    {"id": 7, "name": "Big Parent", "domain": "big.com", "linkedin_url": "https://www.linkedin.com/company/big",
     "estimated_employee_count": 9000, "year_founded": 1900},
    {"id": 8, "name": "Big Portugal", "domain": "big.com", "linkedin_url": "https://www.linkedin.com/company/big-pt",
     "estimated_employee_count": 40},
]


class FakeClay:
    """Evaluates the subset of Clay's query language the module emits."""

    def __init__(self, companies, partial_above=None):
        self.companies = companies
        self.calls = 0
        self.partial_above = partial_above   # emulate Clay's partial preview past N matches

    def __call__(self, where):
        self.calls += 1
        m = re.match(r'clay\.include_company_identifiers\(\((.*?)\)\)(.*)$', where)
        assert m, where
        idents = {norm_linkedin(x) or norm_domain(x) for x in re.findall(r'"([^"]*)"', m.group(1))}
        rows = [c for c in self.companies
                if (norm_linkedin(c["linkedin_url"]) in idents and not c.get("li_unresolvable"))
                or norm_domain(c["domain"]) in idents]
        for fld, op, val in re.findall(r"(\w+) (>=|<) ([\d.]+)", m.group(2)):
            val = float(val)
            rows = [c for c in rows if c.get(fld) is not None and (c[fld] >= val if op == ">=" else c[fld] < val)]
        out = [{k: c[k] for k in ("id", "name", "domain", "linkedin_url", "estimated_employee_count")} for c in rows]
        if self.partial_above is not None and len(out) > self.partial_above:
            return out[: self.partial_above], "partial"
        return out, "complete"


def run(idents, fields, partial_above=None, **kw):
    fake = FakeClay(COMPANIES, partial_above)
    sig = CompanySignals(client=None, workers=2, query_fn=fake, **kw)
    found = sig.run(idents, fields, year=year_grid(1800, 2026), growth=growth_grid(5))
    rows = to_rows(idents, found, fields)
    return {(r["input_domain"] or r["input_linkedin"]): r for r in rows}, sig, fake


class SignalsTests(unittest.TestCase):
    def test_normalisation(self):
        self.assertEqual(norm_linkedin("https://de.linkedin.com/company/Acme/?trk=x"), "linkedin.com/company/acme")
        self.assertEqual(norm_linkedin("acme.de"), "")
        self.assertEqual(norm_domain("https://www.Acme.de/impressum"), "acme.de")

    def test_exact_year_and_edges(self):
        rows, _, _ = run(["acme.de", "oldworks.com", "rocket.ai", "blank.de", "ancient.it"], ["year_founded"])
        self.assertEqual(rows["acme.de"]["year_founded"], "2015")
        self.assertEqual(rows["oldworks.com"]["year_founded"], "1871")
        self.assertEqual(rows["rocket.ai"]["year_founded"], "2025")
        self.assertEqual(rows["blank.de"]["year_founded"], "")
        self.assertEqual(rows["ancient.it"]["year_founded"], "<1800")

    def test_growth_ranges(self):
        rows, _, _ = run(["acme.de", "oldworks.com", "rocket.ai", "blank.de", "ancient.it"], ["employee_growth_12mo"])
        self.assertEqual((rows["acme.de"]["headcount_growth_12mo_pct_min"], rows["acme.de"]["headcount_growth_12mo_pct_max"]), ("+30", "+35"))
        self.assertEqual((rows["oldworks.com"]["headcount_growth_12mo_pct_min"], rows["oldworks.com"]["headcount_growth_12mo_pct_max"]), ("", "-50"))
        self.assertEqual((rows["rocket.ai"]["headcount_growth_12mo_pct_min"], rows["rocket.ai"]["headcount_growth_12mo_pct_max"]), ("+200", ""))
        self.assertEqual((rows["ancient.it"]["headcount_growth_12mo_pct_min"], rows["ancient.it"]["headcount_growth_12mo_pct_max"]), ("+0", "+5"))
        self.assertEqual(rows["blank.de"]["headcount_growth_12mo_pct_min"], "")

    def test_unmatched_and_linkedin_input(self):
        rows, _, _ = run(["https://de.linkedin.com/company/acme/", "nobody.de"], ["year_founded"])
        acme = rows["https://www.linkedin.com/company/acme"]
        self.assertEqual(acme["year_founded"], "2015")
        self.assertEqual(acme["estimated_employee_count"], "40")
        self.assertEqual(rows["nobody.de"]["matched_by"], "")

    def test_linkedin_first_domain_fallback(self):
        inputs = [SignalInput(domain="macon.de", linkedin="linkedin.com/company/macon-bau-old-slug"),
                  SignalInput(domain="big.com", linkedin="linkedin.com/company/big-pt")]
        rows, _, _ = run(inputs, ["year_founded"])
        self.assertEqual(rows["macon.de"]["matched_by"], "domain")
        self.assertEqual(rows["macon.de"]["year_founded"], "1996")
        # the subsidiary page wins over the parent's domain, and has no year of its own
        self.assertEqual(rows["big.com"]["matched_by"], "linkedin")
        self.assertEqual(rows["big.com"]["clay_name"], "Big Portugal")
        self.assertEqual(rows["big.com"]["year_founded"], "")

    def test_each_block_runs_its_own_grid(self):
        _, sig_small, _ = run(["acme.de"], ["year_founded"])
        _, sig_blocks, _ = run(["acme.de", "oldworks.com", "rocket.ai"], ["year_founded"], chunk_size=1)
        self.assertLess(sig_small.queries, 40)
        self.assertGreater(sig_blocks.queries, sig_small.queries)

    def test_partial_coverage_is_split_not_lost(self):
        idents = ["acme.de", "oldworks.com", "rocket.ai", "blank.de", "ancient.it"]
        rows, sig, _ = run(idents, ["year_founded"], partial_above=2)
        self.assertEqual(sum(1 for r in rows.values() if r["matched_by"]), 5)
        self.assertEqual(rows["oldworks.com"]["year_founded"], "1871")
        self.assertGreater(sig.partial, 0)

    def test_chunk_size_guard(self):
        with self.assertRaises(ValueError):
            CompanySignals(client=None, chunk_size=201)

    def test_input_reader_prefers_linkedin(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            w = csv.writer(f)
            w.writerow(["Firma", "Domain", "LinkedIn Company URL"])
            w.writerow(["Acme", "acme.de", "https://www.linkedin.com/company/acme"])
            w.writerow(["NoLi", "noli.de", ""])
        got = _read_signal_inputs(f.name)
        self.assertEqual([(i.domain, i.linkedin) for i in got],
                         [("acme.de", "linkedin.com/company/acme"), ("noli.de", "")])

    def _lines(self, *lines):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("\n".join(lines) + "\n")
        return [(i.domain, i.linkedin) for i in _read_signal_inputs(f.name)]

    def test_plain_lists_keep_their_first_line(self):
        self.assertEqual(self._lines("https://www.linkedin.com/company/acme", "https://www.linkedin.com/company/beta"),
                         [("", "linkedin.com/company/acme"), ("", "linkedin.com/company/beta")])
        self.assertEqual(self._lines("domainfactory.de", "acme.com"), [("domainfactory.de", ""), ("acme.com", "")])
        self.assertEqual(self._lines("domain", "acme.com"), [("acme.com", "")])

    def test_headers_with_dots_and_slashes(self):
        self.assertEqual(self._lines("Nr.,Firma,Domain,Company LinkedIn URL",
                                     "1,Acme,acme.de,https://www.linkedin.com/company/acme"),
                         [("acme.de", "linkedin.com/company/acme")])
        self.assertEqual(self._lines("Firma,Website / Domain,LinkedIn", "Acme,https://acme.de/,"), [("acme.de", "")])

    def test_header_without_known_column_fails_loudly(self):
        with self.assertRaises(SystemExit):
            self._lines("Firma,Ort", "Acme,Berlin")

    def test_header_picks_company_linkedin_and_website_variants(self):
        got = self._lines("Name,person_linkedin_url,Company LinkedIn URL,Website URL",
                          "Ann,https://www.linkedin.com/in/ann,https://www.linkedin.com/company/acme,https://acme.de/")
        self.assertEqual(got, [("acme.de", "linkedin.com/company/acme")])

    def test_shared_domain_with_two_linkedin_pages_stays_two_rows(self):
        inputs = [SignalInput(domain="big.com", linkedin="linkedin.com/company/big"),
                  SignalInput(domain="big.com", linkedin="linkedin.com/company/big-pt")]
        fake = FakeClay(COMPANIES)
        sig = CompanySignals(client=None, workers=2, query_fn=fake)
        rows = to_rows(inputs, sig.run(inputs, ["year_founded"], year=year_grid(1800, 2026)), ["year_founded"])
        self.assertEqual([(r["clay_name"], r["year_founded"]) for r in rows], [("Big Parent", "1900"), ("Big Portugal", "")])

    def test_parser_registers_command(self):
        parser, _ = build_parser()
        args = parser.parse_args(["companies", "signals", "--in", "x.csv"])
        self.assertEqual(args.fields, "year_founded,employee_growth_6mo,employee_growth_12mo")
        self.assertEqual(args.growth_step, 5)


if __name__ == "__main__":
    unittest.main()

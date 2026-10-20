"""Founding year, headcount growth and employee count for a list of known companies.

Clay's search (the query language behind the app's "Find leads" screen) can filter on
`year_founded` and `employee_growth_{3,6,12,24}mo`, but it never returns those values. This
module recovers them anyway: for each block of companies it asks "which of these have a value
in range X?" for a grid of disjoint ranges, coarse first and finer only inside ranges that
returned companies. A company's value is the finest range it came back in.

It talks to the endpoint the app itself uses (`search-dsl/query`), authenticated with the
clay-cpl session. That endpoint does not book the public API's yearly record quota (measured
2026-10-20: 62 queries, counter unchanged). It returns at most 250 rows per query and has no
paging, which is why companies are processed in blocks of at most 200.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..exceptions import ClayAPIError

ROW_CAP = 250
DEFAULT_CHUNK = 200
GROWTH_FIELDS = ("employee_growth_3mo", "employee_growth_6mo", "employee_growth_12mo", "employee_growth_24mo")
ALL_FIELDS = ("year_founded",) + GROWTH_FIELDS


def norm_domain(value):
    d = (value or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = re.sub(r"^www\.", "", d)
    return d.split("/")[0].split("?")[0]


def norm_linkedin(value):
    """`https://de.linkedin.com/company/acme/?x=1` -> `linkedin.com/company/acme`."""
    v = (value or "").strip().lower().split("?")[0].rstrip("/")
    v = re.sub(r"^https?://", "", v)
    v = re.sub(r"^[a-z]{2,3}\.linkedin\.com", "linkedin.com", v)
    v = re.sub(r"^www\.linkedin\.com", "linkedin.com", v)
    return v if v.startswith("linkedin.com/company/") else ""


@dataclass
class Grid:
    """Integer grid [start, stop) scaled to field values; `levels` are the cell widths, coarse to fine."""
    start: int
    stop: int
    scale: float
    levels: tuple
    below: str = ""   # label for values under start*scale ("" = not queried)
    above: str = ""   # label for values at or over stop*scale


def year_grid(first_year=1800, last_year=None):
    last_year = last_year or time.gmtime().tm_year
    return Grid(first_year, last_year + 1, 1, (25, 5, 1), below=f"<{first_year}")


def growth_grid(step_pct=5):
    """Ratio grid from 0.5 (-50 %) to 3.0 (+200 %) in steps of `step_pct` percentage points."""
    if step_pct not in (1, 5, 10, 25):
        raise ValueError("growth step must be 1, 5, 10 or 25 percentage points")
    levels = tuple(w for w in (25, 5, 1) if w >= step_pct) if step_pct != 10 else (50, 10)
    return Grid(50, 300, 0.01, levels, below="<-50", above=">=+200")


@dataclass
class SignalInput:
    """One company to look up. `key` is what the output row is labelled with."""
    key: str
    domain: str = ""
    linkedin: str = ""

    @classmethod
    def parse(cls, value):
        li = norm_linkedin(value)
        return cls(key=value, linkedin=li) if li else cls(key=value, domain=norm_domain(value))


@dataclass
class CompanyRow:
    key: str                    # input this company was matched to
    matched_by: str = ""        # "linkedin" or "domain"
    clay_company_id: str = ""
    name: str = ""
    domain: str = ""
    linkedin_url: str = ""
    estimated_employee_count: str = ""
    values: dict = field(default_factory=dict)


class CompanySignals:
    def __init__(self, client, workers=6, chunk_size=DEFAULT_CHUNK, on_progress=None, query_fn=None):
        if chunk_size > DEFAULT_CHUNK:
            raise ValueError(f"chunk size above {DEFAULT_CHUNK} risks the {ROW_CAP}-row cap")
        self.client = client
        self.workers = workers
        self.chunk_size = chunk_size
        self.on_progress = on_progress or (lambda m: None)
        self._query_fn = query_fn or self._query_remote
        self.queries = 0
        self.capped = []
        self.partial = 0   # queries Clay answered from part of its index and that were split

    # transport ---------------------------------------------------------------------------
    def _query_remote(self, where):
        body = {"query": f"select from companies where {where} limit {ROW_CAP}",
                "workspaceId": self.client.workspace_id, "selectMode": "audiences", "userQueryHasLimit": True}
        path = f"workspaces/{self.client.workspace_id}/search-dsl/query"
        for attempt in range(5):
            try:
                d = self.client.post(path, body)
                return d.get("rows") or [], d.get("previewCoverage") or "complete"
            except ClayAPIError as e:
                # A read despite POST: resending is safe. Retry rate limits and network errors.
                if (e.status_code == 429 or e.network) and attempt < 4:
                    time.sleep(2 + 4 * attempt)
                    continue
                raise

    def _query(self, idents, cond=""):
        """Rows for `idents` (and an optional condition), split until Clay searched its whole index.

        Once a query matches more than roughly 30 companies, the preview answers from part of the
        index (`previewCoverage: partial`) and silently drops matches (measured 2026-10-20: 46
        identifiers -> 37 of 46 companies; 30 -> complete). Such a query is halved and repeated.
        """
        where = self.identifier_list(idents) + (f" and {cond}" if cond else "")
        self.queries += 1
        res = self._query_fn(where)
        rows, coverage = res if isinstance(res, tuple) else (res, "complete")
        if coverage != "complete" and len(idents) > 1:
            self.partial += 1
            mid = len(idents) // 2
            merged = {}
            for part in (idents[:mid], idents[mid:]):
                for r in self._query(part, cond):
                    merged[r.get("id")] = r
            return list(merged.values())
        if len(rows) >= ROW_CAP:
            self.capped.append(where[:120])
        return rows

    # matching ----------------------------------------------------------------------------
    @staticmethod
    def identifier_list(idents):
        return "clay.include_company_identifiers((" + ", ".join('"' + i.replace('"', "") + '"' for i in idents) + "))"

    def resolve(self, block):
        """Match one block of SignalInputs to Clay companies. Returns {input key: CompanyRow}.

        Every input is sent with all identifiers it has: Clay cannot resolve some LinkedIn slugs
        that it does know by domain. A LinkedIn match wins (it pins the exact entity, e.g. a
        subsidiary page); the domain is the fallback.
        """
        idents = []
        for i in block:
            if i.linkedin:
                idents.append("https://www." + i.linkedin)
            if i.domain:
                idents.append(i.domain)
        rows = self._query(list(dict.fromkeys(idents)))
        by_li, by_dom = {}, {}
        for row in rows:
            li, dom = norm_linkedin(row.get("linkedin_url")), norm_domain(row.get("domain"))
            if li:
                by_li[li] = row
            if dom:
                prev = by_dom.get(dom)
                # several Clay companies can share a domain - keep the largest
                if prev is None or (row.get("estimated_employee_count") or 0) > (prev.get("estimated_employee_count") or 0):
                    by_dom[dom] = row
        found = {}
        for i in block:
            row, how = (by_li.get(i.linkedin), "linkedin") if i.linkedin else (None, "")
            if row is None and i.domain:
                row, how = by_dom.get(i.domain), "domain"
            if row is not None:
                found[i.key] = CompanyRow(key=i.key, matched_by=how, clay_company_id=str(row.get("id") or ""),
                                          name=row.get("name") or "", domain=row.get("domain") or "",
                                          linkedin_url=row.get("linkedin_url") or "",
                                          estimated_employee_count=str(row.get("estimated_employee_count") or ""))
        return found

    # value recovery ----------------------------------------------------------------------
    def _cell(self, idents, fld, lo, hi, grid):
        cond = f"{fld} >= {self._fmt(lo * grid.scale)} and {fld} < {self._fmt(hi * grid.scale)}"
        return self._query(idents, cond)

    @staticmethod
    def _fmt(x):
        return str(int(x)) if float(x).is_integer() else f"{x:.4f}".rstrip("0")

    def recover(self, companies, fld, grid):
        """Fill companies[k].values[fld] with (min, max) of the finest grid cell, or an edge label."""
        by_id = {}
        for c in companies.values():
            if c.clay_company_id:
                by_id.setdefault(c.clay_company_id, []).append(c)
        if not by_id:
            return
        # query each company by the identifier that matched it
        idents = list(dict.fromkeys((cs[0].linkedin_url if cs[0].matched_by == "linkedin" else "") or cs[0].domain
                                    for cs in by_id.values()))
        # cells to query at the current level: (lo, hi); start with the coarse cells
        width = grid.levels[0]
        cells = [(lo, min(lo + width, grid.stop)) for lo in range(grid.start, grid.stop, width)]
        edges = []
        if grid.below:
            edges.append(("below", f"{fld} < {self._fmt(grid.start * grid.scale)}"))
        if grid.above:
            edges.append(("above", f"{fld} >= {self._fmt(grid.stop * grid.scale)}"))
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for label, cond in edges:
                for row in self._query(idents, cond):
                    for c in by_id.get(str(row.get("id")), []):
                        c.values[fld] = grid.below if label == "below" else grid.above
            for depth, width in enumerate(grid.levels):
                results = list(pool.map(lambda cell: (cell, self._cell(idents, fld, cell[0], cell[1], grid)), cells))
                nxt = []
                for (lo, hi), rows in results:
                    for row in rows:
                        for c in by_id.get(str(row.get("id")), []):
                            c.values[fld] = (lo * grid.scale, hi * grid.scale)
                    if rows and depth + 1 < len(grid.levels):
                        fine = grid.levels[depth + 1]
                        nxt += [(x, min(x + fine, hi)) for x in range(lo, hi, fine)]
                cells = nxt
                if not cells:
                    break

    def run(self, inputs, fields, year=None, growth=None):
        """`inputs`: SignalInput objects or plain strings (domain or LinkedIn URL)."""
        year = year or year_grid()
        growth = growth or growth_grid()
        items = list({i.key: i for i in (x if isinstance(x, SignalInput) else SignalInput.parse(x)
                                          for x in inputs if x)}.values())
        out = {}
        blocks = [items[i:i + self.chunk_size] for i in range(0, len(items), self.chunk_size)]
        for n, block in enumerate(blocks, 1):
            found = self.resolve(block)
            for fld in fields:
                self.recover(found, fld, year if fld == "year_founded" else growth)
            out.update(found)
            self.on_progress(f"  block {n}/{len(blocks)}: {len(found)} of {len(block)} matched, {self.queries} queries so far")
        return out


def to_rows(inputs, companies, fields):
    """Flatten results for CSV output, one row per input, in input order."""
    rows = []
    keys = [x.key if isinstance(x, SignalInput) else x for x in inputs if x]
    for key in dict.fromkeys(keys):
        c = companies.get(key)
        r = {"input": key, "matched_by": c.matched_by if c else "",
             "clay_company_id": c.clay_company_id if c else "", "clay_name": c.name if c else "",
             "clay_domain": c.domain if c else "", "clay_linkedin_url": c.linkedin_url if c else "",
             "estimated_employee_count": c.estimated_employee_count if c else ""}
        for fld in fields:
            v = c.values.get(fld) if c else None
            if fld == "year_founded":
                if isinstance(v, tuple):
                    lo, hi = int(v[0]), int(v[1])
                    r["year_founded"] = str(lo) if hi - lo == 1 else f"{lo}-{hi - 1}"
                else:
                    r["year_founded"] = v or ""
            else:
                col = fld.replace("employee_growth_", "headcount_growth_") + "_pct"
                if isinstance(v, tuple):
                    r[col + "_min"] = f"{round(v[0] * 100 - 100):+d}"
                    r[col + "_max"] = f"{round(v[1] * 100 - 100):+d}"
                elif v:
                    r[col + "_min"] = "+200" if v.startswith(">=") else ""
                    r[col + "_max"] = "-50" if v.startswith("<") else ""
                else:
                    r[col + "_min"] = r[col + "_max"] = ""
        rows.append(r)
    return rows

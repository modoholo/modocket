# modocket

Scrapers for the regulatory dockets and filing libraries of US energy regulators
and grid operators. None of these sites offer a usable API, so `modocket` reads
their HTML (or, where one exists, the JSON a page loads behind the scenes), lists
the cases and documents on them, and downloads the files.

## Sources

| Module | Class | What it covers |
| --- | --- | --- |
| `modocket.sources.caiso` | `CaliforniaISO` | CAISO's library of regulatory filings and orders: FERC, CPUC, CEC, other agency and court filings, CPUC proceedings, and the recent-filings page |
| `modocket.sources.puco` | `OhioPublicUtilityCommission` | Case records and filed documents on PUCO's Docketing Information System (DIS) |
| `modocket.sources.puco` | `OhioPowerSitingBoard` | The full OPSB case list |

Each class only has class methods: it acts as a small module for one site, and
you call its methods directly without creating an instance.

## Installation

The project uses [uv](https://docs.astral.sh/uv/) and needs Python 3.13 or later.

```sh
uv sync
```

## Network setup

Some of these sites, PUCO's in particular, block non-US IPs with a firewall page
that's served with a `200` status. Every request goes through
`modocket.https.make_us_session`, which can send traffic through Modo's US proxy:

```sh
cp .env.example .env
# then set MODOCKET_PROXY_URL (the same proxy data-service reads from ERCOT_PROXY_URL)
```

| `MODOCKET_PROXY_URL` | Modo VPN | Result |
| --- | --- | --- |
| set | **on** | Requests go through the US proxy, which is only reachable over the VPN |
| unset | **off** | Requests go direct. US sites may block the London VPN IP |

When a firewall blocks a request, `modocket` raises
`modocket.https.RequestRejected`, a subclass of `requests.HTTPError`, so a block
page is never saved as if it were a document.

CAISO's site loads from any IP, so it doesn't need the proxy.

## Usage

### CAISO

```python
from modocket.sources.caiso import CaliforniaISO, Collection, Proceedings

# Every FERC filing, across all years (one request per year page)
docs = list(CaliforniaISO.get_collection_documents(Collection.FERC_FILINGS))

doc = docs[-1]
doc.title    # "Sep 25, 2026 August 2026 WEIM Transition Period Report - GWA (ER15-2565)"
doc.dated    # date(2026, 9, 25), parsed from the title
doc.posted   # when CAISO posted the file (not the filing date)
doc.dockets  # ("ER15-2565",)

# A cheap check for new filings across every regulator (~150 documents)
recent = CaliforniaISO.get_recent_documents()

# CPUC proceedings: one page per docket
for slug in CaliforniaISO.get_proceeding_pages(Proceedings.RULEMAKINGS):
    docs = CaliforniaISO.get_library_documents(slug)

# Download files into one directory. Files already there are skipped, so an
# interrupted run can be resumed by running it again.
filings_2026 = CaliforniaISO.get_library_documents("ferc-filings-2026")
for path in CaliforniaISO.download_documents(filings_2026, root="caiso/ferc-2026"):
    print(path)
```

`LibraryDocument.url` is unique per document, which makes it the natural key for
de-duplicating across pages. Crawling the whole library takes about 180 requests,
returns about 9,000 documents, and runs for several minutes.

### PUCO and OPSB

```python
from modocket.sources.puco import OhioPowerSitingBoard, OhioPublicUtilityCommission

# Every case on the OPSB case list, as a list of dicts with keys like
# caseNumber, project, applicant, location, status, type and openDate.
# It always returns every case, so filter after fetching.
cases = OhioPowerSitingBoard.get_cases_list()

# Every file of every document filed in a PUCO case
OhioPublicUtilityCommission.download_case_documents("26-0746-EL-BGN", root="puco")
```

`download_case_documents` saves the files in a Hive-style layout:

```
puco/
  case_number=26-0746-EL-BGN/
    doc_id=<document UUID>/
      <CMID>.pdf
```

The lower-level methods (`get_case_document_ids`, `get_document_urls`,
`download_document`) are available if you only need part of a case.

## Project layout

```
src/modocket/
  https.py          # Shared HTTP helpers: sessions, retries, firewall detection, downloads
  sources/
    caiso.py        # California ISO
    puco.py         # Public Utilities Commission of Ohio and Ohio Power Siting Board
```

## Adding a source

New sources go in `src/modocket/sources/<regulator>.py`. To match the existing
ones:

- Write a class of class methods for each site.
- Make requests with `modocket.https`: `make_us_session` for sessions,
  `get_page` for pages and `download_file` for files. These handle the
  browser User-Agent, retries on reset connections, timeouts and firewall block
  pages.
- Let every method that makes a request take an optional `session`, so callers
  can share one connection across a crawl.
- Write NumPy-style docstrings. Use the class docstring's `Notes` section to
  describe how the site is structured and any quirks you found.

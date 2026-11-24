"""California Independent System Operator

CAISO posts what it files with its regulators (FERC, the CPUC, the CEC, and others),
and the orders it receives, in the document library on its website. None of it sits
behind an API; every page is server-rendered HTML with the same document table.
"""

from dataclasses import dataclass
from datetime import date, datetime, UTC
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Generator, Iterable
from urllib.parse import urljoin

import requests

from modocket.https import (
    download_file,
    get_page,
    make_us_session,
)


class Collection(StrEnum):
    """A collection of CAISO's library that is split into one page per year

    Each value is the collection's slug: its index page is ``/library/<slug>``, which
    links to its year pages, ``/library/<slug>-<year>``. A few year slugs aren't
    plain years, e.g. ``ferc-orders-2000-and-earlier``.
    """

    FERC_FILINGS = "ferc-filings"  # 1998-
    FERC_ORDERS = "ferc-orders"  # 2000 and earlier-
    CPUC_FILINGS = "cpuc-filings"  # 2007-
    CEC_FILINGS = "cec-filings"  # 2017-, with gaps
    OTHER_AGENCY_FILINGS = "other-regulatory-agency-filings"  # 2017-
    COURT_FILINGS = "court-filings"  # 2020-, with gaps


class Proceedings(StrEnum):
    """A collection of CAISO's library that is split into one page per CPUC docket

    Each value is the collection's slug: its index page is ``/library/<slug>``, which
    links to a page per proceeding, e.g. ``/library/r2310011-order-instituting-...``,
    and (for most) an archive index of older proceedings, ``/library/<slug>-archive``.
    """

    APPLICATIONS = "applications-of-approval"
    INVESTIGATIONS = "orders-instituting-investigation"
    PETITIONS = "petitions-archive"
    RULEMAKINGS = "orders-instituting-rulemaking"


@dataclass(frozen=True)
class LibraryDocument:
    """One row of a CAISO library document table

    Attributes
    ----------
    title (str)
        The title as displayed, e.g. ``"Sep 25, 2026 August 2026 WEIM Transition
        Period Report - GWA (ER15-2565)"``
    url (str)
        The absolute URL of the file itself, almost always a
        ``https://www.caiso.com/documents/<name>.pdf``. It is unique per document,
        so it's the natural key for de-duplicating across pages.
    types (tuple[str, ...])
        CAISO's document types, e.g. ``("Filing",)`` or ``("Filing", "Comment")``.
        Empty for a few (mostly old) documents.
    posted (datetime)
        When CAISO posted the file to its website, in UTC. **This is not the filing
        date**: much of the pre-2006 archive was posted in 2005, for example.
    dated (date | None)
        The date the title starts with, when it starts with one (e.g. ``"Sep 25,
        2026 ..."``). This is usually the filing or order date. Most titles since
        about 2008 have one; older ones mostly don't.
    size (str | None)
        The file size as displayed, e.g. ``"2.56 MB"``. Only the library pages show
        it, not the recent-filings page.
    section (str | None)
        The table the row came from, when a page has more than one. Only the
        recent-filings page does, e.g. ``"recent-ferc-filings"``.
    dockets (tuple[str, ...])
        The FERC dockets (e.g. ``"ER15-2565"``) and CPUC proceedings (e.g.
        ``"A.24-06-017"``) named in the title, uppercased, in the order they appear.
        This is best effort: older titles often write dockets without the hyphen
        (``ER983760000``), which isn't recognized.
    """

    title: str
    url: str
    types: tuple[str, ...]
    posted: datetime
    dated: date | None
    size: str | None
    section: str | None
    dockets: tuple[str, ...]

    @property
    def filename(self) -> str:
        """The last segment of the URL, e.g. ``"sep-25-2026-...-er15-2565.pdf"``"""
        return self.url.rstrip("/").rsplit("/", 1)[-1]


class _DocumentTableParser(HTMLParser):
    """Collects the rows of every CAISO document table on a page

    Both kinds of table (the library's ``table-doc-lib`` and the recent-filings page's
    ``table-doc``) label their cells with ``data-mobile-title``: ``Title`` (a link to
    the file, and on library pages a ``<small>`` with its size), ``Type``, and
    ``Posted`` (whose ``data-sort`` is a Unix timestamp). Each table sits inside a
    ``<div data-block-type="doc-table" data-track-section="...">``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self._section: str | None = None
        self._row: dict | None = None
        self._cell: str | None = None
        self._in_size = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div" and attributes.get("data-block-type") == "doc-table":
            self._section = attributes.get("data-track-section")
        elif tag == "tr":
            self._row = {"section": self._section, "text": {}, "size": ""}
        elif self._row is None:
            return
        elif tag == "td":
            self._cell = attributes.get("data-mobile-title")
            if self._cell:
                self._row["text"][self._cell] = ""
            if self._cell == "Posted":
                self._row["timestamp"] = attributes.get("data-sort")
        elif tag == "a" and self._cell == "Title" and "href" not in self._row:
            self._row["href"] = attributes.get("href")
        elif tag == "small" and self._cell == "Title":
            self._in_size = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "small":
            self._in_size = False
        elif tag == "td":
            self._cell = None
        elif tag == "tr" and self._row is not None:
            # Header rows use <th>, so they never have a title
            if "Title" in self._row["text"]:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._row is None or self._cell is None:
            return
        if self._in_size:
            self._row["size"] += data
        else:
            self._row["text"][self._cell] += data


class CaliforniaISO(object):
    """An interface for the regulatory filings and orders in CAISO's document library

    This object only has class methods; it's a mini-module within
    ``modocket.sources.caiso``. The ``get_collection_documents`` method is the primary
    endpoint, and lists every document in a ``Collection``, across all its years.

    Attributes
    ----------
    BASE_URL (str)
        The root URL of CAISO's website
    FERC_DOCKET_PATTERN (re.Pattern)
        Matches a FERC docket number with hyphens, e.g. ``ER15-2565`` or
        ``ER01-724-000``
    CPUC_PROCEEDING_PATTERN (re.Pattern)
        Matches a CPUC proceeding number, e.g. ``A.24-06-017`` or ``R-21-06-017``
    PROCEEDING_SLUG_PATTERN (re.Pattern)
        Matches the slug of a CPUC proceeding's page, e.g. ``a-24-06-017-...`` or
        ``r2310011-...``
    RECENT_FILINGS_PATH (str)
        The page listing CAISO's most recent filings and orders across every
        regulator, relative to ``BASE_URL``
    TITLE_DATE_PATTERN (re.Pattern)
        Matches the date at the start of a title, e.g. ``Sep 25, 2026`` or
        ``September 5, 2019``

    Methods
    -------
    download_document
        Save one document's file
    download_documents
        Save many documents' files into one directory, skipping ones already there
    get_collection_documents
        List every document in a yearly collection
    get_collection_pages
        The slugs of a yearly collection's year pages
    get_library_documents
        List the documents on one library page
    get_library_page
        Fetch one library page
    get_proceeding_pages
        The slugs of every proceeding page in a CPUC proceedings collection
    get_recent_documents
        List the documents on the recent-filings page
    parse_documents
        Parse the document tables out of a page's HTML

    Notes
    -----
    The library is at ``/library/regulatory-filings-and-orders``. It is organized as
    index pages that link to leaf pages, and only the leaf pages list documents:

    1. **Yearly collections** (``Collection``): ``/library/ferc-filings`` links to
       ``/library/ferc-filings-1998`` through ``/library/ferc-filings-2026``.
    2. **CPUC proceedings** (``Proceedings``): ``/library/orders-instituting-rulemaking``
       links to a page per proceeding, plus an archive index of older ones.

    Each leaf page lists every document in one table: there's no pagination, and the
    "N of N results" count on the page always matches the rows in the HTML. The
    tables only link to files; there's no per-document page in between.

    ``/legal-regulatory/regulatory-filings-orders/filings`` is a separate page with
    the ~150 most recent documents across every regulator (50 each for FERC and the
    CPUC, and everything recent for the others). As of Sep 2026 every document on it
    is also in the library, so it's only a cheaper way to check for new filings.

    The CPUC proceeding pages mostly repeat documents from ``Collection.CPUC_FILINGS``,
    but not entirely: as of Sep 2026 they add a couple of hundred documents that no
    yearly page lists. Crawling everything is about 180 requests and ~9,000
    documents, and takes several minutes.

    The pages load without a US IP and don't need a browser User-Agent, but the
    session from ``make_us_session`` is used anyway, for consistency and its retries.
    """

    BASE_URL: str = "https://www.caiso.com"
    FERC_DOCKET_PATTERN = re.compile(
        r"\b(?:ER|EL|EC|EG|ES|OA|RM|AD|TX|QF|RT|RR|IN|PL|EX|RC|RD|NJ|ND)"
        r"\d{2}-\d{1,5}(?:-\d{3})?\b",
        re.IGNORECASE,
    )
    CPUC_PROCEEDING_PATTERN = re.compile(
        r"\b([ARIPC])[.\-]?\s?(\d{2})-(\d{2})-(\d{3})\b", re.IGNORECASE
    )
    PROCEEDING_SLUG_PATTERN = re.compile(r"^[arcip]-?\d{2}-?\d{2}-?\d{2,3}-")
    RECENT_FILINGS_PATH: str = "/legal-regulatory/regulatory-filings-orders/filings"
    TITLE_DATE_PATTERN = re.compile(
        r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})"
    )

    @classmethod
    def download_document(
        cls,
        document: LibraryDocument | str,
        path: str | Path,
        session: requests.Session | None = None,
    ) -> Path:
        """Save the file of one document to ``path``

        Parameters
        ----------
        document (LibraryDocument | str)
            The document, or the URL of its file
        path (str | Path)
            Where to save the file. Its parent directory must already exist, and an
            existing file at ``path`` is overwritten.
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Returns
        -------
        Path
            ``path``, as a ``Path``

        Raises
        ------
        requests.HTTPError, requests.Timeout
            If the request fails; see ``modocket.https.download_file``
        """
        url = document.url if isinstance(document, LibraryDocument) else document
        # A handful of library rows link to files that aren't PDFs, so accept any type
        return download_file(url, path, session, content_type=None)

    @classmethod
    def download_documents(
        cls,
        documents: Iterable[LibraryDocument],
        root: str | Path | None = None,
        session: requests.Session | None = None,
        overwrite: bool = False,
    ) -> Generator[Path]:
        """Save the files of many documents into one directory

        Each file is saved as ``root / document.filename``. CAISO serves every file
        from one flat ``/documents/`` directory, so filenames are unique.

        Files are downloaded as iteration proceeds, not when this is called, so a
        long download can be resumed by calling this again with ``overwrite=False``.

        Parameters
        ----------
        documents (Iterable[LibraryDocument])
            The documents, e.g. from ``get_collection_documents``
        root (str | Path | None)
            The directory to save into. Defaults to the current working directory.
            It is created if needed.
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.
        overwrite (bool)
            Whether to download files that already exist under ``root``

        Yields
        ------
        Path
            The path of each document's file, whether downloaded or already present

        Raises
        ------
        requests.HTTPError, requests.Timeout
            If any request fails. Files saved before the failure are kept.
        """
        root = Path.cwd() if root is None else Path(root)
        root.mkdir(parents=True, exist_ok=True)
        session = session or make_us_session()

        for document in documents:
            path = root / document.filename
            if overwrite or not path.exists():
                cls.download_document(document, path, session)
            yield path

    @classmethod
    def get_collection_documents(
        cls,
        collection: Collection | str,
        session: requests.Session | None = None,
    ) -> Generator[LibraryDocument]:
        """List every document in a yearly collection, across all its years

        Year pages are fetched one at a time as iteration proceeds, oldest first.
        A document on more than one year page is only yielded once.

        Parameters
        ----------
        collection (Collection | str)
            The collection, e.g. ``Collection.FERC_FILINGS``
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Yields
        ------
        LibraryDocument
            Each document, in the order of its year page's table

        Raises
        ------
        requests.HTTPError, modocket.https.RequestRejected, requests.Timeout
            If any request fails; see ``get_library_page``
        """
        session = session or make_us_session()
        seen: set[str] = set()
        for slug in cls.get_collection_pages(collection, session):
            for document in cls.get_library_documents(slug, session):
                if document.url not in seen:
                    seen.add(document.url)
                    yield document

    @classmethod
    def get_collection_pages(
        cls,
        collection: Collection | str,
        session: requests.Session | None = None,
    ) -> list[str]:
        """The slugs of a yearly collection's year pages, read from its index page

        Parameters
        ----------
        collection (Collection | str)
            The collection, e.g. ``Collection.FERC_FILINGS``
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Returns
        -------
        list[str]
            Slugs like ``"ferc-filings-2026"``, sorted, so ``...-2000-and-earlier``
            comes before ``...-2001``

        Raises
        ------
        requests.HTTPError, modocket.https.RequestRejected, requests.Timeout
            If the request fails; see ``get_library_page``
        RuntimeError
            If the index page links to no year pages, which means its layout changed
        """
        collection = str(collection)
        html = cls.get_library_page(collection, session).text
        pattern = re.compile(rf'href="/library/({re.escape(collection)}-\d{{4}}[\w-]*)"')

        slugs = sorted(set(pattern.findall(html)))
        if not slugs:
            raise RuntimeError(f"/library/{collection} links to no year pages")
        return slugs

    @classmethod
    def get_library_documents(
        cls,
        slug: str,
        session: requests.Session | None = None,
    ) -> list[LibraryDocument]:
        """List the documents on one library page

        Parameters
        ----------
        slug (str)
            The page's slug, e.g. ``"ferc-filings-2026"`` or a proceeding's slug from
            ``get_proceeding_pages``
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Returns
        -------
        list[LibraryDocument]
            The page's documents, in table order. Empty for index pages.

        Raises
        ------
        requests.HTTPError, modocket.https.RequestRejected, requests.Timeout
            If the request fails; see ``get_library_page``
        """
        return cls.parse_documents(cls.get_library_page(slug, session).text)

    @classmethod
    def get_library_page(
        cls,
        slug: str,
        session: requests.Session | None = None,
    ) -> requests.Response:
        """Fetch one page of CAISO's library

        Parameters
        ----------
        slug (str)
            The page's slug, e.g. ``"ferc-filings"`` for ``/library/ferc-filings``
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Returns
        -------
        requests.Response
            The response for ``/library/<slug>``. Its ``.text`` is the page's HTML.

        Raises
        ------
        requests.HTTPError
            If the request fails, e.g. a 404 for an unknown slug
        modocket.https.RequestRejected
            If a firewall blocks the request
        requests.Timeout
            If the request takes longer than ``DEFAULT_TIMEOUT`` seconds
        """
        return get_page(f"{cls.BASE_URL}/library/{slug}", session)

    @classmethod
    def get_proceeding_pages(
        cls,
        proceedings: Proceedings | str,
        session: requests.Session | None = None,
    ) -> list[str]:
        """The slugs of every proceeding page in a CPUC proceedings collection

        Reads the collection's index page and, if it links to one, its archive index
        (``<slug>-archive``) of older proceedings.

        Parameters
        ----------
        proceedings (Proceedings | str)
            The collection, e.g. ``Proceedings.RULEMAKINGS``
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Returns
        -------
        list[str]
            Slugs like ``"r2310011-order-instituting-rulemaking-oversee-resource-..."``,
            in the order they first appear. Pass them to ``get_library_documents``.

        Raises
        ------
        requests.HTTPError, modocket.https.RequestRejected, requests.Timeout
            If any request fails; see ``get_library_page``
        """
        session = session or make_us_session()
        link_pattern = re.compile(r'href="/library/([\w-]+)"')

        indexes = [str(proceedings)]
        slugs: dict[str, None] = {}
        for index in indexes:
            html = cls.get_library_page(index, session).text
            for slug in link_pattern.findall(html):
                if slug == f"{proceedings}-archive" and slug not in indexes:
                    indexes.append(slug)
                elif cls.PROCEEDING_SLUG_PATTERN.match(slug):
                    slugs[slug] = None

        return list(slugs)

    @classmethod
    def get_recent_documents(
        cls,
        session: requests.Session | None = None,
    ) -> list[LibraryDocument]:
        """List the documents on the recent-filings page

        The page has one table per regulator ("Recent FERC filings", "Recent CPUC
        filings", and so on); each document's ``section`` says which it came from.

        Parameters
        ----------
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Returns
        -------
        list[LibraryDocument]
            The page's documents, in table order

        Raises
        ------
        requests.HTTPError, modocket.https.RequestRejected, requests.Timeout
            If the request fails; see ``modocket.https.get_page``
        """
        url = cls.BASE_URL + cls.RECENT_FILINGS_PATH
        return cls.parse_documents(get_page(url, session).text)

    @classmethod
    def parse_documents(cls, html: str) -> list[LibraryDocument]:
        """Parse every CAISO document table in a page's HTML

        Parameters
        ----------
        html (str)
            The HTML of a library page or the recent-filings page

        Returns
        -------
        list[LibraryDocument]
            One per table row, in page order. Rows without a link or a posted
            timestamp (so far there are none) are skipped.
        """
        parser = _DocumentTableParser()
        parser.feed(html)
        parser.close()

        documents = []
        for row in parser.rows:
            if not row.get("href") or not row.get("timestamp"):
                continue

            text = {k: " ".join(v.split()) for k, v in row["text"].items()}
            title = text["Title"]
            types = tuple(t.strip() for t in text.get("Type", "").split(",") if t.strip())
            dockets = [m.group(0).upper() for m in cls.FERC_DOCKET_PATTERN.finditer(title)]
            dockets += [
                f"{m.group(1).upper()}.{m.group(2)}-{m.group(3)}-{m.group(4)}"
                for m in cls.CPUC_PROCEEDING_PATTERN.finditer(title)
            ]

            documents.append(
                LibraryDocument(
                    title=title,
                    url=urljoin(cls.BASE_URL, row["href"]),
                    types=types,
                    posted=datetime.fromtimestamp(int(row["timestamp"]), UTC),
                    dated=cls._parse_title_date(title),
                    size=" ".join(row["size"].split()) or None,
                    section=row["section"],
                    dockets=tuple(dict.fromkeys(dockets)),
                )
            )

        return documents

    @classmethod
    def _parse_title_date(cls, title: str) -> date | None:
        """The date a title starts with, e.g. ``"Sep 25, 2026 ..."``, if any"""
        match = cls.TITLE_DATE_PATTERN.match(title)
        if match is None:
            return None
        try:
            return datetime.strptime(" ".join(match.groups()), "%b %d %Y").date()
        except ValueError:
            return None

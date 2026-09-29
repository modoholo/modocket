"""Public Utilities Commission of Ohio

The PUCO delegates most of its energy-related decisions to the OPSB (the Ohio Power
Siting Board)
"""

from enum import Enum
from pathlib import Path
import re
from typing import Generator

import requests

from modocket.https import (
    DEFAULT_TIMEOUT,
    make_us_session,
    raise_for_rejection,
)


class OhioPowerSitingBoard(object):
    """An interface for gathering all the sites on the OPSB case list

    This object only has class methods because it really is just a mini-module within
    the larger ``modocket.sources.puco`` module. The ``get_cases_list`` is the primary
    endpoint, and returns a ``list[dict]`` of the cases there. 

    Each case is a dict with the keys ``caseNumber``, ``project``, ``applicant``,
    ``location`` (the county), ``status``, ``type``, ``url``, ``href``, and
    ``openDate`` (e.g. ``"Sep 18, 2024"``). The data is not perfectly clean: some
    records have blank ``status`` or ``type`` values, and a few ``caseNumber`` values
    don't follow the usual ``YY-NNNN-XX-XXX`` format.

    Attributes
    ----------
    BASE_URL (str)
        The root URL of the OPSB website
    COMPONENT_ID_PATTERN (re.Pattern)
        Matches a ``cmpntid: '<uuid>'`` entry in the ``/cases`` page's inline
        JavaScript, capturing the UUID. The key may or may not be quoted.
    DEFAULT_CASES_COMPONENT_ID (str)
        The (seemingly) stable ID for the JSON list of cases currently in OPSB's
        system. If the JSON request with this ID returns a 404, we fall back to
        discovering the current ID from the ``/cases`` page.
    JSON_PARAMS (dict[str, str])
        The query parameters, other than ``cmpntid``, that make WCM return a library
        component. The page's own request also sends ``WCM_Page.ResetAll`` and
        ``location``, but they don't appear to be needed.
    JSON_PATH (str)
        The WCM path, relative to ``BASE_URL``, that serves the cases component

    Methods
    -------
    discover_cases_cmpntid
        Scrape the current component ID of the cases JSON from the ``/cases`` page
    get_cases_list
        Fetch every case on the OPSB case list

    Notes
    -----
    The OPSB website runs on IBM WebSphere Portal. Its ``/cases`` page doesn't contain
    the case table itself; instead, inline JavaScript on that page fetches a Web
    Content Manager (WCM) "component" -- a JSON array of every case -- and renders the
    table client-side. We skip the page and request that component directly. It is
    identified by a ``cmpntid`` query parameter, which is hardcoded in the page's
    inline JavaScript.

    The component always returns every case, regardless of the status filter chosen
    on the ``/cases`` page, so any filtering has to happen after fetching. It is served
    through CloudFront with ``max-age=86400``, so the data may be up to a day stale.

    """

    BASE_URL: str = "https://opsb.ohio.gov"
    COMPONENT_ID_PATTERN = re.compile(r"cmpntid['\"]?\s*:\s*['\"]([0-9a-f-]{36})['\"]")
    DEFAULT_CASES_COMPONENT_ID: str = "691ce407-26ae-4653-9dc1-4789a8a6711e"
    JSON_PARAMS: dict[str, str] = {"source": "library", "srv": "cmpnt"}
    JSON_PATH: str = "/wps/wcm/connect/gov/Ohio%20Content%20English/opsb"

    @classmethod
    def get_cases_list(cls) -> list[dict[str, str]]:
        """Fetch every case on the OPSB case list

        First requests the cases JSON using ``DEFAULT_CASES_COMPONENT_ID``. If that
        returns a 404, which is what the server does when a component ID doesn't
        exist, the current ID is found with ``discover_cases_cmpntid`` and the request
        is retried once. The discovered ID is not saved, so later calls try the default
        ID again first.

        Returns
        -------
        list[dict]
            One dict per case, in the order the server returns them. See the class
            docstring for the keys and their caveats.

        Raises
        ------
        requests.HTTPError
            If the first request fails with anything other than a 404, or if the
            retry with the discovered ID fails
        modocket.https.RequestRejected
            If the site's firewall blocks the request, which it does for (at least
            some) non-US IPs. A subclass of ``requests.HTTPError``.
        requests.Timeout
            If any request takes longer than ``DEFAULT_TIMEOUT`` seconds
        RuntimeError
            If the fallback runs and the ``/cases`` page no longer contains a
            recognizable component ID
        """
        url = cls.BASE_URL + cls.JSON_PATH

        with make_us_session() as session:
            try:
                params = cls.JSON_PARAMS | {"cmpntid": cls.DEFAULT_CASES_COMPONENT_ID}
                response = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
                response.raise_for_status()

            except requests.HTTPError as e:
                if e.response.status_code != 404:
                    raise
                component_id = cls.discover_cases_cmpntid(session)
                params = cls.JSON_PARAMS | {"cmpntid": component_id}
                response = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
                response.raise_for_status()

            # The firewall's block page comes back as a 200, so raise_for_status misses it
            raise_for_rejection(response)

        return response.json()

    @classmethod
    def discover_cases_cmpntid(cls, session: requests.Session | None = None) -> str:
        """Read the cases page's inline JS to find the component ID of the cases JSON.

        There are four ``cmpntid``s on the ``/cases`` page. The others load site
        alerts, "support" content, and a related-resources widget, and one of them
        comes after the cases component. So neither the first nor the last match on
        the page is reliable.

        Instead we anchor on ``opsb-cases-main-filter``, the template that renders the
        cases table. The page's JavaScript fetches the cases component and then passes
        it to that template, so the cases component ID is the last one before the
        anchor. That ordering is a (seemingly) stable feature of this page.

        This costs one request for a page of about 700 KB, so ``get_cases_list`` only
        calls it when the default ID fails.

        Parameters
        ----------
        session (requests.Session | None)
            The session to make the request with. Defaults to a new session from
            ``make_us_session``.

        Returns
        -------
        str
            The component ID (a UUID) of the cases JSON

        Raises
        ------
        requests.HTTPError
            If the ``/cases`` page request fails
        modocket.https.RequestRejected
            If the site's firewall blocks the request
        requests.Timeout
            If the request takes longer than ``DEFAULT_TIMEOUT`` seconds
        RuntimeError
            If the page doesn't contain ``opsb-cases-main-filter``, or no component ID
            appears before it. Either means the page layout has changed.
        """
        session = session or make_us_session()
        response = session.get(cls.BASE_URL + "/cases", timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        raise_for_rejection(response)

        html = response.text
        try:
            anchor = html.index("opsb-cases-main-filter")
            return cls.COMPONENT_ID_PATTERN.findall(html[:anchor])[-1]
        except (ValueError, IndexError) as e:
            raise RuntimeError(
                "OPSB /cases page layout changed; could not find cases component ID"
            ) from e


class OhioPublicUtilityCommission(object):
    """An interface for the case records and documents on PUCO's docketing system

    This object only has class methods; it's a mini-module within
    ``modocket.sources.puco``. The ``download_case_documents`` method is the primary
    endpoint, and saves every document filed in a case.

    Attributes
    ----------
    BASE_URL (str)
        The root URL of PUCO's Docketing Information System (DIS)
    DOCUMENT_ID_PATTERN (re.Pattern)
        Matches a ``DocumentRecord.aspx?DocID=<uuid>`` link on a case record page,
        capturing the document ID
    CONTENT_ID_PATTERN (re.Pattern)
        Matches a ``ViewImage.aspx?CMID=<id>`` ("View Document") link on a document
        page, capturing the content ID of the file itself

    Methods
    -------
    download_case_documents
        Save every file of every document filed in a case
    download_document
        Save the file served at a ``ViewImage.aspx`` URL
    get_case_record_page
        Fetch the page listing every document filed in a case
    get_case_document_ids
        Scrape the document IDs from a case's record page
    get_case_document_page
        Fetch the page describing a single document
    get_document_content_ids
        Scrape the content IDs (CMIDs) of the files attached to a document
    get_document_urls
        The ``ViewImage.aspx`` URLs that serve a document's files

    Notes
    -----
    DIS organizes filings in three levels, each with its own identifier:

    1. A **case**, e.g. ``26-0746-EL-BGN``. Its page is ``CaseRecord.aspx?CaseNo=``.
    2. A **document** filed in that case, identified by a UUID ``DocID``. Its page is
       ``DocumentRecord.aspx?DocID=``, which lists metadata and links to the file(s).
    3. A **file**, identified by a ``CMID`` like ``A1001001A26I16B45934I00564``,
       served directly (e.g. as a PDF) by ``ViewImage.aspx?CMID=``.

    None of these pages have an API or a JSON payload, so each level is scraped from
    the HTML of the level above it.

    The site sits behind an F5 firewall that blocks (at least some) non-US IPs with a
    "Request Rejected" page, served with a 200 status. Every method that makes a
    request checks for it and raises ``modocket.https.RequestRejected``.

    The server also resets connections that a session reuses. ``make_us_session``
    mounts a retrying adapter, so sharing a session across these methods works; a
    plain ``requests.Session`` without that adapter will hit
    ``ConnectionResetError``.
    """

    BASE_URL: str = "https://dis.puc.state.oh.us"
    DOCUMENT_ID_PATTERN = re.compile(r"DocumentRecord\.aspx\?DocID=([0-9a-f-]{36})")
    CONTENT_ID_PATTERN = re.compile(r"ViewImage\.aspx\?CMID=([A-Za-z0-9]+)")

    @classmethod
    def download_case_documents(
        cls,
        case_number: str,
        root: Path | str | None = None,
        session: requests.Session | None = None,
    ) -> Path:
        """Save every file of every document filed in a case

        Walks the whole hierarchy: the case's record page, each document's page, and
        each file. Files are saved in a Hive-style layout under ``root``::

            root/
                case_number=<case_number>/
                    doc_id=<document_id>/
                        <content_id>.pdf

        One session is shared across every request, so the connection (and the
        site's cookies) are reused.

        Parameters
        ----------
        case_number (str)
            The PUCO case number, e.g. ``26-0746-EL-BGN``
        root (str | Path | None)
            The directory to save under. Defaults to the current working directory.
            It, and the directories below it, are created as needed.
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Returns
        -------
        Path
            ``root``, not the case's own directory beneath it

        Raises
        ------
        requests.HTTPError, modocket.https.RequestRejected, requests.Timeout
            If any request fails; see the methods above. Files saved before the
            failure are kept.
        ValueError
            If any document's file is not a PDF; see ``download_document``
        """
        if root is None:
            root = Path.cwd().resolve()
        else:
            root = root if isinstance(root, Path) else Path(root)

        case_root = root / f"case_number={case_number}"
        session = session or make_us_session()

        for doc_id in OhioPublicUtilityCommission.get_case_document_ids(case_number, session):
            path = case_root / f"doc_id={doc_id}"
            path.mkdir(exist_ok=True, parents=True)
            for url in OhioPublicUtilityCommission.get_document_urls(doc_id, session):

                content_id = url.rsplit("CMID=", 1)[1]
                file_path = path / f"{content_id}.pdf"
                _ = OhioPublicUtilityCommission.download_document(url, file_path, session)

        return root

    @classmethod
    def download_document(
        cls,
        url: str,
        path: str | Path,
        session: requests.Session | None = None,
    ) -> Path:
        """Save the file served at a ``ViewImage.aspx`` URL to ``path``

        The file is streamed to disk in chunks, so large filings aren't held in memory.
        The response has no ``Content-Disposition`` filename, so the caller chooses the
        path; the CMID (e.g. ``f"{content_id}.pdf"``) is a convenient unique name.

        Parameters
        ----------
        url (str)
            A ``ViewImage.aspx?CMID=`` URL, e.g. from ``get_document_urls``
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
        requests.HTTPError
            If the request fails
        modocket.https.RequestRejected
            If the site's firewall blocks the request. Its block page is a 200, so
            without this check it would be saved as if it were the document.
        ValueError
            If the response is not a PDF. Nothing is written in that case.
        requests.Timeout
            If the server takes longer than ``DEFAULT_TIMEOUT`` seconds to respond,
            or to send any one chunk of the file
        """
        session = session or make_us_session()
        with session.get(url, timeout=DEFAULT_TIMEOUT, stream=True) as response:
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            if "pdf" not in content_type:
                raise_for_rejection(response)
                raise ValueError(f"{url} returned {content_type!r}, not a PDF")

            path = Path(path)
            with path.open("wb") as file:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    file.write(chunk)

        return path

    @classmethod
    def get_case_record_page(
        cls,
        case_number: str,
        session: requests.Session | None = None,
    ) -> requests.Response:
        """Fetch the record page of a case, which lists every document filed in it

        Parameters
        ----------
        case_number (str)
            The PUCO case number, e.g. ``26-0746-EL-BGN``. Upper/lowercase doesn't matter.
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Returns
        -------
        requests.Response
            The response for ``CaseRecord.aspx?CaseNo=<case_number>``. Its ``.text``
            is the page's HTML.

        Raises
        ------
        requests.HTTPError
            If the request fails
        modocket.https.RequestRejected
            If the site's firewall blocks the request
        requests.Timeout
            If the request takes longer than ``DEFAULT_TIMEOUT`` seconds
        """
        url = cls.BASE_URL + "/CaseRecord.aspx?CaseNo=" + case_number

        session = session or make_us_session()
        response = session.get(url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        raise_for_rejection(response)

        return response

    @classmethod
    def get_case_document_ids(
        cls,
        case_number: str,
        session: requests.Session | None = None,
    ) -> Generator[str]:
        """Scrape the IDs of the documents filed in a case

        The IDs are taken from the ``DocumentRecord.aspx?DocID=`` links on the case's
        record page, in the order they appear there.

        The case page is fetched when iteration starts, not when this is called.

        Parameters
        ----------
        case_number (str)
            The PUCO case number, e.g. ``26-0746-EL-BGN``
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Yields
        ------
        str
            A document ID (a UUID), for use with ``get_case_document_page``

        Raises
        ------
        requests.HTTPError, modocket.https.RequestRejected, requests.Timeout
            See ``get_case_record_page``
        """
        response = cls.get_case_record_page(case_number, session)
        yield from dict.fromkeys(cls.DOCUMENT_ID_PATTERN.findall(response.text))

    @classmethod
    def get_case_document_page(
        cls,
        document_id: str,
        session: requests.Session | None = None,
    ) -> requests.Response:
        """Fetch the page describing a single document

        The page lists the document's metadata (filing date, document type, and so
        on) and its "View Document" links to the file(s) themselves.

        Parameters
        ----------
        document_id (str)
            The document's ID (a UUID), e.g. from ``get_case_document_ids``
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Returns
        -------
        requests.Response
            The response for ``DocumentRecord.aspx?DocID=<document_id>``. Its
            ``.text`` is the page's HTML.

        Raises
        ------
        requests.HTTPError
            If the request fails
        modocket.https.RequestRejected
            If the site's firewall blocks the request
        requests.Timeout
            If the request takes longer than ``DEFAULT_TIMEOUT`` seconds
        """
        url = cls.BASE_URL + "/DocumentRecord.aspx?DocID=" + document_id

        session = session or make_us_session()
        response = session.get(url, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        raise_for_rejection(response)

        return response

    @classmethod
    def get_document_content_ids(
        cls,
        document_id: str,
        session: requests.Session | None = None,
    ) -> Generator[str]:
        """Scrape the content IDs (CMIDs) of the files attached to a document

        The CMIDs are taken from the ``ViewImage.aspx?CMID=`` ("View Document") links
        on the document's page. The page repeats each link several times (once per
        layout), so the CMIDs are de-duplicated, keeping the order they first appear
        in. A CMID is a different identifier from the document's ``DocID``.

        The document page is fetched when iteration starts, not when this is called.

        Parameters
        ----------
        document_id (str)
            The document's ID (a UUID), e.g. from ``get_case_document_ids``
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Yields
        ------
        str
            A content ID, e.g. ``A1001001A26I16B45934I00564``

        Raises
        ------
        requests.HTTPError, modocket.https.RequestRejected, requests.Timeout
            See ``get_case_document_page``
        """
        response = cls.get_case_document_page(document_id, session)
        yield from dict.fromkeys(cls.CONTENT_ID_PATTERN.findall(response.text))

    @classmethod
    def get_document_urls(
        cls,
        document_id: str,
        session: requests.Session | None = None,
    ) -> Generator[str]:
        """The "View Document" URLs of a document, each of which serves a file

        Builds a ``ViewImage.aspx?CMID=`` URL for each content ID from
        ``get_document_content_ids``. Each URL serves the file itself (so far always a
        PDF), with no further page in between.

        Parameters
        ----------
        document_id (str)
            The document's ID (a UUID), e.g. from ``get_case_document_ids``
        session (requests.Session | None)
            The session to make the request(s) with. Defaults to a new session from
            ``make_us_session``.

        Yields
        ------
        str
            An absolute URL, for use with ``download_document``

        Raises
        ------
        requests.HTTPError, modocket.https.RequestRejected, requests.Timeout
            See ``get_case_document_page``
        """
        for content_id in cls.get_document_content_ids(document_id, session):
            url = cls.BASE_URL + "/ViewImage.aspx?CMID=" + content_id
            yield url


if __name__ == "__main__":
    case_number = "26-0426-el-btx"
    doc_id = "91e01ff3-bc71-465c-a58d-e1c79ff52802"

    case_number = "26-0746-EL-BGN"
    root = "ports_energy_center"

    OhioPublicUtilityCommission.download_case_documents(case_number, root)

    raise NotImplementedError("fda")

    for response in OhioPowerSitingBoard.get_cases_list():
        print(response)


    session = make_us_session()
    for doc_id in OhioPublicUtilityCommission.get_case_document_ids(case_number, session):
        print(doc_id)
        for url in OhioPublicUtilityCommission.get_document_urls(doc_id, session):
            print(f"  {url}")
            directory = Path(f"case_number={case_number}") / f"doc_id={doc_id}"
            directory.mkdir(exist_ok=True, parents=True)

            content_id = url.rsplit("CMID=", 1)[1]
            path = directory / f"{content_id}.pdf"
            path = OhioPublicUtilityCommission.download_document(url, path, session)
            print(f"  Saved {path}")

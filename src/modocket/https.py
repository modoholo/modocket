"""Common utilities for surfing the web"""

import os
from pathlib import Path
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# Some sites return 404 to non-browser User-Agents, including the requests default
DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}
DEFAULT_TIMEOUT: int = 30  # seconds
# Some servers reset kept-alive connections when a session reuses them. Retry them
# to make urllib3 open a fresh connection and resend the request,
DEFAULT_RETRY: Retry = Retry(total=3, backoff_factor=0.5)


class RequestRejected(requests.HTTPError):
    """A web application firewall (e.g. F5 BIG-IP) blocked the request"""


def make_us_session() -> requests.Session:
    """Create a requests session, perhaps routed through a US proxy

    The US proxy is used when ``MODOCKET_PROXY_URL`` is set in the environment
    variables; it's the same US-market proxy that data-service reads from
    ``ERCOT_PROXY_URL``. The proxy is only reachable from the Modo VPN, so:

    - With ``MODOCKET_PROXY_URL`` set, the Modo VPN must be on.
    - Without it, requests go direct, and US sites may block the London VPN IP, so
      the VPN should be off.
    """
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    adapter = HTTPAdapter(max_retries=DEFAULT_RETRY)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    if proxy_url := os.environ.get("MODOCKET_PROXY_URL"):
        session.proxies = {"http": proxy_url, "https": proxy_url}

    return session


def raise_for_rejection(response: requests.Response) -> None:
    if "The requested URL was rejected" in response.text:
        support_id = re.search(r"support ID is: <?(\d+)", response.text)
        raise RequestRejected(
            f"{response.url} was blocked by a firewall (support ID "
            f"{support_id.group(1) if support_id else 'unknown'}); "
            "the request probably needs to come from a US IP",
            response=response,
        )


def get_page(
    url: str,
    session: requests.Session | None = None,
    **kwargs,
) -> requests.Response:
    """GET a URL, raising for HTTP errors and firewall block pages

    Parameters
    ----------
    url (str)
        The URL to request
    session (requests.Session | None)
        The session to make the request with. Defaults to a new session from
        ``make_us_session``.
    **kwargs
        Passed on to ``session.get``, e.g. ``params``. ``timeout`` defaults to
        ``DEFAULT_TIMEOUT``.

    Returns
    -------
    requests.Response
        The response, with its body already read

    Raises
    ------
    requests.HTTPError
        If the request fails
    RequestRejected
        If a firewall blocks the request. Block pages are often served with a 200,
        so ``raise_for_status`` alone misses them.
    requests.Timeout
        If the request takes longer than the timeout
    """
    session = session or make_us_session()
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    response = session.get(url, **kwargs)
    response.raise_for_status()
    raise_for_rejection(response)

    return response


def download_file(
    url: str,
    path: str | Path,
    session: requests.Session | None = None,
    content_type: str | None = "pdf",
) -> Path:
    """Stream the file served at ``url`` to ``path``

    The file is written in chunks, so large filings aren't held in memory.

    Parameters
    ----------
    url (str)
        The URL that serves the file itself
    path (str | Path)
        Where to save the file. Its parent directory must already exist, and an
        existing file at ``path`` is overwritten.
    session (requests.Session | None)
        The session to make the request with. Defaults to a new session from
        ``make_us_session``.
    content_type (str | None)
        A substring the response's ``Content-Type`` must contain, e.g. ``"pdf"``.
        ``None`` accepts any type.

    Returns
    -------
    Path
        ``path``, as a ``Path``

    Raises
    ------
    requests.HTTPError
        If the request fails
    RequestRejected
        If a firewall blocks the request, rather than saving its block page
    ValueError
        If the ``Content-Type`` doesn't contain ``content_type``. Nothing is
        written in that case.
    requests.Timeout
        If the server takes longer than ``DEFAULT_TIMEOUT`` seconds to respond,
        or to send any one chunk of the file
    """
    session = session or make_us_session()
    with session.get(url, timeout=DEFAULT_TIMEOUT, stream=True) as response:
        response.raise_for_status()

        served_type = response.headers.get("Content-Type", "")
        if content_type is not None and content_type not in served_type:
            raise_for_rejection(response)
            raise ValueError(f"{url} returned {served_type!r}, not {content_type!r}")

        path = Path(path)
        with path.open("wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                file.write(chunk)

    return path

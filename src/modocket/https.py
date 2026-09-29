"""Common utilities for surfing the web"""

import os
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

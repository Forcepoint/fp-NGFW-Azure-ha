"""Azure cloud integration for HA script.

Provides HTTP session management with retry logic.
"""
import requests
import requests.adapters
import urllib3.util.retry


def session_with_retry() -> requests.Session:
    """Create a requests session with retry logic.

    :return: configured requests Session
    """
    session = requests.Session()
    retry = urllib3.util.retry.Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[409, 429, 500, 502, 503, 504],
        allowed_methods=False,
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

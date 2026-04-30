"""Azure API Authentication

A standalone authentication implementation for Azure Resource Manager API
using Managed Identity credentials.  Used on a VM to authenticate against
Azure API without storing any secrets or service principal credentials.

Implementation provides a 'requests' authentication method that acquires
OAuth2 bearer tokens from the Azure Instance Metadata Service (IMDS) [1].

Managed Identity authentication is a single-step approach: request a token
from the local metadata endpoint and attach it to outgoing API requests.

Algorithm:

    1. Request an OAuth2 access token from IMDS (http://169.254.169.254)
    2. Cache the token until it is close to expiry (120 s headroom)
    3. Attach the token as a Bearer Authorization header to each request

Example usage:

    >>> import requests
    >>> from ha_script.azure.auth import RequestSigner
    >>> requests.get(url, auth=RequestSigner())

[1] https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/how-to-use-vm-token
"""
import time
import logging
from typing import Optional

import requests.auth

import ha_script.azure as azure


LOGGER = logging.getLogger(__name__)


class Token:
    """Represents an OAuth2 access token."""

    def __init__(self, value: str, expires_on: int):
        self.value = value
        self.expires_on = expires_on

    def expired(self) -> bool:
        """Check if the token is expired or close to expiry."""
        return time.time() >= (self.expires_on - 120)


class RequestSigner(requests.auth.AuthBase):
    """Requests auth handler that adds Azure bearer token."""

    def __init__(self) -> None:
        self._token: Optional[Token] = None

    def invalidate(self) -> None:
        """Discard the cached token, forcing re-acquisition on next use."""
        if self._token:
            LOGGER.debug("Invalidating existing token")
        self._token = None

    def __call__(
        self, r: requests.PreparedRequest
    ) -> requests.PreparedRequest:
        if self._token is None or self._token.expired():
            self._token = _request_token()
        r.headers["Authorization"] = f"Bearer {self._token.value}"
        return r


def _request_token():
    """Fetch a managed identity token from IMDS.

    :return: Token object
    """
    LOGGER.debug("Requesting new token from IMDS")
    data = azure.metadata.get_identity_token()
    return Token(data["access_token"], int(data["expires_on"]))

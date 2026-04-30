"""
Tests for Azure managed identity authentication.
"""
import time

import requests
import responses

from ha_script.azure.auth import Token, RequestSigner, _request_token
from ha_script.azure.metadata import METADATA_URL


IMDS_TOKEN_URL = f"{METADATA_URL}/identity/oauth2/token"


def test_token_not_expired():
    token = Token("abc", int(time.time()) + 300)
    assert not token.expired()


def test_token_expired():
    token = Token("abc", int(time.time()) - 10)
    assert token.expired()


def test_token_expired_within_headroom():
    """Token expiring within 120s headroom should be expired."""
    token = Token("abc", int(time.time()) + 60)
    assert token.expired()


@responses.activate
def test_request_token_success():
    expires_on = str(int(time.time()) + 3600)
    responses.add(
        responses.GET,
        IMDS_TOKEN_URL,
        json={
            "access_token": "test-token-value",
            "expires_on": expires_on,
        },
        status=200,
    )

    token = _request_token()
    assert token.value == "test-token-value"
    assert token.expires_on == int(expires_on)
    assert not token.expired()

    # Verify the request included the resource param
    assert "resource=https" in responses.calls[0].request.url


@responses.activate
def test_request_token_failure():
    responses.add(
        responses.GET,
        IMDS_TOKEN_URL,
        json={"error": "not found"},
        status=404,
    )

    try:
        _request_token()
        assert False, "Expected exception"
    except Exception:
        pass


@responses.activate
def test_request_signer():
    """RequestSigner adds Authorization header."""
    expires_on = str(int(time.time()) + 3600)
    responses.add(
        responses.GET,
        IMDS_TOKEN_URL,
        json={
            "access_token": "my-bearer-token",
            "expires_on": expires_on,
        },
        status=200,
    )

    signer = RequestSigner()
    req = requests.Request("GET", "https://example.com")
    prepared = req.prepare()
    signed = signer(prepared)
    assert signed.headers["Authorization"] == "Bearer my-bearer-token"


@responses.activate
def test_request_signer_invalidate():
    """RequestSigner.invalidate() clears the cached token."""
    expires_on = str(int(time.time()) + 3600)
    responses.add(
        responses.GET,
        IMDS_TOKEN_URL,
        json={
            "access_token": "token-1",
            "expires_on": expires_on,
        },
        status=200,
    )
    responses.add(
        responses.GET,
        IMDS_TOKEN_URL,
        json={
            "access_token": "token-2",
            "expires_on": expires_on,
        },
        status=200,
    )

    signer = RequestSigner()
    req = requests.Request("GET", "https://example.com").prepare()

    # First call fetches token-1
    signed = signer(req)
    assert signed.headers["Authorization"] == "Bearer token-1"

    # Invalidate and call again — should fetch token-2
    signer.invalidate()
    signed = signer(req)
    assert signed.headers["Authorization"] == "Bearer token-2"
    assert len(responses.calls) == 2

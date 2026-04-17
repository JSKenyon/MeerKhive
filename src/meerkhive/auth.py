"""Keycloak authentication for the MeerKAT archive.

This module owns every interaction with the SARAO Keycloak realm. The rest of
the codebase only ever asks for one thing — a valid bearer token — via
:func:`get_access_token`.

Why a hand-rolled implementation? The full OAuth 2.0 / OIDC dance looks
intimidating from the outside but is actually a short, well-defined sequence:

    1. The user opens a URL in a browser and authenticates with Keycloak.
    2. Keycloak redirects the browser back to a URL we control, attaching a
       short-lived ``code`` (and the ``state`` we sent, so we can match the
       response to the request).
    3. We exchange that ``code`` plus a ``code_verifier`` for an access token
       and a refresh token via a server-to-server POST.
    4. From then on, we use the refresh token to mint new access tokens
       without prompting the user.

The "PKCE" (Proof Key for Code Exchange) extension protects step 3: when we
build the auth URL we include ``sha256(code_verifier)`` as a *challenge*, and
when we exchange the code we include the original ``code_verifier``. Keycloak
checks they match. This means an attacker who somehow intercepts the redirect
URL still cannot exchange the code for tokens — they don't have the verifier
that only ever lived in our process memory. PKCE is mandatory for native /
CLI clients because they cannot keep a traditional client secret.

We listen for Keycloak's redirect on ``http://127.0.0.1:<random_port>`` — the
"loopback" pattern documented in RFC 8252. Loopback URIs are the standard way
for native applications to receive a one-shot redirect: the OS picks an unused
port (``bind(("127.0.0.1", 0))``), the URI is unguessable and unreachable from
the network, and the listener shuts down as soon as it has the code.
"""

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import socketserver
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

import requests
from rich.console import Console
from rich.text import Text

logger = logging.getLogger(__name__)

# Treat the access token as expired this many seconds before its real ``exp``.
# The skew absorbs clock drift and the time it takes to actually use the
# token after we hand it out.
_EXP_SKEW_SECONDS = 30


@dataclass(frozen=True)
class KeycloakAuth:
    """Static configuration for a Keycloak client.

    Attributes:
        issuer_url: The realm URL, e.g. ``https://keycloak.example/realms/FOO``.
            Endpoint paths are discovered from
            ``{issuer_url}/.well-known/openid-configuration`` rather than
            being hard-coded so that realm URL changes do not break us.
        client_id: The OIDC client identifier registered in Keycloak. The
            client must be configured as a public client with PKCE enabled
            and the loopback redirect URI ``http://127.0.0.1:*`` allowed.
        scopes: Scopes to request. ``offline_access`` is what causes Keycloak
            to issue a refresh token whose ``refresh_expires_in`` is 0,
            i.e. one that never expires while the session lives — this is
            what lets a CLI run unattended for weeks at a time.
        token_path: Where to persist the token JSON between invocations.
        verify_ssl: Whether to verify TLS certificates when talking to
            Keycloak. Set to ``False`` only for development against a
            self-signed Keycloak.
    """

    issuer_url: str
    client_id: str
    scopes: tuple[str, ...] = ("openid", "email", "profile", "offline_access")
    token_path: Path = field(
        default_factory=lambda: _default_token_path(),
    )
    verify_ssl: bool = True

    @classmethod
    def default(cls, *, verify_ssl: bool = True) -> Self:
        """Build the default configuration for the SARAO MeerKAT archive.

        Args:
            verify_ssl: Whether to verify TLS certificates when talking to
                Keycloak. Callers can set this to match the SSL verification
                behaviour they use for archive requests.

        Returns:
            A ``KeycloakAuth`` instance pre-configured with the SARAO
            SKASA realm and the ``archive-openid-pkce`` client.
        """
        return cls(
            issuer_url="https://keycloak.sarao.ac.za/realms/SKASA",
            client_id="archive-openid-pkce",
            verify_ssl=verify_ssl,
        )


def _default_token_path() -> Path:
    """Return the default on-disk location for the persisted token file.

    Honours ``XDG_STATE_HOME`` if set, falling back to ``~/.local/state``.

    Returns:
        The path at which tokens should be read and written.
    """
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "meerkhive" / "tokens.json"


# ---------------------------------------------------------------------------
# Public API: the rest of the codebase only ever calls these two.
# ---------------------------------------------------------------------------


def get_access_token(auth: KeycloakAuth, *, force_refresh: bool = False) -> str:
    """Return a valid bearer token, refreshing or logging in as needed.

    The decision tree is:

    1. If we have cached tokens and the access token has not yet expired
       (allowing for ``_EXP_SKEW_SECONDS`` of slack), return it as-is.
    2. Otherwise, if we have a refresh token, try to exchange it for a new
       access token. On success, persist and return.
    3. Otherwise, drive an interactive browser login. On success, persist
       and return.

    Args:
        auth: The Keycloak configuration to use.
        force_refresh: If ``True``, skip step 1 and force a refresh attempt.
            Used by the GraphQL transport's 401-retry path.

    Returns:
        A bearer access token as a string.
    """
    tokens = _load_tokens(auth.token_path)

    if tokens and not force_refresh and not _is_expired(tokens.get("access_token")):
        return tokens["access_token"]

    if tokens and tokens.get("refresh_token"):
        refreshed = _refresh(auth, tokens["refresh_token"])
        if refreshed is not None:
            _save_tokens(auth.token_path, refreshed)
            return refreshed["access_token"]
        logger.info("Refresh failed; falling back to interactive login.")

    fresh = _browser_login(auth)
    _save_tokens(auth.token_path, fresh)
    return fresh["access_token"]


# ---------------------------------------------------------------------------
# OIDC discovery
# ---------------------------------------------------------------------------


# Cache the discovery document per (issuer_url, verify_ssl) pair so we only
# fetch it once per process. Endpoints don't change between calls.
_DISCOVERY_CACHE: dict[tuple[str, bool], dict[str, Any]] = {}


def _discover(auth: KeycloakAuth) -> dict[str, Any]:
    """Fetch and cache the OIDC discovery document.

    Every OIDC provider exposes a JSON document at
    ``{issuer}/.well-known/openid-configuration`` that lists the URLs of its
    authorization, token, and userinfo endpoints. Using discovery means we
    do not hard-code Keycloak-specific URL paths — if the realm is moved or
    Keycloak changes its layout, we keep working.

    Args:
        auth: The Keycloak configuration whose issuer URL and TLS
            verification policy drive the discovery request.

    Returns:
        The parsed OIDC discovery document as a dict.

    Raises:
        requests.HTTPError: If the discovery endpoint returns a non-2xx
            response.
    """
    key = (auth.issuer_url, auth.verify_ssl)
    if key not in _DISCOVERY_CACHE:
        url = auth.issuer_url.rstrip("/") + "/.well-known/openid-configuration"
        response = requests.get(url, verify=auth.verify_ssl, timeout=30)
        response.raise_for_status()
        _DISCOVERY_CACHE[key] = response.json()
    return _DISCOVERY_CACHE[key]


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------


def _load_tokens(path: Path) -> dict[str, Any] | None:
    """Load the tokens JSON file.

    Args:
        path: Path to the persisted tokens file.

    Returns:
        The parsed tokens dict, or ``None`` if the file is missing or
        malformed.
    """
    try:
        with path.open() as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_tokens(path: Path, data: dict[str, Any]) -> None:
    """Persist the tokens JSON atomically with restrictive permissions.

    We write to a sibling tempfile and ``os.replace`` it onto the target,
    which on POSIX is atomic. Without this, a crash mid-write could leave
    a truncated tokens file and force the user to log in again.

    The file is created with mode ``0600`` because it contains a refresh
    token that is, for our purposes, equivalent to a password.

    Args:
        path: Destination path for the tokens file. Parent directories are
            created if they do not already exist.
        data: The token payload to serialise as JSON.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``mkstemp`` already creates the file with mode 0600.
    fd, tmp = tempfile.mkstemp(prefix=".tokens-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup so we don't leave a stray .tokens-XYZ behind.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# JWT inspection (no signature check — we only need ``exp``)
# ---------------------------------------------------------------------------


def _is_expired(access_token: str | None) -> bool:
    """Check whether an access token is expired or unusable.

    Args:
        access_token: A JWT access token string, or ``None``.

    Returns:
        ``True`` if the token is missing, malformed, or within
        :data:`_EXP_SKEW_SECONDS` of its ``exp`` claim; ``False`` otherwise.
    """
    if not access_token:
        return True
    try:
        exp = _jwt_exp(access_token)
    except Exception:
        # Any failure to parse the token (malformed base64url, invalid JSON,
        # missing/non-integer ``exp`` claim, etc.) means we can't trust it.
        # Treat it as expired so the caller falls through to a refresh or
        # interactive login.
        return True
    return time.time() + _EXP_SKEW_SECONDS >= exp


def _jwt_exp(token: str) -> int:
    """Extract the ``exp`` claim from a JWT without validating the signature.

    A JWT is three base64url-encoded segments joined by dots:
    ``<header>.<payload>.<signature>``. We only care about ``exp`` from the
    payload, and we are not relying on the token for any security decision —
    if the token is forged, the archive (which *does* validate the signature)
    will simply return 401 and we will refresh. So treating the JWT as opaque
    bytes whose ``exp`` we believe is fine here.

    Args:
        token: A JWT access token string.

    Returns:
        The integer value of the ``exp`` claim (seconds since the Unix epoch).

    Raises:
        ValueError: If ``token`` is not a three-segment JWT.
        KeyError: If the payload has no ``exp`` claim.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Not a JWT")
    payload_b64 = parts[1]
    # base64url uses '-' and '_' instead of '+' and '/', and strips padding.
    # ``urlsafe_b64decode`` handles the alphabet; we re-add the padding.
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    return int(payload["exp"])


# ---------------------------------------------------------------------------
# Refresh-token grant
# ---------------------------------------------------------------------------


def _refresh(auth: KeycloakAuth, refresh_token: str) -> dict[str, Any] | None:
    """Exchange a refresh token for a new access token.

    Keycloak rejects the refresh most often because the offline session was
    revoked or the token is genuinely too old. On ``None`` the caller is
    expected to fall through to a full interactive login.

    Args:
        auth: The Keycloak configuration to use.
        refresh_token: The refresh token previously issued by Keycloak.

    Returns:
        The full token response dict on success, or ``None`` if Keycloak
        rejected the refresh.
    """
    token_endpoint = _discover(auth)["token_endpoint"]
    response = requests.post(
        token_endpoint,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": auth.client_id,
        },
        verify=auth.verify_ssl,
        timeout=30,
    )
    if response.status_code == 200:
        logger.info("Refreshed access token.")
        return response.json()
    logger.info("Refresh request returned %s: %s", response.status_code, response.text)
    return None


# ---------------------------------------------------------------------------
# Interactive browser login (PKCE + loopback)
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    """Generate a PKCE ``(verifier, challenge)`` pair.

    The verifier is 32 bytes of entropy expressed as base64url (yielding a
    43-character string, the lower bound permitted by RFC 7636). The
    challenge is the SHA-256 digest of the verifier's ASCII bytes, also
    base64url-encoded with padding stripped.

    Returns:
        A tuple of ``(verifier, challenge)`` suitable for the PKCE
        authorization-code flow.
    """
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Single-shot handler that captures the OAuth callback query string."""

    # The server stashes the captured query dict here; the calling thread
    # reads it after the server shuts down.
    captured_query: dict[str, list[str]] | None = None

    def do_GET(self):  # noqa: N802 — required name for BaseHTTPRequestHandler.
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        # Stash the result on the *server* (shared between threads) so the
        # main thread can read it after we shut the server down.
        self.server.captured_query = query  # type: ignore[attr-defined]

        body = (
            b"<!doctype html><html><body>"
            b"<h2>Login complete</h2>"
            b"<p>You can close this tab and return to the terminal.</p>"
            b"</body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002, ARG002
        # Silence the default stderr access log — it is noise in the CLI.
        return


def _wait_for_pasted_url(
    result: dict[str, Any],
    done: threading.Event,
) -> None:
    """Block on stdin waiting for the user to paste a callback URL.

    This runs in a daemon thread so it dies automatically when the main
    thread proceeds. It reads one line from stdin, parses the query
    parameters, and signals ``done``.

    Args:
        result: A mutable dict into which the parsed query parameters are
            stored under the key ``"query"``.
        done: Event to signal when a URL has been successfully parsed.
    """
    try:
        # Use readline() not input() — input() invokes GNU readline, which modifies
        # terminal attributes and does not restore them if this daemon thread is
        # killed on process exit, leaving the shell prompt unresponsive.
        line = sys.stdin.readline().strip()
        if line:
            parsed = urllib.parse.urlparse(line)
            result["query"] = urllib.parse.parse_qs(parsed.query)
            done.set()
    except (EOFError, KeyboardInterrupt):
        # Non-interactive stdin or user cancelled — the loopback path is
        # the only option.
        pass


def _browser_login(auth: KeycloakAuth) -> dict[str, Any]:
    """Run the full PKCE authorization-code flow against Keycloak.

    Two completion paths race against each other:

    - **Loopback server** — a one-shot HTTP server on ``127.0.0.1`` receives
      Keycloak's redirect directly. This works when the browser runs on the
      same machine as the CLI.
    - **Manual paste** — the user copies the callback URL from the browser's
      address bar and pastes it into the terminal. This works when the CLI
      runs on a remote machine (e.g. via SSH) where the loopback port is
      unreachable from the local browser.

    Whichever path delivers the authorization code first wins; the other
    is silently abandoned (both threads are daemonic).

    Args:
        auth: The Keycloak configuration to use.

    Returns:
        The full token response dict issued by the token endpoint.

    Raises:
        RuntimeError: If no OAuth callback is received within the five-
            minute window, or if the callback is missing its ``code`` or
            has a mismatched ``state``.
        requests.HTTPError: If the final token-endpoint exchange returns
            a non-2xx response.
    """
    discovery = _discover(auth)
    auth_endpoint = discovery["authorization_endpoint"]
    token_endpoint = discovery["token_endpoint"]

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    # Bind to port 0 → OS picks an unused ephemeral port.
    server = socketserver.TCPServer(("127.0.0.1", 0), _CallbackHandler)
    server.captured_query = None  # type: ignore[attr-defined]
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    auth_url = (
        auth_endpoint
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": auth.client_id,
                "response_type": "code",
                "scope": " ".join(auth.scopes),
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )

    # Print the URL with soft-wrap so it stays a single clickable token in
    # the terminal even when it overflows the line.
    console = Console(soft_wrap=True)
    message = Text()
    message.append("Open this URL to log in:\n\n")
    message.append(f"{auth_url}\n", style="bold blue")
    message.append(
        "\nIf the redirect page cannot connect (e.g. you are on a remote "
        "machine), copy the URL from the browser address bar and paste it "
        "here:\n",
        style="dim",
    )
    console.print(message)

    # Try to open it in the user's browser. ``webbrowser.open`` returns
    # False on headless systems; we already printed the URL so that is fine.
    try:
        webbrowser.open(auth_url)
    except webbrowser.Error:
        pass

    # Race two completion paths: the loopback server receiving the redirect
    # directly, or the user pasting the callback URL into stdin.
    done = threading.Event()
    paste_result: dict[str, Any] = {}

    def _serve() -> None:
        try:
            server.handle_request()
        except OSError:
            # The main thread closes the listening socket when the paste path
            # wins or the wait times out. Treat that as a normal shutdown.
            pass
        finally:
            done.set()

    server_thread = threading.Thread(target=_serve, daemon=True)
    paste_thread = threading.Thread(
        target=_wait_for_pasted_url,
        args=(paste_result, done),
        daemon=True,
    )
    server_thread.start()
    paste_thread.start()

    done.wait(timeout=300)  # Five minutes is plenty for a human.
    server.server_close()

    # Prefer the loopback server result (it includes the HTML confirmation
    # page), but fall back to whatever the user pasted.
    query = getattr(server, "captured_query", None) or paste_result.get("query")
    if not query:
        raise RuntimeError("Did not receive an OAuth callback within 5 minutes.")

    code = query.get("code", [None])[0]
    state_returned = query.get("state", [None])[0]
    if code is None or state_returned != state:
        raise RuntimeError("OAuth callback was missing 'code' or had a mismatched state.")

    response = requests.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": auth.client_id,
            "code_verifier": verifier,
        },
        verify=auth.verify_ssl,
        timeout=30,
    )
    response.raise_for_status()
    logger.info("Login complete.")
    return response.json()

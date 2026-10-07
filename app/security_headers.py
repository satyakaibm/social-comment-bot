"""Browser-side hardening headers shared by the dashboard and billing apps.

Both apps serve only their own CSS and JS from ``static/`` and are reached
solely through the Cloudflare Tunnel, so one strict policy fits both:

* ``Content-Security-Policy`` restricts every resource type to the same
  origin. Inline ``<script>`` blocks (the dashboard has a handful) are allowed
  through a per-request nonce that templates attach with
  ``nonce="{{ csp_nonce() }}"``; a script injected into page content has no
  way to learn the nonce, so it does not run. Inline ``<style>`` blocks and
  ``style=""`` attributes keep ``'unsafe-inline'`` -- a nonce cannot cover
  style attributes, and the templates use both freely. ``frame-ancestors
  'none'`` plus ``X-Frame-Options: DENY`` stop clickjacking in old and new
  browsers alike; ``form-action 'self'`` keeps a tampered form from posting
  credentials elsewhere.
* ``Strict-Transport-Security`` is only sent when the app is configured for
  secure cookies, i.e. when it is actually behind TLS. A plain-HTTP local
  instance must not pin a browser to HTTPS for localhost.

``setdefault`` everywhere, so a route that sets a header deliberately (the
avatar route's ``nosniff``) wins over the blanket value.
"""

from __future__ import annotations

import secrets

from flask import Flask, Response, g

HSTS_VALUE = "max-age=31536000; includeSubDomains"


def csp_nonce() -> str:
    """Per-request nonce, generated once and reused by template and header."""
    nonce = getattr(g, "csp_nonce", None)
    if nonce is None:
        nonce = secrets.token_urlsafe(16)
        g.csp_nonce = nonce
    return nonce


def content_security_policy(*, inline_scripts: bool) -> str:
    script_src = "'self'"
    if inline_scripts:
        script_src += f" 'nonce-{csp_nonce()}'"
    return "; ".join(
        (
            "default-src 'self'",
            f"script-src {script_src}",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self'",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )
    )


def install(app: Flask, *, hsts: bool, inline_scripts: bool) -> None:
    """Register the nonce helper for templates and the after_request hook."""
    app.jinja_env.globals["csp_nonce"] = csp_nonce

    @app.after_request
    def _add_security_headers(response: Response) -> Response:
        headers = response.headers
        headers.setdefault(
            "Content-Security-Policy",
            content_security_policy(inline_scripts=inline_scripts),
        )
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if hsts:
            headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
        return response

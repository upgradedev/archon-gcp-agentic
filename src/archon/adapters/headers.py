"""The response headers a public page should carry, and why each one.

The DAST scan found five missing. None of them was hypothetical: the page is
served to anyone with the URL, so every one of these was a real gap between what
the product claims about itself and what it actually sent over the wire.

They are fixed here rather than added to the scanner's ignore list, which is the
whole difference between a security gate and a security decoration.

The content security policy is the one worth reading. It allows inline script
and style, which is weaker than it could be, and that is a deliberate trade: the
judge's page is a single self-contained file with no build step and no external
request, so there is nothing to load from anywhere and nothing to hash against a
nonce that a static file could carry. Everything else is closed: no external
origin, no framing, no form submission, no base tag rewriting.
"""
from __future__ import annotations

#: Nothing loads from anywhere else, because the page has no external resource.
#: `frame-ancestors 'none'` is the modern anti-clickjacking control and does the
#: same work as X-Frame-Options, which is sent as well for older agents.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    # Inline only, and only because the page is one self-contained file.
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    # The page talks to this origin's own API and to nothing else.
    "connect-src 'self'",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
])

SECURITY_HEADERS = {
    # Do not let a browser guess a content type it was not given.
    "X-Content-Type-Options": "nosniff",
    # Anti-clickjacking, for agents that predate frame-ancestors.
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    # No camera, microphone, geolocation or payment API is used by this page,
    # so every one of them is switched off rather than left at the default.
    "Permissions-Policy": ", ".join([
        "accelerometer=()", "camera=()", "geolocation=()", "gyroscope=()",
        "magnetometer=()", "microphone=()", "payment=()", "usb=()",
    ]),
    # Another origin may not read this response into its own document.
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    # Do not leak the path a visitor came from to anywhere else.
    "Referrer-Policy": "no-referrer",
}


def apply(headers) -> None:
    """Set every header on a response, without overwriting a deliberate one.

    `setdefault` rather than assignment so a route that has a reason to send
    something different keeps it. No route does today, and this is the shape
    that stays correct when one does.
    """
    for name, value in SECURITY_HEADERS.items():
        headers.setdefault(name, value)

"""The response headers a public page should carry, and why each one.

The DAST scan found five missing. None of them was hypothetical: the page is
served to anyone with the URL, so every one of these was a real gap between what
the product claims about itself and what it actually sent over the wire.

They are fixed here rather than added to the scanner's ignore list, which is the
whole difference between a security gate and a security decoration.

The content security policy is the one worth reading, and it is fully closed.
An earlier version allowed inline script and style because the page was a single
self-contained file. A scan raised `script-src 'unsafe-inline'`, which is a real
finding rather than a false positive: with it, any injected script tag executes.
So the page became three same-origin files instead, and the policy now refuses
inline outright. Nothing loads from another origin, nothing may frame it,
nothing may submit a form from it, and no base tag may rewrite its URLs.
"""
from __future__ import annotations

#: Nothing loads from anywhere else, because the page has no external resource.
#: `frame-ancestors 'none'` is the modern anti-clickjacking control and does the
#: same work as X-Frame-Options, which is sent as well for older agents.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    # No inline anything. The script and the style live in their own same-origin
    # files, which is what lets this be closed: with 'unsafe-inline' any
    # injected script tag executes, and a scan raised exactly that.
    "script-src 'self'",
    "style-src 'self'",
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
    # Another origin may not read this response into its own document, nor
    # share a browsing context group with it, nor be embedded without opting in.
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
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

"""Static asset versioning for the admin UI.

The CSS/JS/vendored libraries live as real files under `aibroker/web/static`
(no more Python-string assets). They are served long-cached from
`/dashboard/static/<path>?v=<ASSETS_VERSION>`; the version is a hash of the
files' OWN content, so any edit auto-invalidates the immutable browser copy
(an edit without a package-version bump used to ship but never reach browsers).
"""
from __future__ import annotations

import hashlib

from aibroker.web.render import NO_STORE, STATIC_DIR

# Re-exported under the historical name the route modules import.
_NO_STORE = NO_STORE

_LONG_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}


def _content_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(STATIC_DIR.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(STATIC_DIR).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


ASSETS_VERSION = _content_hash()

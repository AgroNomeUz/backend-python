"""
core/middleware.py
Serving user uploads in production.

`MEDIA_URL` has no server behind it by default: whitenoise handles STATIC
only, and `agro/urls.py`'s `static()` helper is a no-op unless DEBUG. This
middleware extends whitenoise to cover the uploads that are *meant* to be
public, so a deployment needs no separate web server in front of the app.

Two things it has to get right, neither of which whitenoise does by default.

**Uploads appear after startup.** `WhiteNoise.add_files()` scans the directory
once at boot and stores the result in `self.files`; anything uploaded later is
simply absent, and would 404 until the next restart. It only populates
`self.directories` — the list `find_file()` consults on the filesystem, per
request — when `autorefresh` is on, which is a development setting. So media
directories are registered as directories directly, and media URLs are routed
through `find_file()`. Static keeps its fast prebuilt index; only media pays
for a filesystem lookup, and it has to, because the set of files changes while
the process runs.

**Not everything under MEDIA_ROOT is public.** `equipment.Document` uploads to
`documents/…` in the same MEDIA_ROOT, and those are an organization's private
papers. Publishing the whole of MEDIA_ROOT would hand every one of them to
anyone with the URL. Only the subdirectories named in
`settings.PUBLIC_MEDIA_DIRS` are served; everything else stays unreachable
through this middleware, and gets a real permission-checked endpoint if it
ever needs one.

This is the interim arrangement. §0b of the API contract still ends at
S3/MinIO, and moving there is a `STORAGES` change — the URLs in responses are
already absolute.
"""

import os
from urllib.parse import urlparse

from django.conf import settings
from whitenoise.middleware import WhiteNoiseMiddleware
from whitenoise.string_utils import ensure_leading_trailing_slash


class PublicMediaWhiteNoiseMiddleware(WhiteNoiseMiddleware):
    """WhiteNoise, plus the public subtrees of MEDIA_ROOT."""

    def __init__(self, get_response=None, settings=settings):
        super().__init__(get_response, settings)

        media_url = urlparse(settings.MEDIA_URL or "").path.rstrip("/")
        media_root = settings.MEDIA_ROOT
        self.public_media_prefixes = []

        if not media_url or not media_root:
            return

        for name in getattr(settings, "PUBLIC_MEDIA_DIRS", ()):
            prefix = ensure_leading_trailing_slash(f"{media_url}/{name}")
            root = os.path.abspath(os.path.join(media_root, name))
            root = root.rstrip(os.sep) + os.sep

            self.public_media_prefixes.append(prefix)
            # Registered directly rather than through `add_files()`, which in
            # production would scan this directory into a dictionary and never
            # look at it again — see the module docstring. `directories` is
            # what `find_file()` walks, and it is consulted per request.
            #
            # Inserted at the front for the same reason whitenoise does: later
            # registrations should match first.
            self.directories.insert(0, (root, prefix))

    def __call__(self, request):
        if any(
            request.path_info.startswith(prefix)
            for prefix in self.public_media_prefixes
        ):
            # Always the live lookup, never `self.files`. Whitenoise's own
            # traversal guards apply: `url_is_canonical` rejects `..` and
            # friends, and `path_is_child_of` confirms the resolved path is
            # still inside the registered root.
            media_file = self.find_file(request.path_info)
            if media_file is not None:
                return self.serve(media_file, request)
            return self.get_response(request)

        return super().__call__(request)

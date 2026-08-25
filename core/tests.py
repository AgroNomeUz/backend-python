"""
core/tests.py
Serving user uploads.

`PublicMediaWhiteNoiseMiddleware` exists because stock whitenoise gets two
things wrong for uploads, and both of them fail *quietly* — a photo that
404s until the next deploy, or a private document served to the world. Each
has a test here, and neither would be caught by testing the upload endpoint.
"""

import tempfile
from pathlib import Path

from django.test import Client, TestCase, override_settings

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# DEBUG=False so whitenoise runs in its production mode: `autorefresh` off,
# which is exactly the configuration that would otherwise serve a stale
# directory listing captured at startup.
@override_settings(DEBUG=False, WHITENOISE_AUTOREFRESH=False)
class PublicMediaServingTests(TestCase):
    def setUp(self):
        super().setUp()
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        self.media_root = Path(self._media.name)

        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)

        # A fresh client, so the middleware chain is built against the
        # settings above rather than reused from an earlier test.
        self.client = Client()

    def write(self, relative_path: str, content: bytes = PNG_BYTES) -> str:
        path = self.media_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return f"/media/{relative_path}"

    def read(self, response) -> bytes:
        return b"".join(response.streaming_content)

    def test_a_file_written_after_startup_is_served(self):
        """
        The whole reason this middleware exists.

        Stock whitenoise scans MEDIA_ROOT once at boot, so a photo uploaded
        by a running process is absent from its index and 404s until the next
        restart — which is every listing photo anyone ever uploads.
        """
        url = self.write("listings/abc/photo.png")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.read(response), PNG_BYTES)
        self.assertEqual(response["Content-Type"], "image/png")

    def test_a_missing_file_is_a_404(self):
        response = self.client.get("/media/listings/abc/nothing.png")
        self.assertEqual(response.status_code, 404)

    def test_private_documents_are_not_served(self):
        """
        `equipment.Document` writes an organization's papers to `documents/`
        in this same MEDIA_ROOT. Serving all of MEDIA_ROOT would publish
        every one of them; PUBLIC_MEDIA_DIRS is an allowlist for that reason.
        """
        url = self.write("documents/2026/08/contract.pdf", b"%PDF-1.4 private")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_traversal_out_of_the_public_subtree_is_refused(self):
        self.write("documents/2026/08/contract.pdf", b"%PDF-1.4 private")
        for url in (
            "/media/listings/../documents/2026/08/contract.pdf",
            "/media/listings/%2e%2e/documents/2026/08/contract.pdf",
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertNotEqual(response.status_code, 200)
                if response.status_code == 200:  # pragma: no cover
                    self.assertNotIn(b"private", self.read(response))

    def test_static_files_still_work(self):
        """The subclass must not break what whitenoise was already doing."""
        response = self.client.get("/static/admin/css/base.css")
        self.assertEqual(response.status_code, 200)


@override_settings(DEBUG=True)
class DevelopmentMediaServingTests(PublicMediaServingTests):
    """
    The same guarantees with DEBUG on, where whitenoise switches to
    `autorefresh` and takes a different code path through `add_files`.
    Development used to be served by a `static()` URL pattern; it is the same
    middleware now, so the two cannot drift.
    """

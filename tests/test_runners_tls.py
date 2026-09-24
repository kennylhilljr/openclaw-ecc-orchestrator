"""TLS trust for the API runners' HTTP clients: verification always on, with a
system CA file fallback when Python's default store is empty (python.org
macOS builds before "Install Certificates")."""

import os
import ssl
import tempfile
import unittest
from collections import namedtuple
from unittest import mock

from openclaw_ecc_orchestrator.runners import discovery

Paths = namedtuple("Paths", "cafile capath openssl_cafile_env openssl_cafile "
                            "openssl_capath_env openssl_capath")
EMPTY = Paths(None, None, "SSL_CERT_FILE", "/nonexistent/cert.pem", "SSL_CERT_DIR",
              "/nonexistent/certs")


def a_real_bundle():
    paths = ssl.get_default_verify_paths()
    for path in (paths.cafile, paths.openssl_cafile) + discovery.SYSTEM_CA_FILES:
        if path and os.path.isfile(path):
            return path
    return None


class TlsContextTests(unittest.TestCase):
    def assert_verifying(self, ctx):
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_default_context_verifies(self):
        self.assert_verifying(discovery.tls_context(env={}))

    def test_falls_back_to_system_ca_file_when_default_store_empty(self):
        bundle = a_real_bundle()
        if not bundle:
            self.skipTest("no CA bundle on this host")
        with mock.patch.object(discovery.ssl, "get_default_verify_paths", return_value=EMPTY), \
                mock.patch.object(discovery.ssl, "create_default_context",
                                  wraps=ssl.create_default_context) as create:
            ctx = discovery.tls_context(env={}, candidates=("/nonexistent/a.pem", bundle))
        create.assert_called_with(cafile=bundle)
        self.assert_verifying(ctx)
        self.assertGreater(len(ctx.get_ca_certs()), 0)

    def test_env_override_wins(self):
        with mock.patch.object(discovery.ssl, "get_default_verify_paths", return_value=EMPTY), \
                mock.patch.object(discovery.ssl, "create_default_context",
                                  wraps=ssl.create_default_context) as create:
            discovery.tls_context(env={"SSL_CERT_FILE": "/x"}, candidates=("/y",))
        create.assert_called_with()

    def test_usable_default_store_is_kept(self):
        with tempfile.NamedTemporaryFile() as fh:
            paths = EMPTY._replace(cafile=fh.name)
            with mock.patch.object(discovery.ssl, "get_default_verify_paths",
                                   return_value=paths), \
                    mock.patch.object(discovery.ssl, "create_default_context",
                                      wraps=ssl.create_default_context) as create:
                discovery.tls_context(env={}, candidates=(fh.name,))
        create.assert_called_with()


if __name__ == "__main__":
    unittest.main()

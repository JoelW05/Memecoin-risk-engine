"""Regressions for helius_get's request construction, transport and errors.

Two separate failures were hit getting this working against a live key, and
both are pinned here:

  403  a bare urlopen() sends "Python-urllib/3.x" and Helius's edge refuses it,
       while the identical URL and key succeed from PowerShell.
  URLError  this machine's Python trust store fails SSL against some hosts
       (the same thing bit wallet.py in August), and the old handler printed
       only the exception class, hiding which of DNS / refused / SSL it was.
"""
import json
import socket
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import patch

import wallet_research as wr


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def urlopen_returning(payload=b"[]", record=None):
    """A urlopen double matching the real signature, including context."""
    def fake(req, timeout=30, context=None):
        if record is not None:
            record["url"] = req.full_url
            record["headers"] = {k.lower(): v for k, v in req.headers.items()}
            record["context"] = context
        return FakeResponse(payload)
    return fake


def urlopen_raising(exc):
    def fake(req, timeout=30, context=None):
        raise exc(req.full_url) if callable(exc) else exc
    return fake


def http_error(code):
    def fake(req, timeout=30, context=None):
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, None)
    return fake


class RequestConstructionTests(unittest.TestCase):
    def call(self, payload=b"[]"):
        record = {}
        with patch.object(wr.urllib.request, "urlopen", side_effect=urlopen_returning(payload, record)):
            result = wr.helius_get("https://api.helius.xyz/v0/addresses/MINT/transactions", {"limit": 100}, "KEY")
        return record, result

    def test_a_user_agent_is_always_sent(self):
        record, _ = self.call()
        self.assertIn("user-agent", record["headers"])
        self.assertNotIn("python-urllib", record["headers"]["user-agent"].lower())

    def test_key_and_params_are_in_the_query(self):
        record, _ = self.call()
        self.assertIn("api-key=KEY", record["url"])
        self.assertIn("limit=100", record["url"])

    def test_json_body_is_returned(self):
        _, result = self.call(json.dumps([{"signature": "s"}]).encode())
        self.assertEqual(result, [{"signature": "s"}])

    def test_an_explicit_ssl_context_is_passed(self):
        record, _ = self.call()
        self.assertIsInstance(record["context"], wr.ssl.SSLContext)


class ErrorHandlingTests(unittest.TestCase):
    def test_403_exits_with_an_explanation_not_a_traceback(self):
        with patch.object(wr.urllib.request, "urlopen", side_effect=http_error(403)), \
             patch.object(wr.time, "sleep"):
            with self.assertRaises(SystemExit) as cm:
                wr.helius_get("https://api.helius.xyz/v0/addresses/M/transactions", {}, "KEY")
        message = str(cm.exception)
        self.assertIn("403", message)
        self.assertIn("User-Agent", message)
        self.assertNotIn("KEY", message)  # the key must never reach an error message

    def test_401_exits_pointing_at_the_key(self):
        with patch.object(wr.urllib.request, "urlopen", side_effect=http_error(401)), \
             patch.object(wr.time, "sleep"):
            with self.assertRaises(SystemExit) as cm:
                wr.helius_get("https://api.helius.xyz/v0/addresses/M/transactions", {}, "KEY")
        self.assertIn("HELIUS_API_KEY", str(cm.exception))

    def test_404_is_not_retried_and_does_not_leak_the_url(self):
        calls = []

        def counting(req, timeout=30, context=None):
            calls.append(1)
            raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)

        with patch.object(wr.urllib.request, "urlopen", side_effect=counting), patch.object(wr.time, "sleep"):
            with self.assertRaises(RuntimeError) as cm:
                wr.helius_get("https://api.helius.xyz/v0/addresses/M/transactions", {}, "KEY")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("KEY", str(cm.exception))

    def test_all_429s_report_the_code_rather_than_crashing_on_an_unbound_name(self):
        with patch.object(wr.urllib.request, "urlopen", side_effect=http_error(429)), \
             patch.object(wr.time, "sleep"):
            with self.assertRaises(RuntimeError) as cm:
                wr.helius_get("https://api.helius.xyz/v0/addresses/M/transactions", {}, "KEY")
        self.assertIn("429", str(cm.exception))

    def test_url_error_reason_reaches_the_final_message(self):
        err = urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
        with patch.object(wr.urllib.request, "urlopen", side_effect=urlopen_raising(err)), \
             patch.object(wr.time, "sleep"):
            with self.assertRaises(RuntimeError) as cm:
                wr.helius_get("https://api.helius.xyz/v0/addresses/M/transactions", {}, "KEY")
        self.assertIn("getaddrinfo failed", str(cm.exception))

    def test_ssl_failure_is_named_in_the_final_message(self):
        err = urllib.error.URLError(wr.ssl.SSLCertVerificationError("unable to get local issuer certificate"))
        with patch.object(wr.urllib.request, "urlopen", side_effect=urlopen_raising(err)), \
             patch.object(wr.time, "sleep"):
            with self.assertRaises(RuntimeError) as cm:
                wr.helius_get("https://api.helius.xyz/v0/addresses/M/transactions", {}, "KEY")
        self.assertIn("local issuer", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)

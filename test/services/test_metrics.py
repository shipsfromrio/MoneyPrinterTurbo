# -*- coding: utf-8 -*-
"""Tests for the metrics webhook reporter (app/services/metrics.py).

Every network call is mocked at urllib.request.urlopen: no test touches a real
endpoint. Each test FAILS a broken implementation of the webhook contract.

Log lines are captured through a loguru sink (the module uses loguru, not the
stdlib logging) instead of assertLogs.
"""
import json
import unittest
import urllib.error
from unittest import mock

from loguru import logger

from app.services import metrics


class _FakeResponse:
    """Stand-in for the urlopen response object (context manager with .read())."""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _resp_ok(id_="abc123", new=True):
    return _FakeResponse(json.dumps({"ok": True, "id": id_, "novo": new}).encode("utf-8"))


ENV_CLEAN = {
    "METRICS_WEBHOOK_URL": "https://example.test",
    "METRICS_WEBHOOK_SECRET": "test-secret",
}  # no OFF


class _CaptureLogMixin:
    """Capture loguru log lines into a list for trail assertions."""

    def setUp(self):
        self.lines = []
        self._sink_id = logger.add(self.lines.append, level="INFO")

    def tearDown(self):
        logger.remove(self._sink_id)

    def _has_line(self, fragment: str) -> bool:
        return any(fragment in line for line in self.lines)


class HappyPathTest(_CaptureLogMixin, unittest.TestCase):
    @mock.patch.dict("os.environ", ENV_CLEAN, clear=True)
    @mock.patch("urllib.request.urlopen")
    def test_makes_one_call_with_right_header_and_body(self, urlopen):
        urlopen.return_value = _resp_ok()

        metrics.report_video_generated("biblioteca", "task-uuid:final-1.mp4")

        # visible trail on success (no silent drop).
        self.assertTrue(self._has_line("sent"))

        self.assertEqual(urlopen.call_count, 1)
        req = urlopen.call_args.args[0]
        # short timeout, mandatory (must not block generation).
        self.assertEqual(urlopen.call_args.kwargs.get("timeout"), metrics.TIMEOUT_S)

        self.assertTrue(req.full_url.endswith("/projetos/sinal"))
        self.assertTrue(req.full_url.startswith("https://example.test/"))
        self.assertEqual(req.get_method(), "POST")

        # secret header present with the env value (urllib capitalizes the key).
        self.assertEqual(req.get_header("X-projetos-secret"), "test-secret")

        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["projeto"], "moneyprinter")
        self.assertEqual(body["tipo"], "producao")
        self.assertEqual(body["metrica"], "video_gerado")
        self.assertEqual(body["alvo"], "biblioteca")
        self.assertEqual(body["ref"], "task-uuid:final-1.mp4")
        self.assertEqual(body["valor"], 1)
        self.assertIn("ocorrido_em", body)
        self.assertIn("idempotencia", body)
        self.assertTrue(body["idempotencia"].startswith("mp:video_gerado:task-uuid:final-1.mp4:"))
        # the secret never enters the body.
        self.assertNotIn("test-secret", req.data.decode("utf-8"))


class KillSwitchTest(_CaptureLogMixin, unittest.TestCase):
    @mock.patch.dict("os.environ",
                     {"METRICS_WEBHOOK_URL": "https://example.test",
                      "METRICS_WEBHOOK_SECRET": "x", "METRICS_WEBHOOK_OFF": "1"},
                     clear=True)
    @mock.patch("urllib.request.urlopen")
    def test_kill_switch_does_not_call(self, urlopen):
        metrics.report_video_generated("biblioteca", "task-uuid:final-1.mp4")
        self.assertEqual(urlopen.call_count, 0)
        # even disabled, leaves ONE trail line saying it is disabled.
        self.assertTrue(self._has_line("disabled"))


class DryRunTest(_CaptureLogMixin, unittest.TestCase):
    @mock.patch.dict("os.environ", ENV_CLEAN, clear=True)
    @mock.patch("urllib.request.urlopen")
    def test_dry_run_does_not_call(self, urlopen):
        metrics.report_video_generated("biblioteca", "task-uuid:final-1.mp4", dry_run=True)
        self.assertEqual(urlopen.call_count, 0)


class NoUrlTest(_CaptureLogMixin, unittest.TestCase):
    @mock.patch.dict("os.environ", {"METRICS_WEBHOOK_SECRET": "x"}, clear=True)  # no URL
    @mock.patch("urllib.request.urlopen")
    def test_no_url_is_a_noop_with_trail(self, urlopen):
        self.assertIsNone(metrics.report_video_generated("biblioteca", "task-uuid:final-1.mp4"))
        self.assertEqual(urlopen.call_count, 0)
        self.assertTrue(self._has_line("no METRICS_WEBHOOK_URL"))


class ConnectionFailureTest(_CaptureLogMixin, unittest.TestCase):
    @mock.patch.dict("os.environ", ENV_CLEAN, clear=True)
    @mock.patch("urllib.request.urlopen")
    def test_urlerror_does_not_raise_and_returns(self, urlopen):
        urlopen.side_effect = urllib.error.URLError("connection refused")
        # Must not raise: generating the video is what matters.
        self.assertIsNone(metrics.report_video_generated("biblioteca", "task-uuid:final-1.mp4"))
        self.assertEqual(urlopen.call_count, 1)

    @mock.patch.dict("os.environ", ENV_CLEAN, clear=True)
    @mock.patch("urllib.request.urlopen")
    def test_timeout_does_not_raise(self, urlopen):
        urlopen.side_effect = TimeoutError("timed out")
        self.assertIsNone(metrics.report_video_generated("biblioteca", "task-uuid:final-1.mp4"))


class NonOkResponseTest(_CaptureLogMixin, unittest.TestCase):
    @mock.patch.dict("os.environ", ENV_CLEAN, clear=True)
    @mock.patch("urllib.request.urlopen")
    def test_http_error_403_does_not_raise(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            "u", 403, "forbidden", hdrs=None, fp=None)
        self.assertIsNone(metrics.report_video_generated("biblioteca", "task-uuid:final-1.mp4"))

    @mock.patch.dict("os.environ", ENV_CLEAN, clear=True)
    @mock.patch("urllib.request.urlopen")
    def test_ok_false_does_not_raise(self, urlopen):
        urlopen.return_value = _FakeResponse(
            json.dumps({"ok": False, "erro": "duplicate idempotencia"}).encode("utf-8"))
        self.assertIsNone(metrics.report_video_generated("biblioteca", "task-uuid:final-1.mp4"))
        self.assertEqual(urlopen.call_count, 1)


class MissingSecretTest(_CaptureLogMixin, unittest.TestCase):
    @mock.patch.dict("os.environ",
                     {"METRICS_WEBHOOK_URL": "https://example.test"}, clear=True)  # no secret
    @mock.patch("urllib.request.urlopen")
    def test_no_secret_does_not_call_and_logs_warning(self, urlopen):
        self.assertIsNone(metrics.report_video_generated("biblioteca", "task-uuid:final-1.mp4"))
        self.assertEqual(urlopen.call_count, 0)
        # closed failure with a trail: ONE warning line saying the secret is missing.
        self.assertTrue(self._has_line("METRICS_WEBHOOK_SECRET missing"))


class VideoRefTest(unittest.TestCase):
    def test_path_becomes_task_id_plus_basename(self):
        # the id is regrouped into blocks of <=8: the hyphen disappears.
        self.assertEqual(
            metrics.video_ref("task-uuid", r"C:\repos\mpt\storage\tasks\task-uuid\final-1.mp4"),
            "taskuuid:final-1.mp4")
        self.assertEqual(
            metrics.video_ref("task-uuid", "/home/user/storage/tasks/task-uuid/final-2.mp4"),
            "taskuuid:final-2.mp4")

    def test_ref_never_carries_full_path(self):
        ref = metrics.video_ref("abc", r"C:\secret\person\final-1.mp4")
        self.assertNotIn("secret", ref)
        self.assertNotIn("\\", ref)
        self.assertNotIn("/", ref)

    def test_real_uuid_does_not_trip_document_filter(self):
        # A hyphenated uuid4 can hide an 11/14-digit run (the `-` does NOT break
        # a run in the receiver's document-id filter). Regrouping into blocks of
        # <=8 with `:` guarantees no run reaches 11/14. This replicates the
        # filter to prove the fix without depending on a server.
        import re
        only_digits = re.compile(r"\D+")

        def looks_like_document(text):
            for run in re.findall(r"\d[\d.\-/ ]{9,}\d", str(text)):
                if len(only_digits.sub("", run)) in (11, 14):
                    return True
            return False

        # uuid built to hold a 14-digit run crossing hyphens.
        raw = "abcdef12-3456-7890-1234-abcdef000000"
        self.assertTrue(looks_like_document(raw))  # raw trips
        ref = metrics.video_ref(raw, "storage/tasks/x/final-1.mp4")
        self.assertFalse(looks_like_document(ref))  # grouped, does not trip


if __name__ == "__main__":
    unittest.main()

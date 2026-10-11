# -*- coding: utf-8 -*-
"""Optional metrics webhook: report when a final video is generated.

WHY THIS EXISTS. When MoneyPrinterTurbo finishes GENERATING a final video, you
may want to count it somewhere (a dashboard, an analytics bus). This module is a
tiny client for that: it POSTs a `video_gerado` ("video generated") signal to a
webhook you configure. `cli.py` calls it at the point where the final .mp4 has
been written successfully. Per-video COST accounting is out of scope here.

OPT-IN. The webhook is OFF unless you set `METRICS_WEBHOOK_URL`. With no URL it
is a no-op (one log line). So this adds nothing to a default install.

PRINCIPLE. Generating the video is what matters; reporting is secondary. The
reporter NEVER raises and NEVER delays generation beyond a short timeout: an
endpoint that is down, a timeout, a missing secret, or a non-200 response each
become ONE log line and a silent return. Every path (success, no-op, dry run,
failure) leaves a log line on purpose — no silent drop.

LOG. Uses loguru's `logger`, like the rest of the project.

KILL SWITCH. `METRICS_WEBHOOK_OFF=1` disables the reporter (no-op with one log
line), independent of whether the URL is set.

SECRET. The value of `METRICS_WEBHOOK_SECRET` travels only in the
`X-Projetos-Secret` header, never in a log line. The request body is not logged
either; the opaque `ref` may be.

PRIVACY. The `ref` is opaque by design: the task id (a UUID) plus the basename
of the file (`final-1.mp4`), never the full path and never any personal data.
The basename alone would collide (it is `final-1.mp4` for every task), so the
task id carries the uniqueness.

THE WEBHOOK CONTRACT. A single POST to `<METRICS_WEBHOOK_URL>/projetos/sinal`:
  headers: Content-Type: application/json, X-Projetos-Secret: <secret>
  body (JSON): {
    "projeto": "moneyprinter", "tipo": "producao", "alvo": <target>,
    "metrica": "video_gerado", "valor": 1, "ref": <opaque ref>,
    "ocorrido_em": <ISO-8601 UTC>, "idempotencia": <dedupe key>
  }
The receiver is expected to answer 200 with a JSON body `{"ok": true, ...}` and
to deduplicate on `idempotencia`. Keep this shape stable: other producers share
the same endpoint.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

from loguru import logger

ROUTE = "/projetos/sinal"
TIMEOUT_S = 3.0

# cli.py always writes the final video to local storage; there is no notion of
# an output profile/destination on the CLI path. "biblioteca" is the default
# opaque target label (a contract value, kept stable for the receiver).
DEFAULT_TARGET = "biblioteca"


def _group_opaque(token: str) -> str:
    """Regroup an opaque token into blocks of <=8 chars joined by ':'.

    The receiver has a document-id filter that treats `-`, `.`, `/` and space as
    separators that do NOT break a run of digits, so a hyphenated UUID such as
    `...a716-446655440000` can hide an 11/14-digit run and trip that filter
    (~2.8% of uuid4). A `:` breaks the run; blocks of <=8 guarantee no run ever
    reaches 11/14 digits. The fix lives here, in the client, not in loosening
    the receiver's filter.
    """
    raw = (token or "").replace("-", "").replace(":", "")
    if not raw:
        return token or ""
    return ":".join(raw[i:i + 8] for i in range(0, len(raw), 8))


def video_ref(task_id: str, mp4_path: str) -> str:
    """Opaque technical reference of the video for the metrics trail.

    `task_id` is the task UUID; `mp4_path` is the path of the final .mp4. Returns
    `"<grouped task_id>:<basename>"`, never the full path and never personal
    data. The basename alone would collide (it is `final-1.mp4` for every task),
    so it travels together with the task id. The id is regrouped into blocks of
    <=8 by `_group_opaque` so it cannot trip the receiver's document-id filter.
    """
    basename = os.path.basename((mp4_path or "").replace("\\", "/"))
    tid = _group_opaque(task_id)
    if tid and basename:
        return "%s:%s" % (tid, basename)
    return tid or basename


def report_video_generated(target: str, ref: str, *, dry_run: bool = False) -> None:
    """Tell the metrics webhook that a final video was generated.

    `target` is the destination label (use `DEFAULT_TARGET`), `ref` the opaque
    reference of the video (use `video_ref`). NEVER raises and NEVER blocks
    generation beyond the short timeout. In `dry_run`, makes no call. Always
    returns None.
    """
    if dry_run:
        logger.info("metrics video_gerado: dry run, not reporting (target={} ref={})", target, ref)
        return

    if os.environ.get("METRICS_WEBHOOK_OFF") == "1":
        logger.info(
            "metrics video_gerado: disabled by METRICS_WEBHOOK_OFF (target={} ref={})",
            target, ref,
        )
        return

    base = os.environ.get("METRICS_WEBHOOK_URL")
    if not base:
        logger.info(
            "metrics video_gerado: no METRICS_WEBHOOK_URL, not reporting (target={} ref={})",
            target, ref,
        )
        return

    secret = os.environ.get("METRICS_WEBHOOK_SECRET")
    if not secret:
        logger.warning(
            "metrics video_gerado NOT sent: METRICS_WEBHOOK_SECRET missing "
            "(target={} ref={})",
            target, ref,
        )
        return

    url = base.rstrip("/") + ROUTE

    now = datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    body = {
        "projeto": "moneyprinter",
        "tipo": "producao",
        "alvo": target,
        "metrica": "video_gerado",
        "valor": 1,
        "ref": ref,
        "ocorrido_em": now.isoformat(),
        # The dedupe key identifies ONE signal: the metric is part of the key so
        # it does not collide with another signal for the same video in the same
        # second.
        "idempotencia": "mp:video_gerado:%s:%d" % (ref, epoch),
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Projetos-Secret": secret})

    try:
        # urlopen via attribute on purpose: the test suite mocks urllib.request.urlopen.
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            status = getattr(resp, "status", None)
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # 400/403/409: the receiver refused; log only the code, never body/headers.
        logger.warning(
            "metrics video_gerado refused: HTTP {} (target={} ref={})",
            getattr(e, "code", "?"), target, ref,
        )
        return
    except Exception as e:  # timeout, DNS, connection refused, OSError...
        logger.warning(
            "metrics video_gerado NOT sent: {} (target={} ref={})",
            type(e).__name__, target, ref,
        )
        return

    if status not in (200, None):
        logger.warning(
            "metrics video_gerado: receiver answered HTTP {} (target={} ref={})",
            status, target, ref,
        )
        return

    try:
        answer = json.loads(raw.decode("utf-8"))
    except Exception:
        logger.warning(
            "metrics video_gerado: unreadable response (target={} ref={})",
            target, ref,
        )
        return

    if answer.get("ok"):
        logger.info(
            "metrics video_gerado sent: target={} ref={} id={} new={}",
            target, ref, answer.get("id"), answer.get("novo"),
        )
    else:
        logger.warning(
            "metrics video_gerado: receiver returned ok=false erro={} (target={} ref={})",
            answer.get("erro"), target, ref,
        )

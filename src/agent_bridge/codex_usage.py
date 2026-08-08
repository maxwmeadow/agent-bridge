"""Optional, best-effort reader for Codex's local usage records.

**This is disabled by default and nothing calls it automatically.** It exists
because Codex has no official machine-readable usage feed: `/status` is
interactive, and `rate_limits` in `codex exec` output is still an open feature
request upstream. What Codex *does* write is a session rollout log:

    ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl

whose ``token_count`` events carry a ``rate_limits`` payload:

    {"limit_id": "codex", "primary": {"used_percent": 48.0,
     "window_minutes": 10080, "resets_at": 1786674570},
     "secondary": null, "plan_type": "plus", "rate_limit_reached_type": null}

Classification: **local but undocumented**. No network calls, no credentials,
no private endpoints -- only files Codex already wrote to this machine. But
the format is not published and can change without notice, so:

* nothing here runs unless you ask for it;
* every parse failure returns ``None`` and is logged, never raised;
* messaging and waiting do not depend on this module at all.

If it silently stops producing samples after a Codex upgrade, the bridge keeps
working exactly as it did before.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"

#: How many recent rollout files to look at. Newest first; the newest file
#: with a usable sample wins.
MAX_FILES_SCANNED = 20


@dataclass(frozen=True, slots=True)
class CodexUsage:
    """One usage sample lifted from a rollout file."""

    percent: float
    window: str  # "primary" or "secondary"
    resets_at: str | None
    plan_type: str | None
    limit_reached: bool
    source_file: Path


def _iso_from_epoch(value: object) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(
            timespec="microseconds"
        )
    except (OverflowError, OSError, ValueError):
        return None


def _sample_from_rate_limits(payload: object, path: Path) -> CodexUsage | None:
    """Pull the busiest window out of one ``rate_limits`` object."""
    if not isinstance(payload, dict):
        return None

    best: tuple[float, str, object] | None = None
    for window in ("primary", "secondary"):
        entry = payload.get(window)
        if not isinstance(entry, dict):
            continue
        percent = entry.get("used_percent")
        if not isinstance(percent, (int, float)):
            continue
        if not 0.0 <= float(percent) <= 100.0:
            continue
        if best is None or float(percent) > best[0]:
            best = (float(percent), window, entry.get("resets_at"))

    if best is None:
        return None

    percent, window, resets_at = best
    plan = payload.get("plan_type")
    reached = payload.get("rate_limit_reached_type")
    return CodexUsage(
        percent=percent,
        window=window,
        resets_at=_iso_from_epoch(resets_at),
        plan_type=plan if isinstance(plan, str) else None,
        # Present and non-null means Codex itself said a limit was hit. Absent
        # or null means "just consuming quota", which is not exhaustion.
        limit_reached=reached is not None,
        source_file=path,
    )


def _scan_file(path: Path) -> CodexUsage | None:
    """Return the last usable sample in one rollout file, or None."""
    latest: CodexUsage | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or "rate_limits" not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a partially written trailing line is normal
                if not isinstance(record, dict):
                    continue
                # The payload has moved around between Codex versions, so look
                # in the record itself and one level down.
                candidates = [record.get("rate_limits")]
                for key in ("payload", "info", "data"):
                    nested = record.get(key)
                    if isinstance(nested, dict):
                        candidates.append(nested.get("rate_limits"))
                for candidate in candidates:
                    sample = _sample_from_rate_limits(candidate, path)
                    if sample is not None:
                        latest = sample
    except OSError as exc:
        log.debug("could not read codex rollout %s: %s", path, exc)
        return None
    return latest


def read_latest_usage(sessions_dir: Path | None = None) -> CodexUsage | None:
    """Best-effort: the most recent Codex usage sample on this machine.

    Returns ``None`` for every failure mode -- directory missing, no files, no
    parsable records, format changed. Never raises.
    """
    root = sessions_dir if sessions_dir is not None else DEFAULT_SESSIONS_DIR
    try:
        if not root.is_dir():
            log.debug("codex sessions directory not found: %s", root)
            return None
        files = sorted(
            root.rglob("rollout-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:MAX_FILES_SCANNED]
    except OSError as exc:
        log.debug("could not list codex rollouts under %s: %s", root, exc)
        return None

    for path in files:
        sample = _scan_file(path)
        if sample is not None:
            log.info(
                "codex usage sample percent=%.1f window=%s file=%s",
                sample.percent,
                sample.window,
                path.name,
            )
            return sample

    log.debug("no codex usage samples found under %s", root)
    return None

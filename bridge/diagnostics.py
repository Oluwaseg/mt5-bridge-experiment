import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

_WRITE_LOCK = threading.Lock()
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|credential|password|secret)\b(\s*[:=]\s*)[^\s,;]+"
)
_LONG_HEX = re.compile(r"(?i)\b[0-9a-f]{24,}\b")
_LONG_DIGITS = re.compile(r"\b\d{5,}\b")
_UUID = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_NAMED_IDENTIFIER = re.compile(r"(?i)\b(account|user|worker)=(\S+)")
_IDENTIFIER_KEYS = ("account_id", "accountid", "connector_id", "connectorid", "user_id", "userid", "worker_id", "workerid")


def masked_identifier(value):
    text = str(value or "")
    if not text:
        return "none"
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:10]


def _redact_text(value):
    text = str(value)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    text = _NAMED_IDENTIFIER.sub(
        lambda match: f"{match.group(1)}=[id:{masked_identifier(match.group(2))}]",
        text,
    )
    text = _UUID.sub(lambda match: "[id:" + masked_identifier(match.group(0)) + "]", text)
    text = _LONG_HEX.sub(lambda match: "[hex:..." + match.group(0)[-4:] + "]", text)
    text = _LONG_DIGITS.sub(lambda match: "***" + match.group(0)[-4:], text)
    return text


def _safe_value(key, value):
    normalized_key = str(key).casefold()
    if any(marker in normalized_key for marker in ("token", "credential", "password", "secret")):
        return "[REDACTED]"
    if normalized_key in _IDENTIFIER_KEYS and value is not None:
        return f"[id:{masked_identifier(value)}]"
    if isinstance(value, dict):
        return {str(child_key): _safe_value(child_key, child) for child_key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(key, child) for child in value]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(value)


def diagnostic_path(pid=None):
    root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    process_id = os.getpid() if pid is None else int(pid)
    return root / "ProfitPilot" / "diagnostics" / f"connector-diagnostics-{process_id}.jsonl"


def log_diagnostic(stage, session_id=None, **fields):
    """Append one redacted JSON record to this process's local diagnostic log."""
    path = diagnostic_path()
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "pid": os.getpid(),
        "stage": _redact_text(stage),
        "session": masked_identifier(session_id),
        "fields": {key: _safe_value(key, value) for key, value in fields.items()},
    }
    serialized = (json.dumps(entry, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(descriptor, serialized)
            finally:
                os.close(descriptor)
        return path
    except OSError:
        return None

"""Run log and the diagnostics the 反馈 dialog prepares.

Song file names may appear in the log. The serial number, FireWire GUID and device name are
scrubbed everywhere, and the user's home folder is shown as ~.
"""

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import time
import traceback

from version import __version__

LOGS = Path(os.environ["LOCALAPPDATA"]) / "ClassicDrop" / "Logs"
LOG_FILE = "classicdrop.log"
LOG_TAIL = 60

log = logging.getLogger("ClassicDrop")
log.addHandler(logging.NullHandler())

_secrets = set()
_last_error = None
_log_file = None


def hide(*values):
    """Register strings that must never reach the log or the report (serial, GUID, device name)."""
    for value in values:
        value = str(value).strip()
        if len(value) < 3:
            continue
        _secrets.add(value)
        if re.fullmatch(r"(?:0x)?[0-9a-fA-F]{8,}", value):
            bare = value.removeprefix("0x").removeprefix("0X")
            _secrets.update((bare.lower(), bare.upper(), "0x" + bare.lower(), "0x" + bare.upper()))


def scrub(text):
    """Hide the home folder and every registered secret in text of any shape, tracebacks included."""
    home = str(Path.home())
    for candidate in {home, home.replace("\\", "/")}:
        text = re.sub(re.escape(candidate), "~", text, flags=re.I)
    for secret in sorted(_secrets, key=len, reverse=True):
        text = text.replace(secret, "[已隐藏]")
    return text


class _Scrubbed(logging.Formatter):
    def format(self, record):
        return scrub(super().format(record))


def setup(folder=LOGS):
    """Attach the rotating file handler to the ClassicDrop and iPodDB loggers. Safe to call twice."""
    global _log_file
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _log_file = folder / LOG_FILE
    for name, level in (("ClassicDrop", logging.INFO), ("iPodDB", logging.WARNING)):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        if not any(isinstance(h, RotatingFileHandler) and getattr(h, "_classicdrop", False)
                   for h in logger.handlers):
            handler = RotatingFileHandler(_log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
            handler._classicdrop = True
            handler.setFormatter(_Scrubbed("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            logger.addHandler(handler)


def reset():
    """Drop handlers, secrets and the remembered error. Only tests need a clean slate."""
    global _last_error, _log_file
    for name in ("ClassicDrop", "iPodDB"):
        logger = logging.getLogger(name)
        for handler in [h for h in logger.handlers if getattr(h, "_classicdrop", False)]:
            logger.removeHandler(handler)
            handler.close()
    _secrets.clear()
    _last_error = None
    _log_file = None


def remember(message, exc=None):
    """Keep the latest error for the feedback report and log it, traceback included."""
    global _last_error
    detail = str(message) or (type(exc).__name__ if exc else "")
    if exc is not None:
        detail += "\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
    _last_error = (time.strftime("%Y-%m-%d %H:%M:%S"), scrub(detail))
    log.error("%s", detail, exc_info=exc)


def report(device=""):
    """Environment, the last error and the tail of the log, ready for the feedback dialog. Scrubbed."""
    from updates import environment_lines
    lines = ["## 环境", *environment_lines(device), "", "## 最近一次报错", ""]
    if _last_error:
        stamp, detail = _last_error
        lines += [f"{stamp} · {detail}", ""]
    else:
        lines += ["本次运行没有报错", ""]
    lines += ["## 最近的日志", ""]
    tail = []
    file = _log_file or LOGS / LOG_FILE
    try:
        tail = file.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL:]
    except OSError:
        pass
    lines += tail or ["（还没有日志）"]
    return scrub("\n".join(lines))

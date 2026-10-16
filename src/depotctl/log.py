"""Tiny console helpers: coloured step/ok/warn/error lines."""

import os
import sys

# Never crash on a character the console/pipe encoding can't show (e.g. cp1252 on Windows).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
if _COLOR and os.name == "nt":
    os.system("")  # enables ANSI escape handling in classic Windows consoles


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def dim(text: str) -> str:
    return _c("2", text)


def bold(text: str) -> str:
    return _c("1", text)


def step(msg: str) -> None:
    print(_c("1;36", f"==> {msg}"), flush=True)


def info(msg: str) -> None:
    print(f"    {msg}", flush=True)


def ok(msg: str) -> None:
    print(_c("32", f"  ✓ {msg}"), flush=True)


def warn(msg: str) -> None:
    print(_c("33", f"  ! {msg}"), file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(_c("1;31", f"error: {msg}"), file=sys.stderr, flush=True)

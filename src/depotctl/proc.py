"""Subprocess runner with log files, live echo, dry-run and a failure tail."""

import collections
import os
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import log

DRY_RUN = False  # set from the CLI; mutating commands are printed, not run


class CommandError(RuntimeError):
    def __init__(self, message: str, tail: list[str] | None = None):
        super().__init__(message)
        self.tail = tail or []


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    tail: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def fmt(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd) if os.name == "nt" else shlex.join(cmd)


def run(
    cmd: list[str | Path],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    log_path: Path | None = None,
    echo: bool = False,
    capture: bool = False,
    split_stdout: bool = False,
    check: bool = True,
    mutating: bool = True,
    interactive: bool = False,
    quiet: bool = False,
    tail_lines: int = 60,
) -> Result:
    """Run a command.

    capture       collect output and return it in Result.stdout
    split_stdout  keep stdout separate (returned) while stderr is logged/echoed;
                  used for `unity build --format json`
    mutating      skipped (only printed) in --dry-run mode
    interactive   inherit the terminal (for password / Steam Guard prompts)
    """
    shown = [str(c) for c in cmd]
    args = list(shown)
    if exe := shutil.which(args[0]):  # resolves .cmd/.bat shims on Windows too
        args[0] = exe
    if mutating and DRY_RUN:
        log.info(log.dim(f"[dry-run] {fmt(shown)}"))
        return Result(0)
    if not quiet:
        log.info(log.dim(f"$ {fmt(shown)}"))

    full_env = {**os.environ, **env} if env else None
    try:
        if interactive:
            rc = subprocess.run(args, cwd=cwd, env=full_env).returncode
            if check and rc != 0:
                raise CommandError(f"{args[0]} exited with code {rc}")
            return Result(rc)

        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if split_stdout else subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError:
        raise CommandError(f"command not found: {args[0]}") from None

    stdout_chunks: list[str] = []
    reader: threading.Thread | None = None
    if split_stdout:
        assert proc.stdout is not None
        out_pipe = proc.stdout
        reader = threading.Thread(target=lambda: stdout_chunks.append(out_pipe.read()), daemon=True)
        reader.start()
        stream = proc.stderr
    else:
        stream = proc.stdout
    assert stream is not None

    tail: collections.deque[str] = collections.deque(maxlen=tail_lines)
    collected: list[str] = []
    logf = None
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = log_path.open("w", encoding="utf-8")
    try:
        for line in stream:
            if logf:
                logf.write(line)
            if echo:
                print(log.dim("    | ") + line.rstrip("\n"), flush=True)
            if capture and not split_stdout:
                collected.append(line)
            tail.append(line.rstrip("\n"))
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        raise
    finally:
        if logf:
            logf.close()
    if reader:
        reader.join()

    out = "".join(stdout_chunks) if split_stdout else "".join(collected)
    result = Result(rc, out, list(tail))
    if check and rc != 0:
        where = f" (log: {log_path})" if log_path else ""
        raise CommandError(f"{Path(args[0]).name} exited with code {rc}{where}", list(tail))
    return result

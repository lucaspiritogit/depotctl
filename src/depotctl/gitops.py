"""Pull the latest changes for a project and describe the commit being built."""

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import log, proc
from .config import Project
from .proc import CommandError, run


@dataclass(slots=True)
class Revision:
    sha: str
    short: str
    branch: str
    subject: str
    dirty: bool

    def as_dict(self) -> dict:
        return {"sha": self.sha, "short": self.short, "branch": self.branch,
                "subject": self.subject, "dirty": self.dirty}


# Never let git wait for a username/password on the terminal (would hang under cron).
NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0"}


def _git(path: Path, *args: str, mutating: bool = False, check: bool = True) -> str:
    return run(["git", "-C", path, *args], capture=True, mutating=mutating, env=NO_PROMPT,
               check=check, quiet=not mutating).stdout.strip()


def is_dirty(path: Path) -> bool:
    return bool(_git(path, "status", "--porcelain", "--untracked-files=no"))


def uses_lfs(path: Path) -> bool:
    attrs = path / ".gitattributes"
    return attrs.is_file() and "filter=lfs" in attrs.read_text(encoding="utf-8", errors="ignore")


def clone(project: Project) -> None:
    """Clone a project that isn't on this machine yet."""
    if not project.repo:
        raise CommandError(
            f"{project.path} doesn't exist and [projects.{project.key}] has no `repo` to clone from"
        )
    if not proc.DRY_RUN:
        project.path.parent.mkdir(parents=True, exist_ok=True)
    cmd: list[str | Path] = ["git", "clone"]
    if project.git_branch:
        cmd += ["--branch", project.git_branch]
    r = run([*cmd, project.repo, project.path], echo=True, env=NO_PROMPT, check=False)
    if not r.ok:
        raise CommandError(
            f"git clone failed. On a server, use an SSH URL with a deploy key (override `repo` "
            f"in depotctl.local.toml) or a credential helper for HTTPS.", r.tail)
    if proc.DRY_RUN:
        return
    if uses_lfs(project.path):
        if shutil.which("git-lfs"):
            _git(project.path, "lfs", "pull", mutating=True)
        else:
            log.warn("repo uses Git LFS but git-lfs is not installed; assets will be pointer files")
    log.ok(f"cloned {project.repo} -> {project.path}")


def remote_url(path: Path) -> str | None:
    r = run(["git", "-C", path, "remote", "get-url", "origin"], capture=True,
            check=False, mutating=False, quiet=True)
    return r.stdout.strip() or None if r.ok else None


def describe(path: Path) -> Revision:
    return Revision(
        sha=_git(path, "rev-parse", "HEAD"),
        short=_git(path, "rev-parse", "--short=8", "HEAD"),
        branch=_git(path, "rev-parse", "--abbrev-ref", "HEAD"),
        subject=_git(path, "log", "-1", "--format=%s"),
        dirty=is_dirty(path),
    )


def sync(project: Project, *, unity_cli: str, allow_dirty: bool) -> Revision:
    path = project.path
    if not (path / ".git").exists():
        raise CommandError(f"{path} is not a git repository")

    if is_dirty(path) and not allow_dirty:
        changed = _git(path, "status", "--short", "--untracked-files=no").splitlines()
        raise CommandError(
            "working tree has uncommitted changes; commit/stash them or pass --allow-dirty",
            changed[:20],
        )

    if project.vcs == "unity":
        run([unity_cli, "vcs", "sync"], cwd=path)
    else:
        current = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        _git(path, "fetch", "--prune", mutating=True)
        if project.git_branch and current != project.git_branch:
            log.info(f"switching {current} -> {project.git_branch}")
            _git(path, "checkout", project.git_branch, mutating=True)
        elif current == "HEAD":
            raise CommandError("repository is in detached HEAD state; set git_branch or check out a branch")
        _git(path, "pull", "--ff-only", mutating=True)
        if uses_lfs(path):
            if shutil.which("git-lfs"):
                _git(path, "lfs", "pull", mutating=True)
            else:
                log.warn("repo uses Git LFS but git-lfs is not installed")

    rev = describe(path)
    log.ok(f"{rev.branch} @ {rev.short}  {log.dim(rev.subject)}")
    return rev

"""The deploy pipeline: pull -> editor check -> build per target -> Steam upload."""

import getpass
import json
import shutil
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import gitops, log, proc, steam, unity
from .config import Config, ConfigError, Project, Target, validate_for_upload
from .proc import CommandError

MANIFEST = "manifest.json"


@dataclass(slots=True, kw_only=True)
class Options:
    targets: list[str] | None = None
    pull: bool = True
    allow_dirty: bool = False
    install: bool = True
    build: bool = True
    upload: bool = True
    build_ref: str = "latest"          # upload-only: which build to upload
    set_live: str | None = None        # None = use project default; "" = don't set live
    allow_default_branch: bool = False
    preview: bool = False
    verbose: bool = False


# --------------------------------------------------------------------------- builds on disk

def project_builds_dir(cfg: Config, project: Project) -> Path:
    return cfg.builds_dir / project.key


def read_manifest(build_dir: Path) -> dict:
    return json.loads((build_dir / MANIFEST).read_text(encoding="utf-8"))


def write_manifest(build_dir: Path, data: dict) -> None:
    (build_dir / MANIFEST).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def list_builds(cfg: Config, project: Project) -> list[tuple[Path, dict]]:
    root = project_builds_dir(cfg, project)
    if not root.is_dir():
        return []
    builds = []
    for d in sorted(root.iterdir(), reverse=True):
        if (d / MANIFEST).is_file():
            m = read_manifest(d)
            if m.get("status") == "built":
                builds.append((d, m))
    return builds


def resolve_build(cfg: Config, project: Project, ref: str) -> tuple[Path, dict]:
    builds = list_builds(cfg, project)
    if not builds:
        raise CommandError(f"no finished builds for {project.key}; run `depotctl build {project.key}`")
    if ref == "latest":
        return builds[0]
    for d, m in builds:
        if d.name == ref or m["revision"]["sha"].startswith(ref):
            return d, m
    raise CommandError(f"no build matching '{ref}'; see `depotctl builds {project.key}`")


def prune(cfg: Config, project: Project) -> None:
    keep = cfg.local.keep_builds
    if keep <= 0:
        return
    root = project_builds_dir(cfg, project)
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True) if root.is_dir() else []
    for old in dirs[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        log.info(log.dim(f"pruned old build {old.name}"))


# --------------------------------------------------------------------------- helpers

class _Timer:
    def __init__(self, label: str):
        self.label = label

    def __enter__(self):
        log.step(self.label)
        self.t0 = time.monotonic()
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            log.info(log.dim(f"{self.label}: {time.monotonic() - self.t0:.0f}s"))


def _resolve_set_live(project: Project, opts: Options) -> str | None:
    branch = project.set_live if opts.set_live is None else opts.set_live
    branch = (branch or "").strip()
    if branch.lower() == "default" and not opts.allow_default_branch:
        raise ConfigError(
            "refusing to set the build live on the public 'default' branch; "
            "pass --allow-default-branch if you really mean to release it"
        )
    return branch or None


def _desc(project: Project, rev: dict) -> str:
    dirty = " +dirty" if rev.get("dirty") else ""
    return (f"{project.key} {rev['short']} ({rev['branch']}){dirty} "
            f"by {getpass.getuser()}@{socket.gethostname()}")


def _preflight(cfg: Config, project: Project, targets: list[Target], opts: Options) -> None:
    problems: list[str] = []
    if opts.build:
        if not project.path.is_dir() and not project.repo:
            problems.append(f"project folder not found: {project.path} (set [projects.{project.key}] "
                            "path in depotctl.local.toml, or `repo` so depotctl can clone it)")
        if cfg.local.builder == "unity-cli" and not shutil.which(cfg.local.unity_cli):
            problems.append(f"Unity CLI '{cfg.local.unity_cli}' not found on PATH "
                            "(install: https://docs.unity.com/en-us/unity-cli/use-unity-cli)")
        if opts.pull and not shutil.which("git"):
            problems.append("git not found on PATH")
    if opts.upload:
        problems += validate_for_upload(cfg, project, targets)
        try:
            problems += steam.preflight(cfg, steam.pick_uploader(cfg, targets))
        except CommandError as e:
            problems.append(str(e))
    if problems:
        raise ConfigError("preflight failed:\n  - " + "\n  - ".join(problems))
    log.ok("config and tools look good")


# --------------------------------------------------------------------------- pipeline

def run_pipeline(cfg: Config, project: Project, opts: Options) -> None:
    targets = project.target_list(opts.targets)
    set_live = _resolve_set_live(project, opts) if opts.upload else None
    started = time.monotonic()
    log.step(f"{project.name}  [{', '.join(t.key for t in targets)}]"
             + (log.dim("  (dry run)") if proc.DRY_RUN else ""))

    with _Timer("Preflight"):
        _preflight(cfg, project, targets, opts)

    if opts.build and not project.path.is_dir():
        with _Timer("Clone"):
            gitops.clone(project)
        if proc.DRY_RUN:
            log.info("dry run stops here: the remaining steps need the cloned project")
            return

    if opts.build:
        build_dir, manifest = _build(cfg, project, targets, opts)
    else:
        build_dir, manifest = resolve_build(cfg, project, opts.build_ref)
        missing = [t.key for t in targets if t.key not in manifest["targets"]]
        if missing:
            raise CommandError(f"build {build_dir.name} has no {missing} output")
        log.info(f"using build {build_dir.name} ({manifest['revision']['short']})")

    build_id = None
    if opts.upload:
        with _Timer("Upload to Steam"):
            build_id = steam.upload(
                cfg, project, build_dir, targets,
                desc=_desc(project, manifest["revision"]),
                set_live=set_live, preview=opts.preview, verbose=opts.verbose,
            )
        if build_id:
            manifest.setdefault("uploads", []).append({
                "build_id": build_id,
                "set_live": set_live,
                "preview": opts.preview,
                "targets": [t.key for t in targets],
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            write_manifest(build_dir, manifest)

    elapsed = time.monotonic() - started
    log.step(f"Done in {elapsed / 60:.1f} min")
    log.info(f"build folder: {build_dir}")
    if build_id:
        live = f"live on '{set_live}'" if set_live and not opts.preview else "not set live"
        kind = "Preview (nothing uploaded)" if opts.preview else f"Steam BuildID {build_id}"
        log.ok(f"{kind}, {live}")
        log.info(steam.BUILDS_URL.format(app_id=project.steam_app_id))


def _build(cfg: Config, project: Project, targets: list[Target], opts: Options) -> tuple[Path, dict]:
    with _Timer("Pull latest changes" if opts.pull else "Read revision"):
        if opts.pull:
            rev = gitops.sync(project, unity_cli=cfg.local.unity_cli, allow_dirty=opts.allow_dirty)
        else:
            rev = gitops.describe(project.path)
            if rev.dirty and not opts.allow_dirty:
                raise CommandError("working tree has uncommitted changes; pass --allow-dirty")
            log.ok(f"{rev.branch} @ {rev.short} (not pulled)")

    with _Timer("Unity editor"):
        ev = unity.editor_version(project)
        if cfg.local.builder == "unity-cli" and cfg.local.ensure_editor and opts.install:
            unity.ensure_editor(cfg, ev, targets)
        else:
            log.ok(f"Unity {ev.version} ({cfg.local.builder} builder)")

    if proc.DRY_RUN:  # scratch folder that never shows up as a real build
        build_dir = cfg.builds_dir / ".dry-run" / project.key
        shutil.rmtree(build_dir, ignore_errors=True)
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        build_dir = project_builds_dir(cfg, project) / f"{stamp}-{rev.short}"
    build_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "project": project.key,
        "status": "building",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "revision": rev.as_dict(),
        "unity": ev.version,
        "builder": cfg.local.builder,
        "host": socket.gethostname(),
        "targets": {},
    }
    write_manifest(build_dir, manifest)

    for t in targets:
        with _Timer(f"Build {t.key}"):
            log_path = build_dir / "logs" / f"unity-{t.key}.log"
            if not opts.verbose:
                log.info(log.dim(f"editor log: {log_path}  (-v to stream it)"))
            artifact = unity.build(cfg, project, ev, t, build_dir / t.key, log_path,
                                   verbose=opts.verbose)
            manifest["targets"][t.key] = artifact.relative_to(build_dir).as_posix() \
                if artifact.is_relative_to(build_dir) else str(artifact)
            write_manifest(build_dir, manifest)
            log.ok(f"{t.key}: {artifact.name}")

    manifest["status"] = "dry-run" if proc.DRY_RUN else "built"
    write_manifest(build_dir, manifest)
    if not proc.DRY_RUN:
        prune(cfg, project)
    return build_dir, manifest

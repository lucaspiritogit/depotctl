"""Unity side: editor version, editor/module install, player builds."""

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import log, proc
from .config import HOST, Config, Project, Target
from .proc import CommandError, run

FALLBACK_METHOD = "Depotctl.DepotctlBuild.Build"
FALLBACK_SCRIPT = "DepotctlBuild.cs"


@dataclass(slots=True)
class EditorVersion:
    version: str
    changeset: str | None


def editor_version(project: Project) -> EditorVersion:
    pv = project.path / "ProjectSettings" / "ProjectVersion.txt"
    if not pv.is_file():
        raise CommandError(f"{pv} not found; is {project.path} a Unity project?")
    text = pv.read_text(encoding="utf-8")
    version = re.search(r"^m_EditorVersion:\s*(\S+)", text, re.M)
    rev = re.search(r"^m_EditorVersionWithRevision:\s*\S+\s*\((\w+)\)", text, re.M)
    if not version:
        raise CommandError(f"could not read m_EditorVersion from {pv}")
    return EditorVersion(version.group(1), rev.group(1) if rev else None)


def modules_needed(targets: list[Target]) -> list[str]:
    """Modules for targets this host's editor can't build out of the box."""
    return [t.module for t in targets if t.native_host != HOST]


def _unity_json(cfg: Config, *args: str) -> str:
    return run([cfg.local.unity_cli, *args, "--format", "json"], capture=True,
               split_stdout=True, mutating=False, check=False, quiet=True).stdout


def ensure_editor(cfg: Config, ev: EditorVersion, targets: list[Target]) -> None:
    unity = cfg.local.unity_cli
    modules = modules_needed(targets)
    installed = _unity_json(cfg, "editors", "--installed", "--verbose")
    if not installed.strip():
        log.warn("couldn't list installed editors via the Unity CLI; skipping the install check")
        return
    if ev.version not in installed:
        log.info(f"installing Unity {ev.version} with modules {modules or '(none)'}")
        cmd = [unity, "install", ev.version]
        if ev.changeset:
            cmd += ["-c", ev.changeset]
        if modules:
            cmd += ["-m", *modules]
        run(cmd)
        return
    missing = [m for m in modules if m not in installed]
    if missing:
        log.info(f"adding modules {missing} to Unity {ev.version}")
        r = run([unity, "install-modules", "-e", ev.version, "-m", *missing], check=False)
        if not r.ok:
            log.warn("install-modules failed; the build will fail if the module is really missing")
    else:
        log.ok(f"Unity {ev.version} with {modules or 'built-in modules'}")


# --------------------------------------------------------------------------- editor path

_DEFAULT_EDITOR = {
    "Windows": "C:/Program Files/Unity/Hub/Editor/{version}/Editor/Unity.exe",
    "Darwin": "/Applications/Unity/Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity",
    "Linux": "~/Unity/Hub/Editor/{version}/Editor/Unity",
}
_SUFFIXES = ["", "Editor/Unity.exe", "Unity.exe", "Unity.app/Contents/MacOS/Unity",
             "Contents/MacOS/Unity", "Editor/Unity"]


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def editor_path(cfg: Config, version: str) -> Path:
    if cfg.local.unity_editor:
        p = Path(cfg.local.unity_editor.format(version=version)).expanduser()
        if p.is_file():
            return p
        raise CommandError(f"unity_editor path does not exist: {p}")
    if shutil.which(cfg.local.unity_cli):
        try:
            data = json.loads(_unity_json(cfg, "editors", "--installed") or "null")
        except json.JSONDecodeError:
            data = None
        for s in _strings(data):
            if version in s:
                for suffix in _SUFFIXES:
                    p = Path(s) / suffix if suffix else Path(s)
                    if p.is_file():
                        return p
    p = Path(_DEFAULT_EDITOR.get(HOST, _DEFAULT_EDITOR["Linux"]).format(version=version)).expanduser()
    if p.is_file():
        return p
    raise CommandError(f"Unity {version} editor not found (set unity_editor in depotctl.local.toml)")


def has_fallback_script(project: Project) -> bool:
    return any((project.path / "Assets").rglob(FALLBACK_SCRIPT))


# --------------------------------------------------------------------------- build

def build(
    cfg: Config,
    project: Project,
    ev: EditorVersion,
    target: Target,
    target_dir: Path,
    log_path: Path,
    *,
    verbose: bool,
) -> Path:
    """Build one target into target_dir; returns the artifact path (.exe / .app)."""
    artifact = target_dir / target.artifact_name(project.product_name)
    target_dir.mkdir(parents=True, exist_ok=True)

    if cfg.local.builder == "unity-cli":
        cmd = [*cfg.local.unity_prefix, cfg.local.unity_cli, "build",
               "--project-path", project.path,
               "--target", target.unity_target,
               "--output-path", artifact,
               "--format", "json",
               *project.unity_build_args]
        r = run(cmd, split_stdout=True, log_path=log_path, echo=verbose, check=False)
        result = _parse_json(r.stdout)
        if not r.ok or result.get("success") is False:
            errs = "; ".join(e.get("message", str(e)) for e in result.get("errors", []) if isinstance(e, dict))
            raise CommandError(
                f"unity build ({target.key}) failed with code {r.returncode}"
                + (f": {errs}" if errs else "") + f" (log: {log_path})",
                r.tail,
            )
        reported = (result.get("data") or {}).get("outputPath") if isinstance(result.get("data"), dict) else None
        if reported and Path(reported).exists() and not artifact.exists():
            artifact = Path(reported)
    else:
        if not has_fallback_script(project):
            raise CommandError(
                f"{FALLBACK_SCRIPT} is not in {project.name}'s Assets; "
                f"run `depotctl install-build-script {project.key}` first"
            )
        editor = editor_path(cfg, ev.version)
        cmd = [*cfg.local.unity_prefix, editor, "-batchmode", "-nographics", "-quit",
               "-projectPath", project.path,
               "-buildTarget", target.editor_target,
               "-executeMethod", FALLBACK_METHOD,
               "-depotctlOutput", artifact,
               "-depotctlMacArch", project.mac_arch,
               "-logFile", "-"]
        run(cmd, log_path=log_path, echo=verbose)

    if not proc.DRY_RUN and not artifact.exists():
        raise CommandError(f"build finished but {artifact} is missing (log: {log_path})")
    return artifact


def _parse_json(text: str) -> dict:
    text = text.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # tolerate stray lines before the JSON document
        start = text.find("{")
        try:
            data = json.loads(text[start:]) if start >= 0 else {}
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}

"""depotctl command line: pick a Unity project, pull, build per OS, upload to Steam."""

import argparse
import re
import shutil
import sys
from pathlib import Path

from . import __version__, gitops, log, pipeline, proc, resources, steam, unity
from .config import HOST, LOCAL_FILE, SHARED_FILE, Config, ConfigError, Project, load, validate_for_upload
from .proc import CommandError, run


# --------------------------------------------------------------------------- project selection

def _pick(cfg: Config, key: str | None) -> Project:
    if key:
        return cfg.project(key)
    projects = list(cfg.projects.values())
    if not projects:
        raise ConfigError("no projects configured in depotctl.toml (see `depotctl list --discover`)")
    if len(projects) == 1:
        return projects[0]
    if not sys.stdin.isatty():
        raise ConfigError("which project? pass its key, e.g. `depotctl deploy my-game`")
    for i, p in enumerate(projects, 1):
        print(f"  {i}) {log.bold(p.key):<20} {p.name}")
    while True:
        answer = input("Project number: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(projects):
            return projects[int(answer) - 1]
        if answer in cfg.projects:
            return cfg.projects[answer]


def _targets(arg: str | None) -> list[str] | None:
    return [t.strip() for t in arg.split(",") if t.strip()] if arg else None


# --------------------------------------------------------------------------- commands

def cmd_list(cfg: Config, args) -> None:
    log.step(f"Projects  {log.dim(str(cfg.workspace))}")
    configured = set()
    for p in cfg.projects.values():
        configured.add(p.path.resolve())
        builds = pipeline.list_builds(cfg, p)
        last = f"last build {builds[0][0].name}" if builds else "no builds yet"
        try:
            version = unity.editor_version(p).version if p.path.is_dir() else \
                ("not cloned" if p.repo else "folder missing")
        except CommandError:
            version = "not a Unity project?"
        print(f"  {log.bold(p.key):<20} {p.name}")
        print(log.dim(f"      {p.path}  ·  Unity {version}  ·  app {p.steam_app_id or '?'}  ·  {last}"))

    others = [d for d in discover_unity_projects(cfg.workspace) if d.resolve() not in configured]
    if not others:
        return
    if not args.discover:
        log.info(log.dim(f"{len(others)} more Unity project(s) in the workspace; "
                         "`depotctl list --discover` prints config for them"))
        return
    log.step("Unconfigured Unity projects (paste into depotctl.toml)")
    for d in others:
        print(_snippet(cfg.workspace, d))


def discover_unity_projects(folder: Path) -> list[Path]:
    """Unity projects directly inside `folder` (or `folder` itself)."""
    if not folder.is_dir():
        return []
    candidates = [folder, *sorted(d for d in folder.iterdir() if d.is_dir())]
    return [d for d in candidates if (d / "ProjectSettings" / "ProjectVersion.txt").is_file()]


def _snippet(workspace: Path, folder: Path) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", folder.name.lower()).strip("-")
    product = folder.name
    settings = folder / "ProjectSettings" / "ProjectSettings.asset"
    if settings.is_file():
        m = re.search(r"^\s*productName:\s*(.+)$", settings.read_text(encoding="utf-8", errors="ignore"), re.M)
        if m:
            product = m.group(1).strip()
    appid_file = folder / "steam_appid.txt"
    appid = appid_file.read_text().strip() if appid_file.is_file() else "0"
    try:
        rel = folder.relative_to(workspace).as_posix() or "."
    except ValueError:
        rel = folder.as_posix()
    repo = gitops.remote_url(folder) if (folder / ".git").exists() else None
    repo_line = f'repo = "{repo}"\n' if repo else ""
    return (f'\n[projects.{key}]\nname = "{product}"\npath = "{rel}"\n{repo_line}'
            f'product_name = "{product}"\nsteam_app_id = {appid if appid.isdigit() else 0}\n\n'
            f'[projects.{key}.depots]\nwindows = 0\nmacos = 0\n')


def cmd_builds(cfg: Config, args) -> None:
    project = _pick(cfg, args.project)
    builds = pipeline.list_builds(cfg, project)
    log.step(f"Builds for {project.name}  {log.dim(str(pipeline.project_builds_dir(cfg, project)))}")
    if not builds:
        log.info("none yet")
    for d, m in builds:
        rev = m["revision"]
        uploads = m.get("uploads", [])
        up = (f"uploaded as BuildID {uploads[-1]['build_id']}"
              + (f" -> {uploads[-1]['set_live']}" if uploads[-1].get("set_live") else "")) if uploads else "not uploaded"
        print(f"  {log.bold(d.name)}  {', '.join(m['targets'])}  ·  {up}")
        print(log.dim(f"      {rev['branch']} {rev['short']}  {rev['subject']}"))


def _options(args, *, build: bool, upload: bool) -> pipeline.Options:
    return pipeline.Options(
        targets=_targets(getattr(args, "targets", None)),
        pull=not getattr(args, "skip_pull", False),
        allow_dirty=getattr(args, "allow_dirty", False),
        install=not getattr(args, "no_install", False),
        build=build,
        upload=upload,
        build_ref=getattr(args, "build", "latest"),
        set_live=("" if getattr(args, "no_set_live", False) else getattr(args, "set_live", None)),
        allow_default_branch=getattr(args, "allow_default_branch", False),
        preview=getattr(args, "preview", False),
        verbose=args.verbose,
    )


def cmd_deploy(cfg: Config, args) -> None:
    pipeline.run_pipeline(cfg, _pick(cfg, args.project), _options(args, build=True, upload=True))


def cmd_build(cfg: Config, args) -> None:
    pipeline.run_pipeline(cfg, _pick(cfg, args.project), _options(args, build=True, upload=False))


def cmd_upload(cfg: Config, args) -> None:
    pipeline.run_pipeline(cfg, _pick(cfg, args.project), _options(args, build=False, upload=True))


PROJECTS_MARKER = "# ---- projects"


def cmd_init(args) -> None:
    folder = Path(args.dir).expanduser().resolve()
    shared, local = folder / SHARED_FILE, folder / LOCAL_FILE
    if shared.exists() and not args.force:
        raise ConfigError(f"{shared} already exists (use --force to overwrite)")
    folder.mkdir(parents=True, exist_ok=True)

    template = resources.path("templates/depotctl.toml").read_text(encoding="utf-8")
    head, marker, example = template.partition(PROJECTS_MARKER)
    found = discover_unity_projects(folder)
    if found:
        body = head + marker + "\n" + "".join(_snippet(folder, d) for d in found)
    else:
        body = template
    shared.write_text(body, encoding="utf-8")
    log.ok(f"wrote {shared}" + (f" with {len(found)} discovered project(s)" if found else
                                " with an example project"))
    if not local.exists():
        local.write_text(resources.path("templates/depotctl.local.toml").read_text(encoding="utf-8"),
                         encoding="utf-8")
        log.ok(f"wrote {local}")
    log.info("next: fill in the Steam depot ids and steam_user, then run `depotctl doctor`")
    log.info(f"keep {LOCAL_FILE} out of version control (it is per machine)")


def cmd_clone(cfg: Config, args) -> None:
    projects = list(cfg.projects.values()) if args.all else [_pick(cfg, args.project)]
    for p in projects:
        if p.path.is_dir():
            log.ok(f"{p.key}: already at {p.path}")
            continue
        log.step(f"Clone {p.key}")
        gitops.clone(p)


def cmd_steam_login(cfg: Config, args) -> None:
    steam.login(cfg)
    log.ok("Steam login cached")


def cmd_docker_build(cfg: Config, args) -> None:
    steam.ensure_image(cfg, rebuild=True)
    log.ok(f"built {cfg.local.docker_image}")


def cmd_install_build_script(cfg: Config, args) -> None:
    project = _pick(cfg, args.project)
    src = resources.path("unity") / "Editor" / unity.FALLBACK_SCRIPT
    dst = project.path / "Assets" / "Editor" / "Depotctl" / unity.FALLBACK_SCRIPT
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    log.ok(f"copied to {dst}")
    log.info("commit it to the game repo so teammates get it too")


def cmd_doctor(cfg: Config, args) -> None:
    def check(ok: bool, msg: str, fix: str = "") -> None:
        if ok:
            log.ok(msg)
        else:
            log.warn(msg + (f"  → {fix}" if fix else ""))

    log.step("Config")
    log.ok(f"{cfg.root / SHARED_FILE}")
    log.info(f"workspace {cfg.workspace}  ·  builds {cfg.builds_dir}")

    log.step("Machine")
    check(sys.version_info >= (3, 12), f"Python {sys.version.split()[0]}", "Python 3.12+ required")
    check(bool(shutil.which("git")), "git", "install Git")
    if cfg.local.builder == "unity-cli":
        r = run([cfg.local.unity_cli, "--version"], capture=True, check=False, mutating=False, quiet=True) \
            if shutil.which(cfg.local.unity_cli) else None
        check(bool(r and r.ok), f"Unity CLI {r.stdout.strip() if r and r.ok else ''}".strip(),
              "https://docs.unity.com/en-us/unity-cli/use-unity-cli")
    else:
        log.ok("builder = editor (Unity CLI not required)")

    log.step("Steam uploader")
    mode = cfg.local.uploader if cfg.local.uploader != "auto" else ("native" if HOST == "Darwin" else "docker")
    log.info(f"mode: {mode}  ·  steam_user: {cfg.local.steam_user or '(not set)'}")
    check(bool(cfg.local.steam_user), "steam_user set", f"add steam_user to {LOCAL_FILE}")
    problems = steam.preflight(cfg, mode)
    for p in problems:
        log.warn(p)
    if mode == "docker" and not problems:
        check(steam.image_exists(cfg), f"image {cfg.local.docker_image}",
              "`depotctl docker-build` (or it builds on first upload)")
    elif mode == "native" and not problems:
        log.ok(f"steamcmd at {cfg.local.steamcmd}")

    for p in cfg.projects.values():
        log.step(f"Project {p.key}")
        if not p.path.is_dir():
            log.warn(f"not cloned yet: {p.path}"
                     + (f"  → depotctl clone {p.key}" if p.repo else "  → set `repo` or `path`"))
            continue
        try:
            ev = unity.editor_version(p)
            log.ok(f"Unity {ev.version} at {p.path}")
        except CommandError as e:
            log.warn(str(e))
        for problem in validate_for_upload(cfg, p, p.target_list()):
            if "steam_user" not in problem:
                log.warn(problem)
        if cfg.local.builder == "editor":
            check(unity.has_fallback_script(p), "fallback build script installed",
                  f"depotctl install-build-script {p.key}")


# --------------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="stream Unity/steamcmd output")
    common.add_argument("-n", "--dry-run", action="store_true",
                        help="print what would run without pulling, building or uploading")

    parser = argparse.ArgumentParser(
        prog="depotctl",
        description="Pull, build (Windows + macOS) and upload Unity games to Steam.")
    parser.add_argument("--version", action="version", version=f"depotctl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def add(name, func, help_, project=True):
        p = sub.add_parser(name, parents=[common], help=help_, description=help_)
        if project:
            p.add_argument("project", nargs="?", help="project key from depotctl.toml (prompted if omitted)")
        p.set_defaults(func=func)
        return p

    def build_flags(p):
        p.add_argument("-t", "--targets", help="comma separated: windows,macos (default: project targets)")
        p.add_argument("--skip-pull", action="store_true", help="build the current checkout as-is")
        p.add_argument("--allow-dirty", action="store_true", help="allow uncommitted changes")
        p.add_argument("--no-install", action="store_true", help="don't install the editor/modules")

    def upload_flags(p):
        p.add_argument("--set-live", metavar="BRANCH", help="Steam beta branch to set live (overrides config)")
        p.add_argument("--no-set-live", action="store_true", help="upload without setting any branch live")
        p.add_argument("--allow-default-branch", action="store_true",
                       help="permit --set-live default (public release)")
        p.add_argument("--preview", action="store_true", help="steamcmd preview: validate, upload nothing")

    p = add("deploy", cmd_deploy, "pull, build every target and upload to Steam")
    build_flags(p)
    upload_flags(p)

    p = add("build", cmd_build, "pull and build without uploading")
    build_flags(p)

    p = add("upload", cmd_upload, "upload an existing build")
    p.add_argument("-t", "--targets", help="subset of the build's targets to upload")
    p.add_argument("-b", "--build", default="latest", help="build folder name or commit sha (default: latest)")
    upload_flags(p)

    p = add("list", cmd_list, "show configured projects", project=False)
    p.add_argument("--discover", action="store_true", help="print config for other Unity projects found")
    add("builds", cmd_builds, "list finished builds of a project")
    p = add("clone", cmd_clone, "clone a project's repo into the workspace")
    p.add_argument("--all", action="store_true", help="clone every configured project that is missing")
    add("doctor", cmd_doctor, "check tools, login and project config", project=False)
    p = add("init", cmd_init, "create depotctl.toml here, listing the Unity projects found", project=False)
    p.add_argument("dir", nargs="?", default=".", help="folder that holds your game projects")
    p.add_argument("--force", action="store_true", help="overwrite an existing depotctl.toml")
    add("steam-login", cmd_steam_login, "log in to steamcmd once (password + Steam Guard)", project=False)
    add("docker-build", cmd_docker_build, "(re)build the steamcmd uploader image", project=False)
    add("install-build-script", cmd_install_build_script,
        "copy the fallback C# build script into a project (for builder = \"editor\")")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    proc.DRY_RUN = args.dry_run
    try:
        if args.func is cmd_init:
            cmd_init(args)
        else:
            args.func(load(), args)
        return 0
    except (ConfigError, CommandError) as e:
        log.error(str(e))
        tail = getattr(e, "tail", None)
        if tail:
            print(log.dim("---- last output ----"), file=sys.stderr)
            for line in tail[-40:]:
                print(log.dim("  " + line), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130

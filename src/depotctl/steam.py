"""Steam side: app_build VDF generation and uploading through steamcmd (Docker or native)."""

import re
import shutil
import subprocess
from pathlib import Path

from . import log, proc, resources
from .config import HOST, Config, Project, Target
from .proc import CommandError, run

BUILDS_URL = "https://partner.steamgames.com/apps/builds/{app_id}"
SUCCESS_RE = re.compile(r"Successfully finished AppID (\d+) build \(BuildID (\d+)\)")
LOGIN_HINTS = ("Cached credentials not found", "Login Failure", "Invalid Password",
               "Two-factor", "password", "Steam Guard", "Rate Limit")

# Paths inside the uploader container (see docker/depotctl-steam.sh)
CONTAINER_CONTENT = "/content/"
CONTAINER_JOB = "/job"


# --------------------------------------------------------------------------- VDF

type VdfValue = str | list[tuple[str, VdfValue]]


def _q(text: str) -> str:
    return '"' + str(text).replace('"', "'") + '"'


def render_vdf(pairs: list[tuple[str, VdfValue]], indent: int = 0) -> str:
    pad = "\t" * indent
    lines: list[str] = []
    for key, value in pairs:
        if isinstance(value, list):
            lines.append(f"{pad}{_q(key)}")
            lines.append(f"{pad}{{")
            lines.append(render_vdf(value, indent + 1))
            lines.append(f"{pad}}}")
        else:
            lines.append(f"{pad}{_q(key)}\t\t{_q(value)}")
    return "\n".join(line for line in lines if line != "")


def app_build_vdf(
    project: Project,
    targets: list[Target],
    *,
    desc: str,
    content_root: str,
    build_output: str,
    set_live: str | None,
    preview: bool,
) -> str:
    depots: list[tuple[str, VdfValue]] = []
    for t in targets:
        depot: list[tuple[str, VdfValue]] = [
            ("FileMapping", [
                ("LocalPath", f"{t.key}/*"),
                ("DepotPath", "."),
                ("Recursive", "1"),
            ]),
        ]
        depot += [("FileExclusion", pattern) for pattern in project.exclude]
        depots.append((str(project.depots[t.key]), depot))

    body: list[tuple[str, VdfValue]] = [
        ("AppID", str(project.steam_app_id)),
        ("Desc", desc),
        ("Preview", "1" if preview else "0"),
    ]
    if set_live:
        body.append(("SetLive", set_live))
    body += [
        ("ContentRoot", content_root),
        ("BuildOutput", build_output),
        ("Depots", depots),
    ]
    return render_vdf([("AppBuild", body)]) + "\n"


# --------------------------------------------------------------------------- uploader choice

def pick_uploader(cfg: Config, targets: list[Target]) -> str:
    mode = cfg.local.uploader
    if mode == "auto":
        mode = "native" if HOST == "Darwin" else "docker"
    if mode == "native" and HOST == "Windows" and any(t.key == "macos" for t in targets):
        raise CommandError(
            "native steamcmd on Windows can't upload the macOS build: Windows drops the Unix "
            "executable bit, so Macs couldn't launch it. Use uploader = \"docker\" (the default)."
        )
    return mode


def preflight(cfg: Config, mode: str) -> list[str]:
    problems = []
    if mode == "docker":
        if not shutil.which("docker"):
            problems.append("docker is not installed (Docker Desktop)")
        elif not run(["docker", "info"], capture=True, check=False, mutating=False, quiet=True).ok:
            problems.append("Docker is installed but not running; start Docker Desktop")
    else:
        if not cfg.local.steamcmd or not Path(cfg.local.steamcmd).expanduser().is_file():
            problems.append("native uploader needs steamcmd = \"<path to steamcmd>\" in depotctl.local.toml")
    return problems


# --------------------------------------------------------------------------- docker

def image_exists(cfg: Config) -> bool:
    return run(["docker", "image", "inspect", cfg.local.docker_image], capture=True,
               check=False, mutating=False, quiet=True).ok


def ensure_image(cfg: Config, *, rebuild: bool = False) -> None:
    if not rebuild and image_exists(cfg):
        return
    log.info(f"building uploader image {cfg.local.docker_image} (first run takes a minute)")
    run(["docker", "build", "--platform", "linux/amd64", "-t", cfg.local.docker_image,
         resources.path("docker")],
        echo=True)


def _docker_base(cfg: Config, *, interactive: bool) -> list[str | Path]:
    cmd: list[str | Path] = ["docker", "run", "--rm", "--platform", "linux/amd64"]
    if interactive:
        cmd += ["-it"]
    cmd += [
        "--mount", f"type=volume,source={cfg.local.docker_volume},target=/home/steam",
        "-e", f"STEAM_USER={cfg.local.steam_user}",
    ]
    return cmd


# --------------------------------------------------------------------------- native helpers

def prepare_macos_native(app: Path) -> None:
    """On a Mac: restore exec bits and ad-hoc sign if the bundle isn't validly signed."""
    for f in app.rglob("*"):
        if f.is_file() and (f.parent.name == "MacOS" or f.suffix == ".dylib"):
            f.chmod(0o755)
    if HOST != "Darwin":
        return
    verify = subprocess.run(["codesign", "--verify", "--deep", str(app)], capture_output=True)
    if verify.returncode != 0:
        log.info(f"ad-hoc signing {app.name}")
        run(["codesign", "--force", "--deep", "--sign", "-", app])


# --------------------------------------------------------------------------- upload / login

def upload(
    cfg: Config,
    project: Project,
    build_dir: Path,
    targets: list[Target],
    *,
    desc: str,
    set_live: str | None,
    preview: bool,
    verbose: bool,
) -> str | None:
    """Upload the target folders inside build_dir. Returns the Steam BuildID."""
    mode = pick_uploader(cfg, targets)
    job = build_dir / "steam"
    job.mkdir(parents=True, exist_ok=True)
    (job / "output").mkdir(exist_ok=True)
    steam_log = job / "steamcmd.log"

    if mode == "docker":
        vdf_text = app_build_vdf(project, targets, desc=desc, content_root=CONTAINER_CONTENT,
                                 build_output=f"{CONTAINER_JOB}/output/", set_live=set_live,
                                 preview=preview)
    else:
        vdf_text = app_build_vdf(project, targets, desc=desc,
                                 content_root=build_dir.resolve().as_posix() + "/",
                                 build_output=(job / "output").resolve().as_posix() + "/",
                                 set_live=set_live, preview=preview)
    vdf_path = job / "app_build.vdf"
    vdf_path.write_text(vdf_text, encoding="utf-8")
    log.info(f"wrote {vdf_path}")

    if mode == "docker":
        ensure_image(cfg)
        cmd = _docker_base(cfg, interactive=False) + [
            "--mount", f"type=bind,source={build_dir.resolve()},target=/input,readonly",
            "--mount", f"type=bind,source={job.resolve()},target={CONTAINER_JOB}",
            "-e", f"DEPOTCTL_TARGETS={' '.join(t.key for t in targets)}",
            cfg.local.docker_image, "upload",
        ]
    else:
        for t in targets:
            if t.key == "macos":
                for app in (build_dir / "macos").glob("*.app"):
                    if not proc.DRY_RUN:
                        prepare_macos_native(app)
        cmd = [Path(cfg.local.steamcmd).expanduser(),
               "+@ShutdownOnFailedCommand", "1", "+@NoPromptForPassword", "1",
               "+login", cfg.local.steam_user,
               "+run_app_build", vdf_path.resolve(), "+quit"]

    r = run(cmd, log_path=steam_log, echo=verbose, check=False)
    if proc.DRY_RUN:
        return None

    text = steam_log.read_text(encoding="utf-8", errors="replace")
    match = SUCCESS_RE.search(text)
    if match:
        return match.group(2)

    hint = ""
    if any(h.lower() in text.lower() for h in LOGIN_HINTS):
        hint = " Looks like a login problem: run `depotctl steam-login` once on this machine."
    raise CommandError(f"Steam upload failed (exit {r.returncode}, log: {steam_log}).{hint}", r.tail)


def login(cfg: Config) -> None:
    if not cfg.local.steam_user:
        raise CommandError("set steam_user in depotctl.local.toml first")
    mode = cfg.local.uploader
    if mode == "auto":
        mode = "native" if HOST == "Darwin" else "docker"
    log.info("steamcmd will ask for the password and Steam Guard code once; "
             "the login is cached for later uploads.")
    if mode == "docker":
        ensure_image(cfg)
        run(_docker_base(cfg, interactive=True) + [cfg.local.docker_image, "login"], interactive=True)
    else:
        run([Path(cfg.local.steamcmd).expanduser(), "+login", cfg.local.steam_user, "+quit"],
            interactive=True)

"""Configuration: shared `depotctl.toml` + per-machine `depotctl.local.toml`.

Both live in your workspace (the folder holding your game repos), not in the depotctl
source tree. `depotctl init` creates them.
"""

import os
import platform
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SHARED_FILE = "depotctl.toml"
LOCAL_FILE = "depotctl.local.toml"

HOST = platform.system()  # "Windows" | "Darwin" | "Linux"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Target:
    key: str
    unity_target: str     # `unity build --target` value
    editor_target: str    # Unity Editor `-buildTarget` value (fallback builder)
    module: str           # Unity module id needed when the host can't build it natively
    native_host: str      # platform.system() value whose editor includes this target
    artifact: str         # build output inside the target folder, {product} substituted

    def artifact_name(self, product: str) -> str:
        return self.artifact.format(product=product)


TARGETS: dict[str, Target] = {
    "windows": Target(
        key="windows",
        unity_target="StandaloneWindows64",
        editor_target="Win64",
        module="windows-mono",
        native_host="Windows",
        artifact="{product}.exe",
    ),
    "macos": Target(
        key="macos",
        unity_target="StandaloneOSX",
        editor_target="OSXUniversal",
        module="mac-mono",
        native_host="Darwin",
        artifact="{product}.app",
    ),
}

DEFAULT_EXCLUDE = [
    "*.pdb",
    "*_BurstDebugInformation_DoNotShip*",
    "*_BackUpThisFolder_ButDontShipItWithYourGame*",
    "*steam_appid.txt",
]


@dataclass(slots=True, kw_only=True)
class Project:
    key: str
    name: str
    path: Path
    steam_app_id: int
    product_name: str
    depots: dict[str, int]
    targets: list[str]
    set_live: str
    exclude: list[str]
    git_branch: str | None = None
    repo: str | None = None                # git URL; cloned when `path` doesn't exist
    vcs: str = "git"                       # "git" | "unity" (unity vcs sync)
    unity_build_args: list[str] = field(default_factory=list)
    mac_arch: str = "universal"            # fallback builder only: universal | x64 | arm64

    def target_list(self, override: list[str] | None = None) -> list[Target]:
        keys = override or self.targets
        unknown = [k for k in keys if k not in TARGETS]
        if unknown:
            raise ConfigError(f"unknown target(s) {unknown}; valid: {sorted(TARGETS)}")
        return [TARGETS[k] for k in keys]


@dataclass(slots=True, kw_only=True)
class Local:
    steam_user: str = ""
    uploader: str = "auto"                 # auto | docker | native
    steamcmd: str = ""                     # native uploader: path to steamcmd(.exe/.sh)
    builder: str = "unity-cli"             # unity-cli | editor
    unity_cli: str = "unity"
    unity_editor: str = ""                 # editor builder: path template with {version}
    unity_prefix: list[str] = field(default_factory=list)  # e.g. ["xvfb-run", "-a"] on headless Linux
    ensure_editor: bool = True
    docker_image: str = "depotctl-steam:latest"
    docker_volume: str = "depotctl-steam-home"
    keep_builds: int = 5
    builds_dir: str = "depotctl-builds"    # relative to the config folder


@dataclass(slots=True, kw_only=True)
class Config:
    root: Path
    workspace: Path
    projects: dict[str, Project]
    local: Local

    @property
    def builds_dir(self) -> Path:
        return (self.root / Path(self.local.builds_dir).expanduser()).resolve()

    def project(self, key: str) -> Project:
        if key in self.projects:
            return self.projects[key]
        matches = [p for p in self.projects.values() if key.lower() in (p.key.lower(), p.name.lower())]
        if len(matches) == 1:
            return matches[0]
        raise ConfigError(f"unknown project '{key}'. Known: {', '.join(self.projects) or '(none)'}")


def user_config_dir() -> Path:
    if HOST == "Windows" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "depotctl"
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "depotctl"


def find_root() -> Path:
    """Locate the folder holding depotctl.toml.

    Order: $DEPOTCTL_CONFIG (file or folder), the current folder and its parents,
    then the user config folder (~/.config/depotctl or %APPDATA%\\depotctl).
    """
    if env := os.environ.get("DEPOTCTL_CONFIG"):
        p = Path(env).expanduser().resolve()
        return p.parent if p.suffix == ".toml" else p
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / SHARED_FILE).is_file():
            return d
    if (user_config_dir() / SHARED_FILE).is_file():
        return user_config_dir()
    raise ConfigError(
        f"no {SHARED_FILE} in this folder or its parents. Run `depotctl init` in the folder "
        "that holds your game projects, or set DEPOTCTL_CONFIG to its path."
    )


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path.name}: {e}") from None


def load(root: Path | None = None) -> Config:
    root = root or find_root()
    shared = _read(root / SHARED_FILE)
    local_raw = _read(root / LOCAL_FILE)

    local_fields = {k: v for k, v in local_raw.items() if k not in ("projects", "workspace")}
    unknown = set(local_fields) - set(Local.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"{LOCAL_FILE}: unknown setting(s) {sorted(unknown)}")
    local = Local(**local_fields)
    if env_user := os.environ.get("DEPOTCTL_STEAM_USER"):
        local.steam_user = env_user
    if local.uploader not in ("auto", "docker", "native"):
        raise ConfigError(f"{LOCAL_FILE}: uploader must be auto, docker or native")
    if local.builder not in ("unity-cli", "editor"):
        raise ConfigError(f"{LOCAL_FILE}: builder must be unity-cli or editor")

    if not (root / SHARED_FILE).is_file():
        raise ConfigError(f"{root / SHARED_FILE} not found (run `depotctl init`)")
    ws = local_raw.get("workspace") or shared.get("workspace") or "."
    workspace = (root / Path(ws).expanduser()).resolve()

    defaults = shared.get("defaults", {})
    local_projects = local_raw.get("projects", {})
    projects: dict[str, Project] = {}
    for key, raw in shared.get("projects", {}).items():
        merged = {**defaults, **raw, **local_projects.get(key, {})}
        try:
            path = Path(merged["path"]).expanduser()
            projects[key] = Project(
                key=key,
                name=merged.get("name", key),
                path=path if path.is_absolute() else (workspace / path).resolve(),
                steam_app_id=int(merged.get("steam_app_id", 0)),
                product_name=merged.get("product_name") or merged.get("name", key),
                depots={k: int(v) for k, v in merged.get("depots", {}).items()},
                targets=list(merged.get("targets", ["windows", "macos"])),
                set_live=merged.get("set_live", ""),
                exclude=list(merged.get("exclude", DEFAULT_EXCLUDE)),
                git_branch=merged.get("git_branch") or None,
                repo=merged.get("repo") or None,
                vcs=merged.get("vcs", "git"),
                unity_build_args=list(merged.get("unity_build_args", [])),
                mac_arch=merged.get("mac_arch", "universal"),
            )
        except KeyError as e:
            raise ConfigError(f"project '{key}' is missing required key {e}") from None
        projects[key].target_list()  # validates target names
    return Config(root=root, workspace=workspace, projects=projects, local=local)


def validate_for_upload(cfg: Config, project: Project, targets: list[Target]) -> list[str]:
    """Return a list of problems that would make a Steam upload fail."""
    problems = []
    if project.steam_app_id <= 0:
        problems.append(f"[projects.{project.key}] steam_app_id is not set in {SHARED_FILE}")
    for t in targets:
        if project.depots.get(t.key, 0) <= 0:
            problems.append(
                f"[projects.{project.key}.depots] {t.key} depot id is not set in {SHARED_FILE} "
                "(Steamworks > SteamPipe > Depots)"
            )
    if not cfg.local.steam_user:
        problems.append(f"steam_user is not set in {LOCAL_FILE} (or DEPOTCTL_STEAM_USER)")
    return problems

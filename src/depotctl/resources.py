"""Bundled files (uploader image, Unity build script, config templates).

In a wheel they live in depotctl/resources/ (see force-include in pyproject.toml);
in a source checkout / editable install they are read from the repository root.
"""

from importlib.resources import files
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# packaged name -> path in the repository
_REPO_PATHS = {
    "docker": "docker",
    "unity": "unity",
    "templates/depotctl.toml": "depotctl.example.toml",
    "templates/depotctl.local.toml": "depotctl.local.example.toml",
}


def path(name: str) -> Path:
    """Filesystem path of a bundled resource, e.g. path("docker")."""
    packaged = Path(str(files("depotctl").joinpath("resources", *name.split("/"))))
    if packaged.exists():
        return packaged
    top, _, rest = name.partition("/")
    repo_rel = _REPO_PATHS.get(name) or (_REPO_PATHS.get(top, top) + (f"/{rest}" if rest else ""))
    in_repo = _REPO_ROOT / repo_rel
    if in_repo.exists():
        return in_repo
    raise FileNotFoundError(f"bundled resource '{name}' is missing from this depotctl install")

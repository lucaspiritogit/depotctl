#!/usr/bin/env bash
# Prepare a headless Debian 12/13 (x86_64) server to run depotctl.
#
# Run it from a depotctl checkout as your normal user (it uses sudo for apt):
#   ./scripts/setup-debian.sh                       installs everything
#   ./scripts/setup-debian.sh --workspace ~/games   folder for your game repos + depotctl.toml
#                                                   (default: the folder containing this checkout)
#   ./scripts/setup-debian.sh --no-docker           skip Docker (if you upload from elsewhere)
#
# Safe to re-run: every step checks first and skips what is already there.
set -euo pipefail

DEPOTCTL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_DOCKER=1
WORKSPACE="$(dirname "$DEPOTCTL_DIR")"
RELOGIN=0

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m  ✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31merror: %s\033[0m\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-docker) WITH_DOCKER=0 ;;
    --workspace) [[ $# -ge 2 ]] || die "--workspace needs a folder"; WORKSPACE="$2"; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

# --------------------------------------------------------------------------- checks
say "Checking the system"
[[ $EUID -ne 0 ]] || die "run as your normal user, not root (sudo is used where needed)"
command -v sudo >/dev/null || die "sudo is required (apt install sudo, then add yourself to the sudo group)"
[[ "$(uname -m)" == "x86_64" ]] || die "x86_64 required: steamcmd and the Unity editor are x86 binaries"
# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == "debian" ]] || warn "this is ${PRETTY_NAME:-unknown}, not Debian; continuing anyway"
if [[ -n "${VERSION_ID:-}" && "${VERSION_ID%%.*}" -lt 12 ]]; then
  die "Debian ${VERSION_ID} is too old: the Unity CLI needs glibc 2.34+ (Debian 12 or newer)"
fi
ok "${PRETTY_NAME:-Debian} on $(uname -m)"

# --------------------------------------------------------------------------- apt
# Package names changed in Debian 13 (the t64 transition); use whichever exists.
pick() {
  local p
  for p in "$@"; do
    if apt-cache show "$p" >/dev/null 2>&1; then echo "$p"; return; fi
  done
  warn "none of these packages exist here: $*"
}

say "Installing packages"
sudo apt-get update -qq
packages=(
  git git-lfs openssh-client curl ca-certificates tar xz-utils unzip cpio lsb-release xvfb
  # Unity editor runtime libraries (needed even in -batchmode -nographics)
  "$(pick libgtk-3-0t64 libgtk-3-0)" "$(pick libasound2t64 libasound2)"
  libglu1-mesa libnss3 libgbm1 libxss1 libxtst6 libnotify4 libcap2
)
if [[ $WITH_DOCKER -eq 1 ]] && ! command -v docker >/dev/null; then
  packages+=(docker.io)
fi
mapfile -t packages < <(printf '%s\n' "${packages[@]}" | sed '/^$/d')
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${packages[@]}"
git lfs install --skip-repo >/dev/null
ok "apt packages"

# --------------------------------------------------------------------------- docker
if [[ $WITH_DOCKER -eq 1 ]]; then
  say "Docker"
  sudo systemctl enable --now docker >/dev/null 2>&1 || warn "couldn't enable the docker service"
  if ! id -nG "$USER" | grep -qw docker; then
    sudo usermod -aG docker "$USER"
    RELOGIN=1
    warn "added $USER to the docker group; log out and back in for it to apply"
  fi
  ok "$(docker --version 2>/dev/null || echo 'docker installed')"
fi

# --------------------------------------------------------------------------- uv + python
say "uv and Python 3.15"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
uv python install 3.15 >/dev/null
ok "$(uv --version), Python 3.15"

# --------------------------------------------------------------------------- unity cli
say "Unity CLI"
if ! command -v unity >/dev/null; then
  curl -fsSL https://unity.com/install.sh | bash
fi
ok "unity $(unity --version 2>/dev/null || echo '(installed)')"

# --------------------------------------------------------------------------- depotctl
say "depotctl"
uv tool install --quiet --force --python 3.15 -e "$DEPOTCTL_DIR"
mkdir -p "$WORKSPACE"
WORKSPACE="$(cd "$WORKSPACE" && pwd)"
if [[ -f "$WORKSPACE/depotctl.toml" ]]; then
  ok "$WORKSPACE/depotctl.toml already exists, left as is"
else
  depotctl init "$WORKSPACE"
fi
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if ! ssh-keygen -F github.com >/dev/null 2>&1; then
  ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null && ok "added github.com to known_hosts"
fi
ok "$(depotctl --version)"

# --------------------------------------------------------------------------- next steps
say "Next steps"
cat <<EOF
  $( [[ $RELOGIN -eq 1 ]] && echo "0. Log out and back in (docker group), then continue." )
  1. Put your team's depotctl.toml in $WORKSPACE (or fill in the generated one),
     then set steam_user in $WORKSPACE/depotctl.local.toml.
     For private repos, add a deploy key and use SSH URLs, overriding per project there:
       [projects.my-game]
       repo = "git@github.com:your-org/my-game.git"
  2. Sign Unity in and check the license:
       unity auth login        (or export UNITY_SERVICE_ACCOUNT_ID / _SECRET)
       unity license status
  3. cd $WORKSPACE && depotctl clone --all
  4. depotctl steam-login      (password + Steam Guard code, once)
  5. depotctl doctor
  6. depotctl deploy <project> --preview
  If Unity complains about a missing display, add to depotctl.local.toml:
       unity_prefix = ["xvfb-run", "-a"]
EOF

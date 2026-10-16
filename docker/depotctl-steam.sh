#!/usr/bin/env bash
# Entrypoint of the depotctl uploader image.
#   login   interactive steamcmd login; the session is cached in the /home/steam volume
#   upload  stage /input/<target> folders, fix + sign macOS apps, run steamcmd run_app_build
set -euo pipefail

STEAMCMD="${STEAMCMD:-/home/steam/steamcmd/steamcmd.sh}"
INPUT_DIR="${INPUT_DIR:-/input}"
CONTENT_DIR="${CONTENT_DIR:-/content}"
JOB_DIR="${JOB_DIR:-/job}"

say() { printf '[depotctl-steam] %s\n' "$*"; }
die() { say "ERROR: $*" >&2; exit 1; }

prepare_macos() {
  local app
  shopt -s nullglob
  for app in "$CONTENT_DIR"/macos/*.app; do
    say "fixing permissions in $(basename "$app")"
    # Builds made on Windows lose the Unix executable bit.
    find "$app" -type f \( -path '*/Contents/MacOS/*' -o -name '*.dylib' \) -exec chmod 755 {} +
    say "ad-hoc signing $(basename "$app") (needed to launch on Apple Silicon)"
    rcodesign sign "$app"
  done
  shopt -u nullglob
}

case "${1:-}" in
  login)
    [[ -n "${STEAM_USER:-}" ]] || die "STEAM_USER is not set"
    exec "$STEAMCMD" +login "$STEAM_USER" +quit
    ;;

  upload)
    [[ -n "${STEAM_USER:-}" ]] || die "STEAM_USER is not set"
    [[ -f "$JOB_DIR/app_build.vdf" ]] || die "$JOB_DIR/app_build.vdf not found"
    [[ -n "${DEPOTCTL_TARGETS:-}" ]] || die "DEPOTCTL_TARGETS is not set"

    rm -rf "$CONTENT_DIR" && mkdir -p "$CONTENT_DIR"
    for target in $DEPOTCTL_TARGETS; do
      [[ -d "$INPUT_DIR/$target" ]] || die "missing build folder: $target"
      say "staging $target"
      cp -a "$INPUT_DIR/$target" "$CONTENT_DIR/$target"
    done
    [[ -d "$CONTENT_DIR/macos" ]] && prepare_macos

    log="$JOB_DIR/steamcmd.log"
    say "running steamcmd as $STEAM_USER"
    set +e
    "$STEAMCMD" +@ShutdownOnFailedCommand 1 +@NoPromptForPassword 1 \
      +login "$STEAM_USER" +run_app_build "$JOB_DIR/app_build.vdf" +quit 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    set -e
    # steamcmd sometimes exits 0 on failure, so trust the success line instead.
    if grep -q "Successfully finished AppID" "$log"; then
      say "upload finished"
      exit 0
    fi
    say "steamcmd did not report success (exit $rc)"
    exit $(( rc == 0 ? 1 : rc ))
    ;;

  *)
    echo "usage: depotctl-steam login|upload" >&2
    exit 2
    ;;
esac

# depotctl

Ship Unity games to Steam with one command. depotctl picks a project, pulls it, builds
Windows and macOS players, and uploads each one to its own Steam depot.

```
$ depotctl deploy my-game
==> My Game  [windows, macos]
==> Preflight           ✓ config and tools look good
==> Pull latest changes ✓ main @ 3f2a9c1d  Add forge level
==> Unity editor        ✓ Unity 6000.6.4f1 with ['mac-mono']
==> Build windows       ✓ windows: My Game.exe
==> Build macos         ✓ macos: My Game.app
==> Upload to Steam     ✓ Steam BuildID 12345678, live on 'testing'
```

> **Status: alpha.** The pipeline is covered by automated tests that use a real git
> remote and stand-ins for `unity`, `docker` and `steamcmd`. It hasn't been proven on
> many real projects yet. Try `--preview` (Steam validates the upload but nothing goes
> live) before trusting it with a release, and please report what breaks.

## How it works

| Step | What happens |
|---|---|
| Clone / pull | `git clone` if the project is missing, then `git fetch` and `git pull --ff-only` (plus `git lfs pull`). It refuses to run on uncommitted changes. |
| Editor | Reads `ProjectVersion.txt` and uses the [Unity CLI](https://docs.unity.com/en-us/unity-cli/use-unity-cli) to install that editor and any build modules it's missing. |
| Build | `unity build --target … --output-path …`, once per OS. With the Mono backend, any host can build both. |
| Upload | Runs steamcmd `run_app_build` with a generated VDF. Each OS goes to its own depot, and the build can be set live on a beta branch automatically. |

### macOS builds from Windows or Linux

A macOS build made on Windows comes out with two problems. Windows can't store the Unix executable bit, and nothing signs the app, so Apple Silicon Macs refuse to run it.

On Windows and Linux, depotctl therefore uploads from a small Linux container (bundled, built locally on first use). The container restores the executable bits and ad-hoc signs the `.app` with [rcodesign](https://github.com/indygreg/apple-platform-rs). Then it runs steamcmd.

On a Mac, depotctl uses native steamcmd and ad-hoc signs the app with `codesign` only if it isn't already validly signed.

## Install

You need these tools:

- **Python 3.12+**
- **git**
- **The [Unity CLI](https://docs.unity.com/en-us/unity-cli/use-unity-cli)**, signed in with `unity auth login`
- **To upload from Windows or Linux:** Docker (Docker Desktop on Windows)
- **To upload from macOS:** [steamcmd](https://developer.valvesoftware.com/wiki/SteamCMD)

Then install depotctl:

```
uv tool install git+https://github.com/lucaspiritogit/depotctl     # or: pipx install git+https://…
```

## Quick start

Run `init` in the folder that holds your Unity projects. It writes a `depotctl.toml` with a section for every project it finds, and a per-machine `depotctl.local.toml`:

```
cd ~/games
depotctl init
```

Then:

1. **Fill in `depotctl.toml`:** each game's `steam_app_id` and depot IDs (see [Steamworks setup](#steamworks-setup)).
2. **Fill in `depotctl.local.toml`:** set `steam_user`. On macOS, also set `steamcmd`.
3. **Log in to Steam once:** `depotctl steam-login` asks for the password and Steam Guard code, then caches the session.
4. **Check the setup:** `depotctl doctor`.
5. **Do a trial run:** `depotctl deploy <project> --preview`. Nothing goes live.

depotctl finds `depotctl.toml` by searching the current folder and its parents, so it works from inside any of your game folders. To use it from somewhere else, set `DEPOTCTL_CONFIG=/path/to/depotctl.toml`, or put the file in `~/.config/depotctl/` (`%APPDATA%\depotctl\` on Windows).

### Steamworks setup

- **Build account:** create a dedicated Steam account for uploads (Users & Permissions > Manage Users). Give it only *Edit App Metadata* and *Publish App Changes To Steam* for your app.
- **Depots:** in SteamPipe > Depots, set the Windows depot to OS = Windows, then add a macOS depot with OS = macOS. Publish, then copy both IDs into `depotctl.toml`.
- **Beta branch:** in SteamPipe > Builds, create a beta branch (e.g. `testing`). Password-protect it if testers should need a code.

## Usage

```
depotctl init [dir]                 # create depotctl.toml listing the Unity projects in dir
depotctl list [--discover]          # configured projects (+ config for unconfigured ones)
depotctl deploy [project]           # pull + build + upload (asks which project if omitted)
depotctl deploy my-game -t windows  # one platform only
depotctl deploy my-game --preview   # steamcmd validates everything, uploads nothing
depotctl deploy my-game --dry-run   # print the commands without running them
depotctl build my-game              # pull + build, no upload
depotctl upload my-game             # re-upload the latest build (e.g. after a network error)
depotctl upload my-game -b 3f2a9c1d --set-live qa
depotctl builds my-game             # past builds and their Steam BuildIDs
depotctl clone --all                # clone configured projects that aren't on this machine
depotctl doctor                     # check tools, config and Steam login
depotctl steam-login                # cache the steamcmd login (once per machine)
```

Useful flags:

- `-v`: stream the Unity and steamcmd output.
- `--skip-pull`: build the current checkout as-is.
- `--allow-dirty`: build even with uncommitted changes.
- `--no-set-live`: upload without making the build live anywhere.

depotctl never sets a build live on `default` (the public branch) unless you also pass `--allow-default-branch`.

Each build gets its own folder under `depotctl-builds/<project>/<date>-<commit>/`, with a `manifest.json` and the Unity and steamcmd logs. Only the newest `keep_builds` folders are kept.

## Configuration

- [`depotctl.example.toml`](depotctl.example.toml) shows every per-project setting. This file can be shared with your team.
- [`depotctl.local.example.toml`](depotctl.local.example.toml) shows the per-machine settings: Steam user, uploader, builder, paths. Keep the local file out of version control.

Project paths are relative to the folder containing `depotctl.toml`. A project with a `repo` URL is cloned automatically the first time it's built.

### If `unity build` misbehaves

The Unity CLI is experimental. If it gives you trouble:

- Check the target names with `unity build --list-targets --project-path <game>`. depotctl uses `StandaloneWindows64` and `StandaloneOSX`.
- Pass extra flags through `unity_build_args = [...]`.
- Or set `builder = "editor"` in `depotctl.local.toml`. depotctl then runs the editor directly in batch mode, using a small build script. Run `depotctl install-build-script <project>` once and commit the file it copies.

Set your macOS Build Profile's architecture to *Intel 64-bit + Apple silicon* so the game runs natively on every Mac.

## Running on a headless Debian server

A Debian 12/13 x86_64 server can build both platforms and upload through Docker with no emulation. `scripts/setup-debian.sh` installs everything the server needs, from a checkout of this repo:

```
git clone https://github.com/lucaspiritogit/depotctl ~/games/depotctl
~/games/depotctl/scripts/setup-debian.sh --workspace ~/games
```

It installs git and git-lfs, the Unity editor's libraries, xvfb, Docker, uv with Python, the Unity CLI and depotctl itself. It then runs `depotctl init`. It's safe to re-run.

On a server:

- **Private repos:** use SSH URLs with a deploy key. Git is never allowed to prompt, so unattended runs fail instead of hanging.
- **Nightly builds with cron:**
  ```
  0 3 * * * cd ~/games && ~/.local/bin/depotctl deploy my-game >> ~/depotctl.log 2>&1
  ```
- **Display errors:** if Unity complains about a missing display, set `unity_prefix = ["xvfb-run", "-a"]`.

## Troubleshooting

- **Login errors on upload:** the cached Steam session expired. Run `depotctl steam-login` again.
- **The upload failed partway:** run `depotctl upload <project>`. It reuses the build.
- **The macOS build won't launch:** make sure it was uploaded through the container or from a Mac. depotctl refuses native steamcmd on Windows for macOS builds.
- **You changed the uploader image:** run `depotctl docker-build`. The Steam login lives in the `depotctl-steam-home` Docker volume and survives the rebuild.

## Development

```
git clone https://github.com/lucaspiritogit/depotctl && cd depotctl
uv venv && uv pip install -e .
uv run python -m unittest discover -s tests -v
```

The end-to-end tests run a full deploy against a real git remote, with fake `unity`, `docker` and `steamcmd`. They run on macOS and Linux. CI also runs the unit tests on Windows, shellchecks the scripts and builds the uploader image.

Issues and pull requests are welcome. Please include the output of `depotctl doctor` and the relevant log from the build folder when reporting a problem.

## License and trademarks

MIT, see [LICENSE](LICENSE).

The uploader image downloads steamcmd (Valve) and rcodesign (MPL-2.0) when you build it. They are not distributed with this project. Please don't publish built images, because steamcmd isn't redistributable.

depotctl is an independent project, not affiliated with or endorsed by Valve Corporation or Unity Technologies. Steam and Steamworks are trademarks of Valve Corporation. Unity is a trademark of Unity Technologies.

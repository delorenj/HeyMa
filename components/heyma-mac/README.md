# HeyMa for Mac

Native AppKit menu-bar and settings app, `sh.delo.heyma`. It is the Mac end of the HeyMa shell (`docs/heyma-shell.md`): one icon with the same section model as the Linux tray. It owns no capture and runs no Python; Vinyl's installed client owns the microphone, RPC and text injection. Operator route: `skills/heyma/references/shell.md` ("Mac app").

Version: `VERSION` (own semver, decoupled from the Vinyl client). Releases are tags `heyma-v<VERSION>` in `delorenj/vinyl-client-releases`; the cask `delorenj/tap/heyma` installs them.

## Menu

Top **Dictation** toggle (also Control-Option-D when enabled), then the sections:

- **Wax**: absent until the Voice Memos bridge (S6, HEYMA-35) fills `waxSectionItems()`. An empty section adds no menu item.
- **Vinyl**: `Connected: host:port` (the client's `status` field `server`; a client without it, 0.5.0 and older, falls back to `~/.config/vinyl/client.json`). When client.json names another server it shows `Restart the client to use X` and a **Restart client** item (`launchctl kickstart -k gui/$UID/sh.delo.vinyl.client`, refused while dictating). A `Server unreachable` line shows when status says so. Cancel dictation, Copy trace reference, Open client log.
- **Phonograph**: `GET <url>/healthz` every 30 s and **Open Phonograph**. `HEYMA_PHONOGRAPH_URL` overrides `https://phonograph.intellifornia.com`; `HEYMA_PHONOGRAPH=off` hides it.
- Settings…, Quit HeyMa.

A hotkey press the app cannot honour (client unreachable, or a command still running) beeps and flashes `offline` or `busy` on the status item for 1.6 s instead of doing nothing.

## Build, sign, notarize (from big-chungus)

```sh
components/heyma-mac/release.sh            # build + Developer ID sign + notarize + staple
components/heyma-mac/release.sh --no-notarize   # sign only, to check the toolchain
```

`release.sh` ships this directory to a fresh temp dir on the Mac (`ssh mac`), and `mac-sign-notarize.sh` runs there in one ssh session: unlock the login keychain, `build-app.sh` (universal arm64+x86_64, `--options runtime --timestamp`, identity `Developer ID Application: Jarad DeLorenzo (YJ7W7Z36FP)`), `notarytool submit --wait` with the App Store Connect API key, `stapler staple` and `validate`, `codesign --verify --deep --strict`, `spctl -a -vvv -t exec`. The stapled zip lands in `dist/HeyMa-<VERSION>-macos-universal.zip` with its size and sha256. All secrets are `op://` references read on big-chungus and piped over ssh stdin; nothing is written to the repo, an argv here, or output. A rejected notarization prints the notarytool log and stops (no retry).

`build-app.sh` also runs standalone on a Mac (`HEYMA_SIGN_IDENTITY=-` signs ad hoc for local development).

No entitlements file: the app does not capture audio, load unsigned code or use JIT.

## Tests

`python3 -m pytest tests` (source contract and release invariants; Swift compiles only on a Mac).

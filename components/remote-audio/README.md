# SSH audio

After installation, run `ssh big-chungus` in a Mac zsh terminal as usual.
The login imports the Mac's microphone and speakers, switches Big Chungus's
defaults, and moves streams using its previous defaults. Logging out restores
the previous devices and routes. Zellij sessions can stay running.

Each login owns an independent reverse tunnel and a process-stamped lease.
The latest healthy login supplies audio; another login takes over when it
disconnects. The last disconnect restores local audio. SSH keepalives bound
network-failure detection to roughly ten seconds. A server watcher restores
state after a killed session or watcher, even if an idle SSH master persists.

Explicitly selected devices other than the prior defaults remain selected.
Applications that bypass PipeWire/PulseAudio and open hardware directly cannot
be rerouted by this bridge. Native `/voice` availability remains app-dependent.
Changing the default microphone also changes the microphone selected by future
applications that use the default, including recordings started while remote.

## Install on Big Chungus

```sh
chmod +x bin/ssh-audio
bin/ssh-audio install
```

The user service stays idle without a live login lease. Recovery state lives
in `~/.local/state/ssh-audio/state.json`; leases and status live in
`$XDG_RUNTIME_DIR/ssh-audio/`.

## Install on the Mac

Install Homebrew PulseAudio once (`brew install pulseaudio`). Copy `macos/`
to `~/.local/share/ssh-audio/`, then run its `install.py` using Python 3.
It saves `.zshrc.before-ssh-audio` before adding a managed hook. Existing
SSH configuration and commands to other hosts remain unchanged.

Open a new terminal tab, or source `~/.local/share/ssh-audio/shell.zsh`.
macOS may require microphone permission for the local terminal/PulseAudio.
The bridge starts PulseAudio on demand, binds only to loopback, removes its
listener on exit, and uses a private SSH control socket for each login.

`ssh-big-chungus --check` connects, reports server audio status, and exits.
`ssh-audio status` on Big Chungus reports the active routing and devices.
Logs: `journalctl --user -u ssh-audio.service`.

SSH invocations with additional flags/remote commands pass through the shell
hook unchanged. Use `ssh-big-chungus HOST` explicitly for an audio login to
another configured alias of Big Chungus.

## Disable

On Big Chungus, `systemctl --user disable --now ssh-audio.service` restores
audio and disables automatic routing. Remove the `BEGIN SSH AUDIO` block
from the Mac's `.zshrc` and open a new terminal to restore ordinary `ssh`.

## Tests

```sh
python3 -m pytest components/remote-audio/tests
```

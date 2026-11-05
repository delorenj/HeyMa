# syncthing-audio — the phone feed into `dropoff/`

The Syncthing folder ID is `audio`. The hub-side displayed label is `Audio`
(as of 2026-11-05; older docs called it `Transcription Inbox`, and spokes may
still show their own labels). The hub is big-chungus, receiving into
`~/HeyMa/dropoff` as `receiveonly`. Devices sharing the folder: big-chungus
(hub), PottyBarron (the phone sender), mommymac, phoneballs, tiny-chungus.
Spokes use `sendonly` and share this folder with big-chungus and themselves
only. Other folders can keep their existing sharing. Disable the big-chungus
introducer on each spoke so it cannot automatically reintroduce audio shares
between spokes.

Only root-level audio files are accepted. Syncthing does not flatten paths:
recorders must publish completed, uniquely named recordings directly into the
root of their sending folder. Existing
nested recordings must be preserved before changing their layout.

`ignoreDelete=true` protects the hub from sender deletions. It does not stop a
remote update or “Revert Local Changes” from restoring a hub deletion. The
audio policy therefore adds each completed filename to permanent, root-level
ignore rules as soon as it arrives. Local audio stays readable. A later deletion,
remote edit, folder rescan, revert, or restart cannot redownload that filename
once the exclusion is committed. Each recording must have a unique filename;
updates under a previously received name are intentionally excluded too.

`guard.py` manages Syncthing metadata only. It never copies, moves, deletes,
archives, or transcribes audio; Wax owns those stages. Its durable filename
list is `var/syncthing-audio/received.json`, also represented in `dropoff/.stignore`.
The service retries after a Syncthing outage and restores missing ignore rules
from that list. Do not delete either file to clear a sync warning.

Wax's worker copies accepted recordings into `inbox/` through `.staging/`,
verifies the copy by SHA-256, and publishes it with `renameat2(RENAME_NOREPLACE)`.
Content already present in the Wax ledger is never imported again, including
audio subsequently deleted from the local queue. The copy runs inside the
worker loop, so it happens only while pipeline processing is enabled
(`wax pipeline status`) or on a manual `wax drain`.

Install the user unit and run it:

```sh
install -m 644 components/syncthing-audio/syncthing-audio-policy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now syncthing-audio-policy.service
```

Inspect the saved exclusions and service:

```sh
journalctl --user -u syncthing-audio-policy.service -n 20
systemctl --user status syncthing-audio-policy.service
```

Did a recording actually reach the hub? Ask Syncthing, not the file listing:
`lastFile` on folder `audio` (REST `stats/folder`) names and timestamps the
most recent arrival (e.g. `20261104 150629.m4a` at 2026-11-04T15:11-05:00).

Wax-side operation of the whole feed — the worker copy, the inbox rules and
the "recorded but no transcript" triage — is documented in the heyma skill
hub at `skills/heyma/references/intake.md`.

Syncthing's documentation: [folder types](https://docs.syncthing.net/users/foldertypes.html),
[ignore rules](https://docs.syncthing.net/users/ignoring.html), and
[incoming deletion handling](https://docs.syncthing.net/advanced/folder-ignoredelete.html).

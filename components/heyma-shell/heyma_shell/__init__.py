"""HeyMa shell: one client per host.

`registry.collect()` asks each product (Wax, Vinyl, Telephony, Phonograph) for
its status through that product's own CLI or endpoint and returns one shape;
the tray, `heyma status` and (later) the window and setup wizard all read it.
This package never imports Vinyl code (co-location rule) and owns no capture,
no worker and no state: toggles stay `vinyl toggle` and `wax rec toggle`.
"""

SCHEMA = "heyma.shell.status.v1"

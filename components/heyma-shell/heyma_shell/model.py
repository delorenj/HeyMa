"""The tray's brain, with no GTK: combined colour, labels, and the menu spec.

`verdict()` answers "what colour is the one icon, and why". `labels()` answers
"what does each menu row say right now". `MENU` is the static shape of the menu.
The GTK layer (`tray_ui`) only draws what these return, so the interesting
rules are unit-testable on any machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .adapters import PRODUCTS, vinyl_verdict, wax_verdict

DEGRADED = ("warn", "fail")
# Sections whose live verdict comes from a state mirror read every second. Their
# slow registry health is NOT also counted, or a recovery would lag the 30 s refresh.
FAST_SECTIONS = ("wax",)
# A section must read degraded this many collects in a row before the icon turns
# yellow. A hosted Phonograph that drops one probe must not flash the desktop.
DEBOUNCE_AFTER = 2
# Sections whose status line is the registry summary (no state mirror of their own).
REGISTRY_ROWS = ("telephony", "phonograph", "voxxy")
SECTION_LABEL = {"wax": "Wax", "vinyl": "Vinyl", "telephony": "Telephony",
                 "phonograph": "Phonograph", "voxxy": "Voxxy"}


class Debounce:
    """Asymmetric: slow to turn on (N bad reads in a row), instant to clear."""

    def __init__(self, after: int = DEBOUNCE_AFTER):
        self.after = after
        self.streak: dict[str, int] = {}

    def update(self, sections: dict[str, dict]) -> set[str]:
        for product, row in sections.items():
            if row.get("installed") and row.get("health") in DEGRADED:
                self.streak[product] = self.streak.get(product, 0) + 1
            else:
                self.streak[product] = 0
        return self.degraded()

    def degraded(self) -> set[str]:
        return {p for p, n in self.streak.items() if n >= self.after}


@dataclass
class Inputs:
    """One tick's worth of facts. Every field may be missing (None / empty)."""

    wax_snap: Any = None
    vinyl_snap: Any = None
    sections: dict = field(default_factory=dict)      # id -> adapter row (slow, may be empty)
    slow_degraded: set = field(default_factory=set)   # ids that passed the debounce
    installed: dict = field(default_factory=dict)     # id -> bool, from the quick probe
    now: float = 0.0


def installed_ids(inputs: Inputs) -> list[str]:
    """Products to show. The full collect wins once it exists; the quick probe before it."""
    if inputs.sections:
        return [p for p in PRODUCTS if (inputs.sections.get(p) or {}).get("installed")]
    return [p for p in PRODUCTS if inputs.installed.get(p)]


@dataclass
class Verdict:
    colour: str
    flash: str | None
    tooltip: str
    recording: bool
    dictating: bool
    degraded: list[str]


def verdict(inputs: Inputs) -> Verdict:
    """Red while any microphone is hot, yellow if any installed section is degraded.

    Recording and dictating always win: you must be able to see a hot mic even
    when everything behind it is on fire (the rule both source trays follow).
    """
    shown = installed_ids(inputs)
    recording = dictating = False
    degraded: list[str] = []
    lines = ["HeyMa"]
    flash = None

    if "wax" in shown:
        wax = wax_verdict(inputs.wax_snap, inputs.now)
        recording, flash = wax["recording"], wax["flash"]
        if wax["colour"] == "yellow":
            degraded.append("wax")
        wax_lines = wax["tip"].splitlines()
        lines.append("Wax: " + wax_lines[0].replace("stream: ", ""))
        # inbox, cause, then whatever is wrong (failed items with their causes,
        # transcription degraded, a silent source): the terms worth a glance.
        lines.extend("  " + x for x in wax_lines[1:7])
    if "vinyl" in shown and inputs.vinyl_snap is not None:
        vinyl = vinyl_verdict(inputs.vinyl_snap)
        dictating = vinyl["listening"]
        if vinyl["colour"] == "yellow":
            degraded.append("vinyl")
        lines.append("Vinyl: " + vinyl["tip"].splitlines()[0])
    # The slow registry reads cover everything the mirrors cannot (a dead vinyl
    # daemon, Telephony, the hosted Phonograph); Wax is judged by its mirror alone.
    for product in shown:
        if product not in FAST_SECTIONS and product in inputs.slow_degraded \
                and product not in degraded:
            degraded.append(product)
    for product in shown:
        row = inputs.sections.get(product) or {}
        if product in REGISTRY_ROWS and row.get("summary"):
            lines.append(f"{SECTION_LABEL[product]}: {row['summary']}")
    if recording or dictating:
        colour = "red"
        hot = [name for name, on in (("recording", recording), ("dictating", dictating)) if on]
        lines.insert(1, "● " + " + ".join(hot))
    elif degraded:
        colour = "yellow"
    else:
        colour = "green"
    return Verdict(colour, flash if recording else None, "\n".join(lines), recording,
                   dictating, degraded)


# -- the menu -----------------------------------------------------------------

def _item(item_id: str, label: str) -> dict:
    return {"kind": "item", "id": item_id, "label": label}


def _status(item_id: str) -> dict:
    return {"kind": "status", "id": item_id, "label": "…"}


def _sep() -> dict:
    return {"kind": "separator"}


def _sub(item_id: str, label: str, children: list[dict]) -> dict:
    return {"kind": "submenu", "id": item_id, "label": label, "children": children}


# "wax-body" is the Wax menu's own rows (status, Queue, Open inbox, Settings…,
# Telephony…), drawn by wax.tray.QueueMenu so the section cannot drift from the
# standalone Wax tray.
MENU: list[dict] = [
    _item("dictation", "Start dictation"),
    _item("recording", "Start recording"),
    _sep(),
    _sub("wax", "Wax", [{"kind": "wax-body", "id": "wax.body"}]),
    _sub("vinyl", "Vinyl", [
        _status("vinyl.status"),
        _item("vinyl.trace", "Copy last trace reference"),
        _item("vinyl.settings", "Vinyl settings…"),
    ]),
    _sub("telephony", "Telephony", [
        _status("telephony.status"),
        _item("telephony.silence", "Silence local ringer"),
        _item("telephony.page", "Open Telephony page"),
    ]),
    _sub("phonograph", "Phonograph", [
        _status("phonograph.status"),
        _item("phonograph.open", "Open Phonograph"),
    ]),
    _sub("voxxy", "Voxxy", [
        _status("voxxy.status"),
        _item("voxxy.test", "Say a test line"),
        _item("voxxy.open", "Open Voxxy"),
    ]),
    _sep(),
    _item("settings", "Settings…"),
    _item("quit", "Quit HeyMa"),
]


def walk(spec: list[dict] = MENU):
    for entry in spec:
        yield entry
        yield from walk(entry.get("children", []))


def labels(inputs: Inputs, view: Verdict, *, vinyl_settings: bool = False) -> dict[str, dict]:
    """Row id -> {label?, sensitive?, visible?} for this tick.

    A section appears only when its product is installed on this host, and
    hiding it never stops that product's service.
    """
    shown = installed_ids(inputs)
    out: dict[str, dict] = {p: {"visible": p in shown} for p in PRODUCTS}
    wax_on = "wax" in shown
    vinyl_on = "vinyl" in shown
    wax_fresh = wax_verdict(inputs.wax_snap, inputs.now)["fresh"] if wax_on else False
    out["dictation"] = {"label": "Stop dictation" if view.dictating else "Start dictation",
                        "sensitive": vinyl_on, "visible": vinyl_on}
    out["recording"] = {"label": "Stop recording" if view.recording else "Start recording",
                        "sensitive": wax_on and wax_fresh, "visible": wax_on}
    out["settings"] = {"sensitive": True}

    if wax_on:
        wax = wax_verdict(inputs.wax_snap, inputs.now)
        out["wax.status"] = {"label": wax["tip"].replace("\n", " · ")}
    if vinyl_on:
        snap = inputs.vinyl_snap if isinstance(inputs.vinyl_snap, dict) else {}
        vinyl = vinyl_verdict(inputs.vinyl_snap)
        row = inputs.sections.get("vinyl") or {}
        endpoint = (row.get("detail") or {}).get("endpoint") or snap.get("server") or snap.get("peer") or ""
        line = vinyl["tip"].replace("\n", " · ")
        if endpoint:
            line += f" · endpoint {endpoint}"
        out["vinyl.status"] = {"label": line}
        out["vinyl.trace"] = {"sensitive": bool(snap.get("trace_ref")) and not vinyl["listening"]}
        out["vinyl.settings"] = {"sensitive": vinyl_settings}
    for product in REGISTRY_ROWS:
        if product in shown:
            row = inputs.sections.get(product) or {}
            out[f"{product}.status"] = {"label": row.get("summary") or "checking…"}
    return out

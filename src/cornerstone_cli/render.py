"""Human-readable session summary (spec §17)."""

from __future__ import annotations

import shlex

_LABELS = {
    "file.created": "CREATED",
    "file.deleted": "DELETED",
    "file.modified": "MODIFIED",
    "file.metadata_modified": "METADATA",
    "file.renamed": "RENAMED",
}


def render_summary(records: list[dict]) -> str:
    started = records[0]
    finished = records[-1]
    events = records[1:-1]

    lines = [
        f"Session: {started['id']}",
        f"Declared actor: {started['actor']}",
        f"Command: {shlex.join(started['command'])}",
        f"Outcome: {finished['outcome']}",
        f"Duration: {finished['duration_s']:.1f} seconds",
    ]
    if started.get("spec") != "0.1":
        # Spec v0.2 §4: the summary always declares the effective confinement.
        backend = started["confinement_backend"]
        if backend == "none":
            lines.append("Confinement: none")
        else:
            scope = ", signal scope" if started["confinement_signal_scope"] else ""
            lines.append(f"Confinement: {backend} ({started['confinement_profile']} profile{scope})")
    lines.append("")

    if finished["outcome"] == "incomplete":
        lines.append("The final snapshot could not be completed:")
        lines.append("the net effects of this session are unknown.")
    elif not events:
        lines.append("No net effects detected.")
    else:
        lines.append("Net effects detected:")
        for event in events:
            label = _LABELS.get(event["type"], event["type"]).ljust(12)
            if event["type"] == "file.renamed":
                lines.append(f"{label} {event['path_before']} → {event['path']}")
            else:
                lines.append(f"{label} {event['path']}")
        lines.append("")
        lines.append("The order shown does not necessarily represent")
        lines.append("the chronological order of the operations.")

    return "\n".join(lines)

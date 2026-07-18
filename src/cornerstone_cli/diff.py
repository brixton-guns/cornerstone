"""Net-effect computation between two snapshots (spec §7, §8, §9)."""

from __future__ import annotations

from .snapshot import Entry


def diff_events(before: dict[str, Entry], after: dict[str, Entry]) -> list[dict]:
    """Return the ordered event payloads (without `ts` and `prev`) for the net delta."""
    created = set(after) - set(before)
    deleted = set(before) - set(after)
    events: list[dict] = []

    # Rename inference (§8): a deleted path and a created path pair up only when
    # their (entry_type, hash) correspondence is unique and the content is non-empty.
    deleted_by_key: dict[tuple[str, str], list[str]] = {}
    for path in deleted:
        entry = before[path]
        deleted_by_key.setdefault((entry.entry_type, entry.hash), []).append(path)
    created_by_key: dict[tuple[str, str], list[str]] = {}
    for path in created:
        entry = after[path]
        created_by_key.setdefault((entry.entry_type, entry.hash), []).append(path)

    consumed: set[str] = set()
    for key, old_paths in deleted_by_key.items():
        new_paths = created_by_key.get(key, [])
        if len(old_paths) == 1 and len(new_paths) == 1:
            entry = after[new_paths[0]]
            if entry.size > 0:
                events.append(
                    {
                        "type": "file.renamed",
                        "path": new_paths[0],
                        "path_before": old_paths[0],
                        "hash": entry.hash,
                        "size": entry.size,
                    }
                )
                consumed.add(old_paths[0])
                consumed.add(new_paths[0])

    for path in created - consumed:
        entry = after[path]
        record = {
            "type": "file.created",
            "path": path,
            "entry_type": entry.entry_type,
            "hash": entry.hash,
            "size": entry.size,
        }
        if entry.mode is not None:
            record["mode"] = entry.mode
        if entry.entry_type == "symlink" and entry.target is not None:
            record["target"] = entry.target
        events.append(record)

    for path in deleted - consumed:
        entry = before[path]
        events.append(
            {
                "type": "file.deleted",
                "path": path,
                "entry_type": entry.entry_type,
                "hash_before": entry.hash,
            }
        )

    for path in set(before) & set(after):
        old, new = before[path], after[path]
        if old.entry_type != new.entry_type or old.hash != new.hash:
            # Absorption rule (§7): content and permission deltas collapse into
            # one file.modified event; a type change is also file.modified.
            record = {
                "type": "file.modified",
                "path": path,
                "entry_type_before": old.entry_type,
                "entry_type_after": new.entry_type,
                "hash_before": old.hash,
                "hash_after": new.hash,
                "size_before": old.size,
                "size_after": new.size,
            }
            if old.mode is not None:
                record["mode_before"] = old.mode
            if new.mode is not None:
                record["mode_after"] = new.mode
            events.append(record)
        elif old.mode is not None and new.mode is not None and old.mode != new.mode:
            events.append(
                {
                    "type": "file.metadata_modified",
                    "path": path,
                    "mode_before": old.mode,
                    "mode_after": new.mode,
                }
            )

    # Deterministic order (§9): effect type, relative path, previous path.
    events.sort(key=lambda event: (event["type"], event["path"], event.get("path_before", "")))
    return events

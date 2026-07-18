"""Net-effect computation: event types, absorption, rename inference, ordering."""

from cornerstone_cli.diff import diff_events
from cornerstone_cli.snapshot import Entry

H = {name: f"{name:0>64}" for name in ("a", "b", "c", "d")}


def file_entry(content_hash, size=10, mode="0644"):
    return Entry("file", content_hash, size, mode)


def test_created_deleted_modified_metadata():
    before = {
        "gone.txt": file_entry(H["a"]),
        "edited.txt": file_entry(H["b"]),
        "chmodded.txt": file_entry(H["c"], mode="0644"),
    }
    after = {
        "new.txt": file_entry(H["d"]),
        "edited.txt": file_entry(H["c"]),
        "chmodded.txt": file_entry(H["c"], mode="0600"),
    }
    events = diff_events(before, after)
    assert [(e["type"], e["path"]) for e in events] == [
        ("file.created", "new.txt"),
        ("file.deleted", "gone.txt"),
        ("file.metadata_modified", "chmodded.txt"),
        ("file.modified", "edited.txt"),
    ]


def test_absorption_content_and_mode_in_one_event():
    before = {"f": file_entry(H["a"], mode="0644")}
    after = {"f": file_entry(H["b"], mode="0755")}
    events = diff_events(before, after)
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "file.modified"
    assert event["mode_before"] == "0644"
    assert event["mode_after"] == "0755"


def test_type_change_is_modified_with_both_types():
    before = {"f": file_entry(H["a"])}
    after = {"f": Entry("symlink", H["b"], 5, "0755", "a.txt")}
    events = diff_events(before, after)
    assert events[0]["type"] == "file.modified"
    assert events[0]["entry_type_before"] == "file"
    assert events[0]["entry_type_after"] == "symlink"


def test_rename_inferred_on_unique_hash():
    before = {"notes.txt": file_entry(H["a"], size=2048)}
    after = {"docs/notes.txt": file_entry(H["a"], size=2048)}
    events = diff_events(before, after)
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "file.renamed"
    assert event["path_before"] == "notes.txt"
    assert event["path"] == "docs/notes.txt"


def test_empty_files_are_never_paired_as_renames():
    empty = file_entry(H["a"], size=0)
    events = diff_events({"old.empty": empty}, {"new.empty": empty})
    assert [e["type"] for e in events] == ["file.created", "file.deleted"]


def test_ambiguous_hash_matches_are_not_paired():
    before = {"one.txt": file_entry(H["a"]), "two.txt": file_entry(H["a"])}
    after = {"moved.txt": file_entry(H["a"])}
    events = diff_events(before, after)
    assert sorted(e["type"] for e in events) == ["file.created", "file.deleted", "file.deleted"]


def test_each_path_appears_in_at_most_one_event():
    before = {
        "a.txt": file_entry(H["a"]),
        "b.txt": file_entry(H["b"], mode="0644"),
    }
    after = {
        "moved/a.txt": file_entry(H["a"]),
        "b.txt": file_entry(H["c"], mode="0600"),
        "c.txt": file_entry(H["d"]),
    }
    events = diff_events(before, after)
    paths = []
    for event in events:
        paths.append(event["path"])
        if "path_before" in event:
            paths.append(event["path_before"])
    assert len(paths) == len(set(paths))


def test_ordering_is_deterministic():
    before = {"z.txt": file_entry(H["a"]), "a.txt": file_entry(H["b"])}
    after = {"m.txt": file_entry(H["c"]), "b.txt": file_entry(H["d"])}
    first = diff_events(before, after)
    second = diff_events(dict(reversed(before.items())), dict(reversed(after.items())))
    assert first == second
    assert first == sorted(first, key=lambda e: (e["type"], e["path"], e.get("path_before", "")))

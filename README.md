# Cornerstone

Cornerstone runs a command inside a controlled workspace, compares the state of the workspace before and after the execution, and records the detected net effects in a hash-chained ledger.

Cornerstone does not necessarily reconstruct the individual actions performed by the process, nor their chronological order.

**Spec v0.1 — status: implemented.**

---

## The question it answers

v0.1 gives a reliable answer to:

> What net differences are observable in the workspace between the start and the end of this execution?

It cannot answer with certainty:

* in what order the changes happened;
* which specific process produced each change;
* which intermediate operations cancelled out before the final snapshot.

## Where it sits

Cornerstone is the third layer of a three-part design:

| Layer | Question | Requires the agent's cooperation |
|---|---|---|
| **Mandate** | What was the agent allowed to do? | yes |
| **Pentimento** | What was recorded while it acted? | yes |
| **Cornerstone** | What actually changed afterwards? | **no** |

Cornerstone is the only layer that works without any collaboration from the observed process.

## Install

Requires Python ≥ 3.11 on a POSIX system.

```sh
pip install cornerstone-cli
```

This installs the `stone` command.

## Quick start

```sh
cd my-workspace
stone run --actor codex -- codex exec "Reorganize these files"
stone show latest
stone verify latest
```

Example `stone show` output:

```
Session: 01K0EJ3V9CJ0Q6ZM3S1R8T5W2X
Declared actor: codex
Command: codex exec 'Reorganize these files'
Outcome: success
Duration: 18.4 seconds

Net effects detected:
CREATED      docs/architecture.md
MODIFIED     README.md
RENAMED      notes.txt → docs/notes.txt
DELETED      draft.tmp

The order shown does not necessarily represent
the chronological order of the operations.
```

The final warning is part of the output, by design.

## How a session works

1. workspace validation;
2. session id creation (a ULID, lexicographically sortable by creation time);
3. initial snapshot;
4. command execution;
5. capture of exit code and duration;
6. final snapshot;
7. snapshot comparison;
8. event generation for the net effects;
9. deterministic event ordering (effect type, relative path, previous path);
10. ledger write;
11. hash-chain verification;
12. summary presentation.

Validation refuses a `.stone` that is a symbolic link: the lock, the ledger and the index must not be writable through a path that leads outside the workspace.

The command runs in its own process group, and the session waits for the **entire group** to exit before taking the final snapshot: descendants that outlive the immediate child are still inside the observation window, and interruption signals are forwarded to the whole group. Two honest limits remain: a process that detaches from the group (`setsid`, double fork) escapes observation, and a group member that never exits keeps the session open until interrupted. The recorded exit code is the immediate child's; the duration covers the whole group.

An event's position in the ledger reflects the order in which Cornerstone serialized it, not necessarily the order in which the change happened. An event timestamp records when the effect was detected, not when the change occurred. All timestamps are ISO 8601 in UTC.

## What a snapshot observes

For every file: content (SHA-256), size, entry type (`file` or `symlink`), POSIX permissions when available, and the textual target for symbolic links.

* **Symbolic links are never followed.** A symlink is recorded with its target text; its hash is the SHA-256 of that text. Cornerstone never scans the target's content, so it cannot accidentally leave the workspace or loop through filesystem cycles. The snapshot walks the tree with directory file descriptors (`openat`-style): every directory is opened with `O_DIRECTORY | O_NOFOLLOW` and every file with `O_NOFOLLOW`, both relative to the parent directory's descriptor, and file metadata comes from `fstat` on the same descriptor that was hashed. Swapping **any** path component for a symlink while the snapshot runs cannot lead outside the workspace (no TOCTOU escape, on files or on ancestor directories).
* **Directories are not observed elements**: they exist implicitly through file paths. Creating or deleting an empty directory is an invisible effect.
* Owner, group, extended attributes, ACLs and OS-specific metadata are not compared in v0.1.
* No workspace size limit: snapshot cost grows with the size of the observed workspace.

## Event types

| Type | Condition |
|---|---|
| `file.created` | path absent in the initial snapshot, present in the final one |
| `file.deleted` | path present initially, absent at the end |
| `file.modified` | path present in both, different content |
| `file.metadata_modified` | path present in both, identical content, different permissions |
| `file.renamed` | rename inferred (see below) |

**Absorption rule.** If both the content and the permissions of a file changed in the same session, the ledger emits a single `file.modified` event carrying both deltas. `file.metadata_modified` is reserved for permission-only changes.

**Type change.** If the element type at a path changes (file ↔ symlink), the effect is `file.modified`, with both types in the payload.

**Invariant.** Every path is the subject of at most one event per session. An inferred rename occupies a single event referencing two paths.

### Rename inference

A rename is inferred only when all of the following hold: a path disappears, a new path appears, the two entries share the same hash, the correspondence is unique, the file is larger than zero bytes, and the permissions match (when available on both sides).

Empty files are never paired automatically. When several deleted or created files share the same hash, Cornerstone does not pick a pair arbitrarily: it records distinct `file.deleted` and `file.created` events. A rename whose permissions also changed is likewise recorded as distinct events: the `file.renamed` payload has no permission fields, and pairing would silently drop that delta. An inferred rename represents content equivalence, not verified identity continuity.

## The ledger

Path: `.stone/sessions/<session_id>/events.jsonl`

Format: JSON Lines. Each line is a canonical JSON record — keys sorted alphabetically, UTF-8, no superfluous whitespace, newline-terminated. The first record is `session.started`, the last is `session.finished`, and every line in between is one net effect.

```json
{"hash":"9f86d0…","path":"docs/notes.txt","path_before":"notes.txt","prev":"3fdba3…","size":2048,"ts":"2026-07-19T09:12:44Z","type":"file.renamed"}
```

| Record | Fields |
|---|---|
| `session.started` | `actor`, `command` (argv as given), `id`, `prev`, `spec` (`"0.1"`), `ts`, `type` |
| `file.created` | `entry_type`, `hash`, `mode`, `path`, `prev`, `size`, `target` (symlinks only), `ts`, `type` |
| `file.deleted` | `entry_type`, `hash_before`, `path`, `prev`, `ts`, `type` |
| `file.modified` | `entry_type_after`, `entry_type_before`, `hash_after`, `hash_before`, `mode_after`, `mode_before`, `path`, `prev`, `size_after`, `size_before`, `ts`, `type` |
| `file.metadata_modified` | `mode_after`, `mode_before`, `path`, `prev`, `ts`, `type` |
| `file.renamed` | `hash`, `path`, `path_before`, `prev`, `size`, `ts`, `type` |
| `session.finished` | `duration_s`, `exit_code`, `outcome`, `prev`, `ts`, `type` |

Absent optional fields are omitted, never serialized as `null`.

**Hash chain.** Every record carries a `prev` field: the SHA-256 hex digest of the previous line's bytes, newline excluded. The first record uses 64 zeros. `stone verify` recomputes the whole chain **and validates the structure** — `session.started` first, `session.finished` last, known event types with their required fields, at most one event per path, and a ledger that matches the session directory it sits in. Any discrepancy, including truncation, makes the ledger non-intact. `stone show` runs the same verification before rendering and refuses a non-intact ledger.

**Integrity limits.** The chain makes accidental corruption, truncation and naive edits evident. It is **not** proof against an adversary who can rewrite the whole file: the chain carries no signature and no external anchor in v0.1, so it can be recomputed by anyone with write access. If you need stronger guarantees, archive the final line's hash — or the whole ledger — outside the workspace, at a time you trust.

**Paths.** The ledger contains only paths relative to the workspace root; the absolute workspace path is never written. The command line is recorded exactly as given: if it contains absolute paths or secrets, they enter the ledger.

**Index.** `.stone/index.jsonl` is append-only, one line per session (`id`, `started_at`, `outcome`). It backs `latest` resolution and `stone list`.

## CLI

```
stone run [--actor NAME] [--capture-output] -- COMMAND [ARG…]
stone show   <session_id | latest>
stone verify <session_id | latest>
stone list
```

* `--actor` sets the declared actor; default: `undeclared`. It is a declaration provided by the user, not a verified attribution.
* `latest` resolves to the session with the most recent start time among the correctly indexed ones; with no sessions, the command fails with an explicit message and a non-zero exit code.
* `stone list` shows the indexed sessions, most recent first (id, start, actor, outcome).
* One session per workspace at a time: `.stone/lock` (session id and PID) blocks concurrent runs; a leftover lock from a crashed process must be removed manually.
* `stone run` propagates the exit code of the observed command, so it can be dropped into pipelines. Internal errors use dedicated codes:

| Code | Meaning |
|---|---|
| `96` | active lock |
| `97` | invalid workspace or configuration |
| `98` | incomplete snapshot |
| `99` | internal error |

## Session outcomes

* **success** — the command exited with code 0;
* **failed** — it exited with a non-zero code;
* **interrupted** — it was terminated by a signal or by the user;
* **incomplete** — the final snapshot could not be completed.

A `success` outcome only reports the command's exit code. It does not guarantee that the task was performed correctly.

On interruption, Cornerstone forwards the signal to the child process, waits for it to terminate, and still attempts the final snapshot and the ledger write. On `incomplete`, the ledger contains only `session.started` and `session.finished`: `stone verify` still validates the chain and `stone show` declares the outcome.

If Cornerstone itself dies before the ledger write, the ledger may not exist. **The absence of a ledger does not mean the absence of effects.**

## Captured output

By default the child's stdout and stderr are forwarded to the terminal and not preserved. With `--capture-output` they are also saved as `stdout.log` and `stderr.log` next to the ledger, created with owner-only permissions, truncated beyond a configurable threshold (default 10 MiB per file, keeping the tail, where errors typically appear). Output files are never part of the JSONL ledger and may contain secrets printed by the process: Cornerstone performs no secret redaction.

## Configuration

`.stone/config.toml`, optional:

```toml
# additional ignored path prefixes, relative to the root, no globs
ignore = ["data/cache/", "tmp/"]

# truncation threshold for stdout.log and stderr.log, in bytes
capture_max_bytes = 10485760
```

Default ignored prefixes: `.stone/`, `.git/`, `node_modules/`, `__pycache__/`, `.venv/`, `venv/`. Ignored paths are **unobserved zones**: changes inside them produce no events — a Git commit may produce no visible effect at all. Cornerstone must not be treated as a complete activity monitor.

## Invisible effects

Cornerstone records only the net delta between the two snapshots. Among the things it does not detect: a file created and deleted within the session; a file modified and then restored exactly; changes inside ignored paths; creation or deletion of empty directories; network activity; changes outside the workspace; any action that leaves no persistent observable difference.

## Attribution

Every change that happened in the workspace during the session is associated with the session. This association does not prove the change was produced by the observed process: concurrent writers appear in the session's ledger too. The `actor` field is a declared actor, not a verified attribution.

## Non-goals of v0.1

Listed to resist temptation, not out of forgetfulness:

* no reconstruction of individual actions or their order — no syscall tracing, no ptrace/strace;
* no continuous filesystem observation — no inotify/FSEvents watchers: v0.1 is snapshot-based, by choice;
* no network monitoring;
* no automatic secret redaction;
* no rollback or undo — that is Pentimento;
* no upfront authorization — that is Mandate;
* no cross-session comparison;
* no daemon, no GUI, no cloud, no multi-machine sync;
* no attribution guarantee.

Any addition to this perimeter requires a spec version bump, not a decision made while writing code.

## Packaging

* Installed command: `stone`
* PyPI package: `cornerstone-cli`
* Internal Python module: `cornerstone_cli`

The distinction avoids conflicts with the existing `cornerstone` and `stone` packages on PyPI.

## The promise

An agent can claim it performed a task correctly.

Cornerstone does not record what the agent claims to have done, and does not pretend to reconstruct its every action.

It records the net differences that are observable in the workspace after the execution.

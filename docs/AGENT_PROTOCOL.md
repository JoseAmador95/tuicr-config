# nvim-review agent protocol

`tuicr-round` creates a reviewable Git snapshot without modifying the selected
checkout. Global agent instructions may point directly at this document and the
two canonical schemas in `schemas/`.

## Lifecycle

1. Run `tuicr-round start --repo /absolute/repo`. Keep the returned UUIDs. A
   dirty initialized submodule receives its own UUID and `repo_root`.
2. A human runs `tuicr-round open --round UUID`, or uses
   `tuicr-round start --repo /absolute/repo --open` for the combined workflow.
   It creates or attaches to one private tmux session. A live terminal executes
   the tmux client without printing automation JSON into the TUI; a
   noninteractive invocation returns the JSON preflight and attach argv without
   attaching. A normal tuicr exit such as `q` closes that private session and
   releases every attached surface. A nonzero exit retains the dead pane and
   its output; a later `open` fails visibly with the exact attach command instead
   of silently replacing it.
3. Poll `tuicr-round status --round UUID` until `session` contains the exact
   slug returned by `tuicr review list`.
4. Add findings with `add`; reply with `respond`. Agents and verifiers must pass
   their exact display name with `--author`.
5. Read explicit acceptances with `tuicr-round accepted --round UUID`. Never
   type into, send keys to, or otherwise drive the human's TUI pane.
6. The human closes the round manually. Resolved threads close immediately.
   When open, discuss, malformed, or unmanaged comments remain, the first
   `close` returns a token bound to the current public comment JSON; retry with
   `--confirm TOKEN`. A changed comment set makes the token stale.

Every noninteractive result is one compact JSON object. Selection ambiguity is
an error: use `--round UUID` when more than one open round exists for a repo.

## Comments

The first content line is exactly:

```text
@nvim-review {"version":1,"role":"agent","author":"Exact Agent Name","severity":"blocker","status":"open","reply_to":null}
```

The JSON object must conform to
[`comment-header.schema.json`](../schemas/comment-header.schema.json). The
human-readable message starts on the next line. Root findings have a null
`reply_to`; responses name the tuicr comment `id` they answer. Severities map to
tuicr types as follows: `blocker` to `issue`, `warning` to `suggestion`, and
`nit` to `pedantic`.

Targets are repository-relative POSIX paths. Positions are one-based. A file
without a position creates a file comment; omit the path for a review comment.

## Batch result interchange

Agent output exchanged outside the launcher uses
[`agent-results.schema.json`](../schemas/agent-results.schema.json). Producers
must serialize UTF-8 JSON no larger than 1 MiB and at most 2,000 `items`. Each
item supplies a repo-relative `path`, one-based `start` and `end` positions,
protocol severity, message, and source. The `repo_root` must exactly equal the
canonical repository root returned by `start`; `run_id` is the producer's
stable run identifier.

The schema is an interchange contract, not an instruction to mutate tuicr's
native session files. Comments enter tuicr only through `tuicr review add`.

## Safety boundary

The launcher requires exactly tuicr 0.21.0. It uses a round-private `HOME`, the
real `XDG_CONFIG_HOME`, and an external Git directory whose object alternates
point read-only at the real common object store. The real repository's index,
refs, object store, and worktree are never update targets. Sparse/split indexes,
conflicts, filters/LFS, and partial/promisor clones fail closed.

Dirty content over 50 MiB is reported as a warning; content over 500 MiB is
rejected before worktree blobs are hashed. Dirty captures preserve staged `S0`
and final worktree `B0` as separate synthetic commits. Dirty rounds review this
frozen range without `-w`; untracked files are already part of `B0`. The pinned
`initial_commit_selection = "oldest"` opens `S0` first and tuicr's inline commit
selector exposes `B0`. If `B0` restores the original `HEAD` tree, a private
transport base is added as B0's second-parent ancestry. It exists only to pass
tuicr 0.21's aggregate-diff preflight: the reviewed `transport-base..B0` range
still enumerates exactly `S0` and `B0`, whose first-parent diffs are the exact
staged and unstaged layers. No transport path is present in either reviewed
commit or in the real checkout.

A clean round reuses the exact `HEAD` commit and tree, then reviews that commit
against its frozen first parent without `-w`. For a repository whose `HEAD` is
the root commit, the launcher creates an empty private base commit so the root
diff remains reviewable; the real checkout and object store are still untouched.

A parent plus dirty initialized submodules is one all-or-nothing capture: if
any child fails its guards, every round directory created by that `start` is
removed.

`clean` only prunes closed, inactive rounds older than 30 days, including their
private tuicr sessions.

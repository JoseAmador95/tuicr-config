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
   of silently replacing it. Newly created sessions show the full
   `TUICR-ROUND:UUID | y: copy review + UUID` marker in their private tmux status
   line. An already-running session is never restarted or reconfigured.
3. Poll `tuicr-round status --round UUID` until `session` contains the exact
   slug returned by `tuicr review list`. `status`, `start`, and `comments`
   include the stable `TUICR-ROUND:UUID` handoff marker and canonical Codex
   prompt. In a newly opened round, tuicr's `y`, `Y`, and `:clip` copies include
   that prompt unless the copied value is an HTTP or HTTPS URL. The URL path is
   byte-for-byte unchanged. `tuicr-round handoff --round UUID --copy` remains a
   fallback that copies only the prompt with `pbcopy`.
   `tuicr-round status --repo ROOT --all` discovers every open round for the
   normalized repository and succeeds with an empty `rounds` list when none
   exist. Each entry is the same status object returned for an exact UUID.
4. Read review input only with `tuicr-round comments --round UUID`. It returns
   the digest of the unmodified public JSON plus normalized comments and
   threads. Its `snapshot` object contains the branch, HEAD, S0/B0 trees and
   commits, review base, and clean flag frozen when the round started. This is
   review context, not proof that the current checkout still matches that
   snapshot. Normal comments entered by a person in the TUI are native
   `human/open` thread roots. A comment beginning with `@nvim-review` must have
   a valid protocol header; otherwise it is reported as malformed rather than
   reinterpreted as native.
5. Add findings with `add`; reply with `respond`. Agents publish their PLAN and
   final RESULT as replies so both remain visible beside the human comment in
   TUICR. Agents and verifiers must pass their exact agent/model display name
   with `--author`; do not abbreviate or guess it. Replies to native and
   protocol comments are both supported, and an omitted target inherits the
   referenced comment's file/range and old/new side.
6. Read explicit human or verifier acceptances with
   `tuicr-round accepted --round UUID`. Never type into, send keys to, or
   otherwise drive the human's TUI pane. All comment reads and writes go
   through the launcher; never read or edit TUICR session/state files.
7. The human closes the round manually. Resolved threads close immediately.
   When open, discuss, malformed, or unmanaged comments remain, the first
   `close` returns a token bound to the current public comment JSON; retry with
   `--confirm TOKEN`. A changed comment set makes the token stale.

Every noninteractive result is one compact JSON object. Selection ambiguity is
an error except for explicit `status --repo ROOT --all` discovery; otherwise use
`--round UUID` when more than one open round exists for a repo. `--all` cannot be
combined with `--round`.

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

`add` and `respond` accept an optional `--comment-type` of `issue`,
`suggestion`, `rationale`, `question`, `pedantic`, or `praise`. The required
`--severity` remains in the unchanged protocol header and must match the native
mapping below. Omitting `--comment-type` retains the default severity mapping
above.

Native Neovim reviews pass a stable `--delivery-key` for each local comment.
The launcher returns the existing TUICR receipt when the same comment is retried
after a local save failure, and rejects reuse of that key for different content.
Callers that do not need retry-safe delivery may omit it.

Targets are repository-relative POSIX paths. Positions are one-based. A file
without a position creates a file comment; omit the path for a review comment.
Roots default to the new side. `respond --reply-to ID` inherits the referenced
target when no target flags are supplied; `--path`, `--start`, `--end`, and
`--side old|new` explicitly override it.

The `comments` command exposes each usable item with its raw TUICR object,
human-readable message, actual or synthetic protocol header, `native` or
`protocol` origin, original `comment_type`, and a normalized target containing
path, start, end, side, and display location. The native taxonomy and severity
mapping are:

- `issue` maps to `blocker`.
- `suggestion`, `rationale`, and `question` map to `warning`.
- `pedantic` and `praise` map to `nit`.

`rationale` requests an explanation of the current choice, comparison with a
proposed alternative, and adoption of the better option. `question` requests
missing information or clarification without implying a change. For historical
compatibility, a missing, empty, or unknown native type remains a valid
`human/open` thread root and maps to `warning`. A native author comes from
`username` or `author`, with `Human reviewer` as the fallback.

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

The launcher verifies that tuicr is available by successfully running
`tuicr --version`; it does not require a particular version string. It uses a
round-private `HOME`, the real `XDG_CONFIG_HOME`, and an external Git directory
whose object alternates point read-only at the real common object store. Before
starting a new TUI, it creates a round-private `pbcopy` wrapper and prepends only
that wrapper's directory to the TUI process `PATH`. The wrapper delegates to the
resolved real `pbcopy`, preserves input bytes, and adds the round handoff once to
non-URL copies. Existing active rounds keep their original environment. The real
repository's index, refs, object store, and worktree are never update targets.
Sparse/split indexes, conflicts, filters/LFS, and partial/promisor clones fail
closed.

Dirty content over 50 MiB is reported as a warning; content over 500 MiB is
rejected before worktree blobs are hashed. Dirty captures preserve staged `S0`
and final worktree `B0` as separate synthetic commits. Dirty rounds review this
frozen range without `-w`; untracked files are already part of `B0`. The pinned
`initial_commit_selection = "oldest"` opens `S0` first and tuicr's inline commit
selector exposes `B0`. If `B0` restores the original `HEAD` tree, a private
transport base is added as B0's second-parent ancestry. It exists only to pass
tuicr's aggregate-diff preflight: the reviewed `transport-base..B0` range
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

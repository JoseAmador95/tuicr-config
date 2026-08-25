# tuicr local configuration

This repository pins the local tuicr behavior and provides `tuicr-round`, a
Python 3.9 standard-library launcher for isolated synthetic review rounds.

```sh
./tuicr-round start --repo /absolute/path/to/repo
./tuicr-round start --repo /absolute/path/to/repo --open
./tuicr-round open --round UUID
./tuicr-round status --round UUID
./tuicr-round status --repo /absolute/path/to/repo --all
./tuicr-round comments --round UUID
./tuicr-round handoff --round UUID --copy
```

`status --repo ROOT --all` returns every open round for the normalized repository;
an empty `rounds` list is a successful result. `add` and `respond` also accept an
optional `--comment-type` using the configured native taxonomy and an optional
stable `--delivery-key` for safe receipt recovery. Their required
`--severity` must match the protocol mapping, while omitting the type preserves
the historical issue/suggestion/pedantic defaults.

In a newly opened round, `y` or `:clip` copies the normal tuicr review together
with its canonical `TUICR-ROUND:UUID` handoff instruction. The private tmux
status line shows the same marker; `handoff --copy` remains available as a
fallback.

See [the agent protocol](docs/AGENT_PROTOCOL.md) for the complete lifecycle,
native and structured comment handling, visible PLAN/RESULT replies, safety
properties, and canonical schema links.

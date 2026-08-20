# tuicr local configuration

This repository pins the local tuicr behavior and provides `tuicr-round`, a
Python 3.9 standard-library launcher for isolated synthetic review rounds.

```sh
./tuicr-round start --repo /absolute/path/to/repo
./tuicr-round start --repo /absolute/path/to/repo --open
./tuicr-round open --round UUID
./tuicr-round status --round UUID
./tuicr-round comments --round UUID
./tuicr-round handoff --round UUID --copy
```

See [the agent protocol](docs/AGENT_PROTOCOL.md) for the complete lifecycle,
native and structured comment handling, visible PLAN/RESULT replies, safety
properties, and canonical schema links.

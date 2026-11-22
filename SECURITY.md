# Security Policy

## Design

[docs/threat-model.md](docs/threat-model.md) describes how this version handles credentials, stale or replayed plans, write scope, and agent misuse, and which of those risks the code does not close. [ARCHITECTURE.md](ARCHITECTURE.md) is the plan, apply, and journal sequence.

## Reporting a vulnerability

Please report vulnerabilities privately using GitHub's
[private vulnerability reporting](https://github.com/jmamodeo/benchling-tools/security/advisories/new)
(Security Advisories). Do not open public issues for security problems. Expect an initial response within a few days.

## Handling credentials

- Never commit tokens, client IDs, client secrets, or real tenant data to this repository, issues, or pull requests.
- Run `benchling-tools auth login` in your own terminal, or non-interactively with `--tenant-url`, `--client-id`, and `--secret-stdin`. The client secret is not accepted as an argument and must not be passed through an agent, a chat, or an MCP client config. The tenant URL and client ID may be arguments; the secret may not.
- The tenant URL and client ID are written to `environments.json` (mode 0600) under the state directory. The client secret is stored in the OS keychain (service `benchling-tools`, username = environment name). Access tokens stay in process memory.
- There is no plaintext fallback. If the OS keychain is unavailable, login fails and nothing is saved. On Linux the keychain is the Secret Service on the session bus. Later shells and `benchling-tools mcp serve` must inherit the same `DBUS_SESSION_BUS_ADDRESS`. The MCP SDK passes only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER` unless the client config sets `env`. Do not capture `gnome-keyring-daemon --unlock` with `$(...)`; that hangs because the daemon does not exit.
- Error envelopes and execution journals are scrubbed of client secrets and access tokens.
- If a secret is exposed, rotate it in Benchling and run `benchling-tools auth login` again for that environment.

## Supported versions

Only the latest released version receives security fixes.

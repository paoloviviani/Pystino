# Agent machines: distributing and installing `pystino-agent`

A machine that runs coding agents for the chat's `/code` panel needs one
binary, `pystino-agent` (`deploy/agent/`), and `opencode`. What the binary
does and why is [Coding agents](coding-agents.md); the wire protocol is
`deploy/agent/PROTOCOL.md`; the panel is the chat repository's
`docs/code-panel.md` and `docs/agent-machines.md`. This page is the operator's
part: building it, handing it out, installing it, and taking a machine away.

The stack side is `pystino init --agents` (see [Deployment](deployment.md)).

## Building the binaries

```bash
deploy/agent/packaging/build-dist.sh ~/pystino-agent-dist
```

It needs Go 1.24+ and writes the files below. The binaries are static (CGO
off), so each one runs on any machine of its OS and architecture.

| File | What |
|---|---|
| `pystino-agent-linux-amd64`, `-linux-arm64`, `-darwin-amd64`, `-darwin-arm64` | the binary |
| `REVISION` | the Pystino commit it was built from (`-dirty` if `deploy/agent` had local changes) |
| `SHA256SUMS` | for `sha256sum -c SHA256SUMS` (macOS: `shasum -a 256 -c SHA256SUMS`) |
| `pystino-agent.service`, `org.pystino.agent.plist` | the user-service files below, from `deploy/agent/packaging/` |

**Where people get them.** There is no published release yet. Until there is,
the directory is the release: copy it somewhere your users can fetch from (an
internal file share, `scp` from the build host), and give them the checksum
file with it. Anyone with a checkout and Go can instead run
`go build -o pystino-agent .` in `deploy/agent/`.

## Installing on a machine (Linux or macOS)

**Prerequisite:** `opencode` on the PATH, either `npm i -g opencode-ai` or
`curl -fsSL https://opencode.ai/install | bash`. `pystino-agent run` supervises
`opencode serve`, and it is the only runtime dependency.

```sh
install -d ~/.local/bin
install -m 0755 pystino-agent-darwin-arm64 ~/.local/bin/pystino-agent   # the file for this OS/arch
xattr -d com.apple.quarantine ~/.local/bin/pystino-agent 2>/dev/null || true   # macOS only

~/.local/bin/pystino-agent enroll \
  --issuer https://llm.example.org/authelia \
  --gateway https://llm.example.org \
  --cerea https://llm.example.org/chat \
  --output ~/.config/opencode/opencode.json
~/.local/bin/pystino-agent run
```

`enroll` signs the person in, picks their billing group (it asks when there
are several), and writes two files: the opencode config at `--output`, and a
refresh credential (mode 0600, default `<config-dir>/opencode/pystino-credentials.json`,
where `<config-dir>` is `~/.config` on Linux and `~/Library/Application Support`
on macOS). `run` then supervises opencode and dials out to the chat. Open
`/chat/code`, go to **Agents**, and the machine is listed as pending until
someone confirms it (**Confirm this machine**).

| Flag | When |
|---|---|
| `--cerea …/chat` | always include `/chat` when the chat is served there (the Pystino stack). The machine dials `<cerea>/api/v2/code/machine`, and without the base path it reaches the gateway instead |
| `--output PATH` | the file is **replaced whole**. `enroll` asks before replacing an existing one, and `--yes` skips the question. If you keep your own opencode config, point `--output` somewhere else and pass `run --opencode-config PATH` |
| `--allow-free-models` | also offer models from providers other than the gateway's. By default only `pystino/*` models are listed, so spend always lands in the account the machine enrolled under |
| `--device` | force the device flow, which prints a URL and a code to open on any other device (the bundled Authelia's `opencode-enrollment` client allows it). Without a flag, `enroll` picks the loopback sign-in in the local browser when there is a display, and the device flow when there is none. `--loopback` forces the browser |
| `--allow-auto-accept`, `--workspace-root PATH` | the machine's own vetoes, fixed at enrol time (PROTOCOL.md §4) |

If `enroll` warns that model discovery failed, the gateway offered no model
yet (no provider configured, or none granted to the person's group). It then
writes a placeholder models map. Enrol again once a model is available.

## Keeping it running

**Linux**, as a systemd user unit:

```sh
install -D -m 0644 pystino-agent.service ~/.config/systemd/user/pystino-agent.service
systemctl --user daemon-reload && systemctl --user enable --now pystino-agent
loginctl enable-linger "$USER"        # keep it up while you are logged out
journalctl --user -u pystino-agent -f
```

**macOS**, as a LaunchAgent:

```sh
sed "s/USER/$USER/g" org.pystino.agent.plist > ~/Library/LaunchAgents/org.pystino.agent.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.pystino.agent.plist
tail -f ~/Library/Logs/pystino-agent.log
```

Both run `~/.local/bin/pystino-agent run` with a PATH that includes the usual
places opencode installs to. Edit that PATH if yours is elsewhere.

## Removing a machine

1. Revoke it in the `/code` panel. The chat tombstones the pairing and closes
   the link, the agent logs `machine revoked, not reconnecting`, and a later
   connection under the same machine id is refused.
2. Stop the service: `systemctl --user disable --now pystino-agent`, or
   `launchctl bootout gui/$(id -u)/org.pystino.agent`.
3. Delete the credential file. Re-enrolling later mints a new machine id,
   which is paired afresh.

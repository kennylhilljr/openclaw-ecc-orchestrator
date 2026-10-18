# launchd agents (macOS)

Two optional user agents run the orchestrator's background jobs on the
operator's Mac. Both are templates: the install commands fill in the
repository path, home directory and user name, so nothing personal is
committed. Command line options are written here with their usual two
leading hyphens because these are shell commands to copy.

| Agent | Script | When | What it does |
| :- | :- | :- | :- |
| `local.openclaw-ecc-orchestrator.inbox-processor` | `bin/inbox-processor` | at login, kept running | Applies approval decisions the OpenClaw plugin writes into the decision inbox, through the same checks as `approvals approve` (`approvals process-inbox --watch`). Needs no API keys. |
| `local.openclaw-ecc-orchestrator.recertify` | `bin/recertify-weekly` | Sundays 09:00 | Certifies claude, codex, gemini, groq and kimi (certifications last 7 days), saves results under `.orchestration/runtime/cert-weekly-<stamp>/` and `cert-latest.json`, and shows a macOS notification with the outcome. Reads provider keys from the Keychain through `bin/with-keychain-secrets`. |

Logs go to `~/Library/Logs/openclaw-ecc-orchestrator/`.

## Before installing

Check the recertification setup without any model calls:

```sh
bin/recertify-weekly check
```

It reports the Node, Python, Claude Code and Codex it will use and whether
each Keychain key is present. The script puts nvm's default Node first on its
own PATH, so an older Claude Code elsewhere on the login PATH is not used.

## Runtime copy outside Documents

macOS privacy protection stops launchd jobs from reading anything under
`~/Documents` (`/bin/sh: ...: Operation not permitted` in the log). Rather
than granting the shell Full Disk Access, the jobs run from a git worktree of
this repository outside Documents. It needs no extra permissions and stays on
the commit you choose:

```sh
mkdir -p ~/.local/share/openclaw-ecc-orchestrator
git -C /path/to/openclaw-ecc-orchestrator worktree add --detach ~/.local/share/openclaw-ecc-orchestrator/runtime HEAD
```

After new commits, move the jobs to them (the inbox processor restarts on the
next relaunch; `kickstart -k` does it now):

```sh
git -C ~/.local/share/openclaw-ecc-orchestrator/runtime checkout --detach main
launchctl kickstart -k "gui/$(id -u)/local.openclaw-ecc-orchestrator.inbox-processor"
```

## Install

From the runtime copy. The loop is safe to run again: it unloads a job that
is already loaded before loading it. Pasted into zsh, avoid `#` comment lines
(interactive zsh runs them as commands).

```sh
cd ~/.local/share/openclaw-ecc-orchestrator/runtime
mkdir -p ~/Library/Logs/openclaw-ecc-orchestrator ~/Library/LaunchAgents
for job in inbox-processor recertify; do
  launchctl bootout "gui/$(id -u)/local.openclaw-ecc-orchestrator.$job" 2>/dev/null
  sed -e "s#__REPO__#$PWD#g" -e "s#__HOME__#$HOME#g" -e "s#__USER__#$USER#g" \
    "integrations/launchd/$job.plist.template" \
    > "$HOME/Library/LaunchAgents/local.openclaw-ecc-orchestrator.$job.plist"
  plutil -lint "$HOME/Library/LaunchAgents/local.openclaw-ecc-orchestrator.$job.plist"
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.openclaw-ecc-orchestrator.$job.plist"
done
```

The inbox processor starts immediately. To run the recertification once now
instead of waiting for Sunday (one to three minutes; uses a little of the
Claude and Codex plans):

```sh
launchctl kickstart -k "gui/$(id -u)/local.openclaw-ecc-orchestrator.recertify"
```

`kickstart -k` stops a run that is already in progress, so start it once and
wait for the notification.

## Check

```sh
launchctl print "gui/$(id -u)/local.openclaw-ecc-orchestrator.inbox-processor" | grep -E "^\s+(state|last exit code)"
launchctl print "gui/$(id -u)/local.openclaw-ecc-orchestrator.recertify" | grep -E "^\s+(state|last exit code)"
tail -n 5 ~/Library/Logs/openclaw-ecc-orchestrator/inbox-processor.log
tail -n 3 ~/Library/Logs/openclaw-ecc-orchestrator/recertify.log
```

The inbox processor should be `running`; a finished recertification shows
`last exit code = 0` and ends its log with "ECC recertification passed".
Results are in the runtime copy under `.orchestration/runtime/` (latest
summary: `cert-latest.json`). `doctor` also reports whether an inbox processor
holds its lock.

## Uninstall

```sh
for job in inbox-processor recertify; do
  launchctl bootout "gui/$(id -u)/local.openclaw-ecc-orchestrator.$job"
  rm "$HOME/Library/LaunchAgents/local.openclaw-ecc-orchestrator.$job.plist"
done
```

Decision files and certification results stay where they are.

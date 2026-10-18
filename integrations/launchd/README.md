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

## Install

From the repository root:

```sh
mkdir -p ~/Library/Logs/openclaw-ecc-orchestrator ~/Library/LaunchAgents
for job in inbox-processor recertify; do
  sed -e "s#__REPO__#$PWD#g" -e "s#__HOME__#$HOME#g" -e "s#__USER__#$USER#g" \
    "integrations/launchd/$job.plist.template" \
    > "$HOME/Library/LaunchAgents/local.openclaw-ecc-orchestrator.$job.plist"
  plutil -lint "$HOME/Library/LaunchAgents/local.openclaw-ecc-orchestrator.$job.plist"
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.openclaw-ecc-orchestrator.$job.plist"
done
```

The inbox processor starts immediately. To run the recertification once now
instead of waiting for Sunday (about 3 minutes; uses a little of the Claude
and Codex plans):

```sh
launchctl kickstart -k "gui/$(id -u)/local.openclaw-ecc-orchestrator.recertify"
```

## Check

```sh
launchctl print "gui/$(id -u)/local.openclaw-ecc-orchestrator.inbox-processor" | grep -E "state|last exit"
tail -n 5 ~/Library/Logs/openclaw-ecc-orchestrator/inbox-processor.log
tail -n 20 ~/Library/Logs/openclaw-ecc-orchestrator/recertify.log
```

`doctor` also reports whether an inbox processor holds its lock.

If a log shows `Operation not permitted` for a path under `~/Documents`, macOS
privacy protection is blocking the background job from the repository folder.
Either grant the shell that launchd runs (`/bin/sh`) access under System
Settings, Privacy and Security, or keep the repository outside `~/Documents`
and reinstall the agents from there.

## Uninstall

```sh
for job in inbox-processor recertify; do
  launchctl bootout "gui/$(id -u)/local.openclaw-ecc-orchestrator.$job"
  rm "$HOME/Library/LaunchAgents/local.openclaw-ecc-orchestrator.$job.plist"
done
```

Decision files and certification results stay where they are.

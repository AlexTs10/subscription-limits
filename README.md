# Subscription Limits

A small macOS menu bar app. One list shows each local Claude and Codex
subscription, its remaining limits, and its reset times. Click the **gauge
icon** in the menu bar to open the list. Hover for a text summary. Updates run
every five minutes. Escape closes the menu.

The interface uses AppKit's native `NSMenu`, section headers, progress
indicators, and standard menu commands. macOS supplies the menu background,
appearance, keyboard controls, and screen placement. Account email addresses
and exact update times are available in the section header's tooltip.
The icon starts near the clock and keeps its saved position. Hold Command
and drag the icon to move it. A local `menu-bar-status.json` file in the app's
cache folder records its own icon position for fault checks.

## Sources

- Claude Code profiles: macOS Keychain or the profile's existing credentials
  file. Profiles come from T3 settings and local `.claude*` directories.
- Claude Desktop: its existing encrypted OAuth cache, read with its existing
  Keychain storage key. This finds Personal Max even if T3 uses other accounts.
- Codex: existing `auth.json` files from local `.codex*` directories, T3
  settings, and CodexBar profile paths.
- Claude and Codex usage endpoints supply current limits. When Claude rejects
  a usage request, the installed CodexBar command-line browser reader can
  supply a fallback. Its email and organization must match a verified local
  account without ambiguity. CodexBar's menu app can stay closed.

Claude subscriptions are grouped by **account ID plus organization ID**, not
by email. Codex subscriptions use account ID plus the signed-in user ID when
available. A missing limit stays unknown. Old readings have a visible label.
Provider errors keep the previous reading and use a retry delay.
The app waits at least five minutes between updates, including menu opens
and manual refreshes. HTTP 429 means that usage checks are paused. The menu
shows the time until the next check. The app keeps the full provider retry
delay and increases the delay after repeated rate-limit responses.
Account verification is cached for up to one day for each exact access token.
A changed token must pass verification again.

The app does not switch accounts or renew credentials. If a login expires,
open its source app to renew it. Disconnected accounts, API keys, and
subscriptions without a local login cannot supply subscription limits.

Credentials stay in process memory. They are sent only to the provider that
issued them. No credential is copied to this project or to the app's cache.
The cache at `~/Library/Application Support/Subscription Limits/snapshot.json`
contains account labels, IDs, email addresses, limits, and update status.
The adjacent `identities.json` stores verified account details and one-way
token fingerprints, without access tokens or refresh tokens.
It has user-only file permissions. The browser fallback uses CodexBar's
existing local access; this app does not copy browser cookies.

## Build and check

Requires Apple Silicon, macOS 14 or later, Xcode command-line tools, and
`/usr/bin/python3`. This build is for this Mac and is signed locally.

```sh
/usr/bin/python3 -m unittest discover -s Tests -v
zsh build.sh
```

The output is `build/Subscription Limits.app`. Copy it to `/Applications`.
The source has no third-party Python packages. The optional browser fallback
uses `/opt/homebrew/bin/codexbar` or `/usr/local/bin/codexbar`.

Provider APIs and local credential formats can change. The reader reports
errors instead of substituting another account or inventing a limit.

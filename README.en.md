# QQ AI Backend

[中文](README.md) | English

A headless backend that connects the built-in AI to a personal QQ account. There is no
desktop pet, avatar, affection system, work-status label, idle-period display, or chat
web page. It does one thing: chat like a real person in QQ private chats and whitelisted
groups, and speak up on its own when the moment fits.

> Personal QQ automation is unofficial and may trigger risk controls, freezes, or bans.
> **Use a secondary account only.**

## Features

- **QQ private chat** via OneBot 11 (NapCat / SnowLuma): multiple whitelisted QQ numbers,
  text and images, auto-binds the first private sender, splits long replies, and
  deduplicates messages.
- **QQ group chat**: record every message in whitelisted groups; reply only when the bot
  is mentioned.
- **Proactive messages**: time of day, weather changes, sitting too long, long silence,
  and random small talk. What used to be desktop-pet bubbles is now sent to QQ.
- **Conversation continuity across restarts**: each private QQ and each group has its own
  history file, and the most recent messages are imported when starting a new
  conversation. History files never mix.
- **Identifiable sources**: user messages use `QQ（nickname）/private: text` or
  `group/group chat: sender QQ（nickname）: text`; the QQ or group number is the unique id.
- **Configurable persona and AI endpoint**: built-in DeepSeek Web2API or any
  OpenAI-compatible API (base_url / API key / model).
- **Computer context**: time, foreground apps, now-playing media, weather, and account
  balance are injected into the system prompt.
- **Headless**: only a system-tray icon (Settings / Quit).

## Quick start (release build)

1. Install and sign in to [NapCatQQ](https://github.com/NapNeko/NapCatQQ) or
   [SnowLuma](https://github.com/SnowLuma/SnowLuma) with a **secondary QQ account**.
2. In the NapCat WebUI, add a `WS forward server` (OneBot 11):
   - host: `127.0.0.1`
   - port: `3001`
   - message format: `array`
   - token: a random string you choose
3. Download `QQAIBackend-v*.zip` from Releases, extract it, and run `QQAIBackend.exe`.
4. Right-click the tray icon → **Settings…**:
   - **AI**: choose a preset and fill in the API key/model; keep the defaults for the
     built-in free AI.
   - **QQ**: set the WS URL to `ws://127.0.0.1:3001`, the exact same token as
     NapCat/SnowLuma, and the whitelisted QQ/group numbers (one per line or
     comma-separated). An empty group whitelist ignores all group messages.
5. Send a private message from your main account to the bot account. The first private
   sender is added to the whitelist automatically.
   Groups are never auto-bound; enter their group numbers manually.
6. Chat normally. Proactive messages go only to whitelisted QQ numbers, never to groups.

### NapCat notes

- Depending on the NapCat version, the option may be called `WS forward server` or
  `WebSocket server`; choose the mode where **NapCat is the server and this app connects
  as a client**.
- **The token must match exactly.** With a wrong token NapCat accepts the connection,
  then returns `{"retcode":1403,"message":"token verification failed"}` and disconnects.
  The log shows `qq bridge: onebot error 1403` and `disconnected code=1000`.
- Bind only to `127.0.0.1`. Do not expose this unencrypted port to the LAN or internet.
- Running multiple clients (for example the desktop pet and this backend) makes both
  receive the same private message and may double-reply; run only one.

### SnowLuma notes

- Download the full Windows package (Lite needs Node.js 24+), run `launcher.bat`, open
  `http://localhost:5099`, and sign in with the initial password from the startup log.
- Keep the official QQ NT client signed in and attach its process in the WebUI.
- Create a **WS server** node: host `127.0.0.1`, port `3001`, path `/`, role `Universal`,
  message format `array`, a generated token, and self-message reporting off.
- Use the same `ws://127.0.0.1:3001` and token here. NapCat and SnowLuma cannot share port 3001.

## AI settings

- **Built-in DeepSeek Web2API (free)**: default `http://127.0.0.1:3000/v1` + `sk-local`.
  The first launch requires a DeepSeek login; the session stays on this machine. If the
  login expires, QQ receives a notice — open tray **Settings → AI → Rebind** and log in
  again.
- **OpenAI-compatible API**: choose a preset or Custom, then fill `base_url` (ending in
  `/v1`), `API Key`, and `model`.
- **Persona**: the text box is the main body of the system prompt. Changing it starts a
  new conversation; the next message re-injects the persona and imports recent history.

## Proactive messages

Triggers: time windows (morning/noon/afternoon/evening/late night/weekend), weather
changes, 45 minutes of inactivity, 90 minutes without interaction, and random small talk.
The **proactive cooldown** limits how often any of these can fire. Setting it to `0`
keeps contextual triggers and disables random small talk.

- Proactive messages go only to whitelisted QQ numbers. Before anyone has bound, they are
  dropped (and logged).
- Saying good night sleeps for 8 hours; a nap phrase sleeps for 2 hours. While asleep the
  backend only sends one fixed line and no proactive messages.
- Proactive messages are written to history too, so they remain part of the conversation
  after a restart.

## History and continuity

- History files: `history/private-<QQ>.json` and `history/group-<group>.json` next to the
  executable, each capped by `chat_history_max`. The old global `chat_history.json` is
  migrated to the first private QQ that accesses it; other sessions start empty.
- User messages use `QQ（nickname）/private: text` and
  `group/group chat: sender QQ（nickname）: text`. Old history is prefixed on first load.
- The first AI request after each start includes the latest `ai_context_n` messages
  (default 10), so a restart continues the same conversation.
- The built-in web AI keeps a separate DeepSeek conversation for each QQ and group, so
  simultaneous private chats do not share context.
- QQ private images are downloaded and passed to the AI (one image, up to 15 MB), with
  optional text alongside. Vision requires a vision-capable model: the built-in DeepSeek
  Web2API supports it; the official `deepseek-chat` API is text-only and will error or
  ignore the image. In groups, only messages that mention the bot download images.

## Configuration

`config.json` lives next to the executable (project root when running from source):

| Key | Meaning |
| --- | --- |
| `ai_preset` / `ai_base_url` / `ai_api_key` / `ai_model` | AI endpoint |
| `ai_persona` | Persona (main system prompt) |
| `ai_context_n` | History messages imported when starting a conversation |
| `ai_web2api_max_messages` | Built-in DeepSeek messages per conversation (0 = unlimited) |
| `self_talk_enabled` / `self_talk_interval` | Proactive messages on/off and cooldown seconds |
| `qq_bridge_enabled` | Enable QQ |
| `qq_bridge_ws_url` | NapCat WS URL, default `ws://127.0.0.1:3001` |
| `qq_bridge_token` | Token matching NapCat |
| `qq_bridge_allowed_ids` | Allowed QQ numbers; empty auto-binds the first private sender |
| `qq_bridge_allowed_groups` | Allowed group numbers; empty ignores all group messages |
| `accounts` / `poll_interval_sec` | Balance accounts and poll interval (context only) |

`config.json` contains API keys and the QQ token. It is git-ignored — never commit or
share it.

## Run from source

```bash
pip install -r requirements.txt
python main.py
```

The built-in AI also needs dependencies inside the vendor directory:

```bash
cd vendor/DeepSeekWeb2API
npm install
```

`web2api.py` prefers `vendor/DeepSeekWeb2API/node/node.exe` and falls back to `node` on
PATH.

Tests (no network access):

```bash
python tests/smoke.py
```

## Build

```bash
python -m PyInstaller QQAIBackend.spec --noconfirm
```

Copy `vendor/DeepSeekWeb2API` (including `node/` and `node_modules/`) into
`dist/QQAIBackend/vendor/`, delete its `data/`, `tmp/`, and `web2api.log`, then zip the
directory. Never ship `config.json`, `chat_history.json`, `history/`, or login state.

## Risks and limits

- Unofficial QQ automation: risk-control actions, freezes, and bans are the user's
  responsibility. **Use a secondary account only.**
- Private chats support text and images. Groups only record messages and reply when
  mentioned; recalls, pokes, and similar events are not handled.
- If a future NapCat release renames network options, pick the mode where NapCat acts as
  the WebSocket server and this backend connects as a client.

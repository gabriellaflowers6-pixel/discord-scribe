# Discord Scribe Bot — Context

## What It Does
Records Discord voice channel audio, transcribes with Whisper, summarizes with Claude, posts meeting notes with downloadable files (summary, action items, transcript).

## Commands
- `/meet` — join voice channel, start recording. Shows live status updates every 10s (packets, audio length, elapsed time)
- `/endmeet` — stop recording, transcribe, summarize, post notes + 3 downloadable files (summary, action items, transcript)
- `/ping` — diagnostic: checks bot health + 3-sec audio capture test. Reports packets/bytes captured
- `/done` — mark an inbox item done; shows a searchable autocomplete dropdown of all pending items
## Drop Zone Inbox System
- **#drop-zone** — anyone posts tasks, links, files, notes, requests here
- **#gabbys-inbox** — Gabby's personal to-do list channel. One clean message at all times.
- **#joyis-inbox** — JoYI's personal to-do list channel. Same format as Gabby's.
- **SB Bot is BLOCKED** from posting in both inbox channels (channel permission deny SendMessages). Only scribe posts inbox summaries.
- **Self-cleaning channels**: every time the inbox updates, ALL messages in the inbox channel are deleted and one fresh summary is posted. No clutter, ever.
- **Disco ball**: when all items are done, a disco ball GIF embed appears (via `discord.Embed` with `.set_image()`).

### Inbox Format (3 sections)
1. **Updates** — FYI items, no action needed (Info/Other types). Shown first.
2. **Action Items** — to-dos that need doing (Task/Review/File types). Shown second.
3. **Recently Completed** — last 5 done items with strikethrough + ✅.
- Header shows: `**X action items** · Y updates` (updates don't count as pending)
- Each item uses Discord blockquotes (`>`) for detail + metadata
- **Short titles**: AI writes max 8-word subject line, not a full paragraph
- **Detail line**: one sentence of context, or bullet steps (`•`) for multi-step instructions
- **Links**: shown as clickable `[view](url)` after the title
- **No third-person narration**: "That kangaroo is pretty" not "Note that someone remarked..."
- **Mention resolution**: Discord `<@id>` mentions are converted to `@DisplayName` before AI classification

### Filtering & Assignment
- **Smart filtering**: messages with 4+ words always go through as actionable. Only very short messages (3 words or less, no links/attachments) get filtered by Claude — and even then, only true filler like "ok", "yeah", "lol" gets skipped (bot reacts with 👀). Everything else makes the cut.
- **Auto-assignment by person**: Claude reads the message to figure out who it's for (by name, @mention, or context). If unclear, falls back to "the other person" — JoYI drops something → assigned to Gabby, Gabby drops something → assigned to JoYI. Self-tagging (Gabby writes "@Gabby do X") = self-reminder, assigned to Gabby. If meant for both, assigned to General.
- **Concurrency lock**: `_inbox_lock` prevents duplicate items from one message
- Deduplication: Discord message ID tracked in memory — same message never processed twice
- Mark done: `/done` in Discord (autocomplete picks from pending list) or checkbox in `inbox.html`
- Data stored at `inbox/inbox.json` — each item has `assigned_to`, `summary`, `detail`, `url` fields
- View in browser: `inbox/inbox.html` (tabs: All / Tasks / Review / Info / Files / Other / Done)
- **IMPORTANT**: scribe ignores ALL bot messages in drop-zone. Only human messages get processed.

## Inbox Channel IDs
- `GABBY_INBOX_CHANNEL_ID=1482115278572748892`
- `JOYI_INBOX_CHANNEL_ID=1486226499718873098`

## Stack
- **Python 3.11** (`/opt/homebrew/bin/python3.11`) — MUST use 3.10+. macOS system Python 3.9 will NOT work (discord.py 2.7+ requires 3.10+, voice connection silently fails on 3.9)
- discord.py 2.7.1 (with `[voice]` extra)
- discord-ext-voice-recv 0.5.2a179
- openai-whisper (local transcription, `base` model ~150MB)
- anthropic SDK (Claude summarization)
- PyNaCl >= 1.5.0
- davey >= 0.1.0 (Discord Dave E2EE library — installed automatically with discord.py[voice])
- opus (`/opt/homebrew/lib/libopus.dylib` on macOS via Homebrew)

## Location
`/Users/gabriellakalvaitis-flowers/Desktop/my projects/discord-scribe/`

## Running
```bash
cd ~/Desktop/my\ projects/discord-scribe
/opt/homebrew/bin/python3.11 bot.py
```

## Config (.env)
- `DISCORD_BOT_TOKEN` — from Discord Developer Portal > Bot > Reset Token
- `ANTHROPIC_API_KEY` — from console.anthropic.com
- `MEETING_NOTES_CHANNEL_ID` — right-click text channel > Copy Channel ID (enable Developer Mode first)
- `WHISPER_MODEL` — `base` (fast), `small` (better), `medium` (best, slow). Default: `base`
- `GUILD_ID` — right-click server > Copy Server ID. Required for guild-scoped slash commands
- `DROP_ZONE_CHANNEL_ID` — ID of #drop-zone channel
- `MY_INBOX_CHANNEL_ID` — ID of #my-inbox channel

## Discord Bot Setup
1. Discord Developer Portal > New Application > Bot tab > Reset Token
2. Enable Privileged Intents: **Message Content**, **Server Members**
3. OAuth2 > URL Generator: scopes `bot` + `applications.commands`, permissions: Connect, Speak, Send Messages, Attach Files, Use Slash Commands
4. Invite bot to server with generated URL

---

## CRITICAL: Dave E2EE Voice Encryption (the hard part)

**This is the #1 thing that will break a Discord voice recording bot in 2026.**

### Background
As of **March 2, 2026**, Discord **enforces Dave E2EE** (end-to-end encryption) on ALL non-stage voice calls. If your bot doesn't support Dave, Discord closes the voice WebSocket with **close code 4017** and the bot blinks in and out of the voice channel.

### The Problem
`discord-ext-voice-recv` (0.5.2a) does NOT handle Dave decryption. It only handles standard RTP transport encryption. The audio pipeline is:

```
Sender: opus_audio -> dave_encrypt -> rtp_encrypt -> UDP send
Receiver: UDP recv -> rtp_decrypt -> [DAVE STILL ENCRYPTED] -> opus_decode FAILS
```

Without patching, you get one of:
- **Close code 4017** — if you try to disable Dave (bot can't even join)
- **0 packets / empty audio** — if Dave negotiates but received audio isn't Dave-decrypted
- **Opus decode crash** — PacketRouter thread dies on corrupted data

### The Solution: Three Monkey-Patches

You MUST apply these patches BEFORE the bot connects to voice. Put them at the top of bot.py after imports.

#### Patch 1: AudioReader.callback — Add Dave decryption after RTP decryption

Replace the entire `AudioReader.callback` method. After `self.decryptor.decrypt_rtp(packet)` (standard RTP decryption), call `dave_session.decrypt(user_id, davey.MediaType.audio, packet.decrypted_data)` to strip the Dave layer.

Key details:
- Get the dave_session from `self.voice_client._connection.dave_session`
- Get user_id from `self.voice_client._ssrc_to_id.get(packet.ssrc, 0)`
- Dave session takes time to become `ready`. During transitions, packets may be passthrough (unencrypted). Handle `DecryptionFailed(UnencryptedWhenPassthroughDisabled)` by checking `dave_session.can_passthrough` — if true, the data is already usable opus
- If decryption fails and NOT in passthrough mode, DROP the packet (return), don't feed garbage to opus

```python
import davey

conn = getattr(self.voice_client, '_connection', None)
dave_session = getattr(conn, 'dave_session', None) if conn else None

if dave_session and packet.decrypted_data:
    try:
        uid = self.voice_client._ssrc_to_id.get(packet.ssrc, 0)
        decrypted = dave_session.decrypt(uid, davey.MediaType.audio, packet.decrypted_data)
        if decrypted is not None:
            packet.decrypted_data = decrypted
    except Exception:
        if dave_session.can_passthrough:
            pass  # data is already usable opus
        else:
            return  # drop packet
```

#### Patch 2: PacketRouter._do_run — Make crash-resilient

The original `_do_run` loop will crash the entire router thread if ANY opus decode error occurs. Replace it with a version that catches exceptions per-packet and calls `decoder.reset()` on failure:

```python
def _resilient_do_run(self):
    while not self._end_thread.is_set():
        self.waiter.wait()
        with self._lock:
            for decoder in self.waiter.items:
                try:
                    data = decoder.pop_data()
                    if data is not None:
                        self.sink.write(data.source, data)
                except Exception:
                    try:
                        decoder.reset()
                    except Exception:
                        pass
```

#### Patch 3: Do NOT disable Dave

**NEVER** set `max_dave_protocol_version = 0`. Discord will reject the connection with close code 4017. Dave MUST stay enabled (protocol version 1).

An earlier approach tried monkey-patching `VoiceConnectionState.max_dave_protocol_version` to return 0. This worked before March 2, 2026 but is now fatal. Note that `voice_recv` has its OWN `VoiceConnectionState` class (in `discord.ext.voice_recv.voice_client`) separate from discord.py's (in `discord.voice_state`), so even if you patch one, the other may still negotiate Dave.

### How to verify it's working

Use the `/ping` command while in a voice channel and talking. You should see:
```
dave_protocol_version: 1
mode: aead_xchacha20_poly1305_rtpsize
Packets received: >0
PCM bytes captured: >0
Recording: WORKING
```

If `dave_protocol_version: 0` — your Dave patch is wrong, and you'll get 4017.
If packets = 0 but connected — Dave decryption in the callback isn't working.
If the bot blinks in/out — close code 4017, Dave is disabled or unsupported.

---

## Meeting Notes Format
Notes are structured **by topic** — Claude auto-detects the subjects discussed (coworking plans, project updates, scheduling, tech stuff, etc.) and creates a section for each. Action items are still collected together at the bottom. No more flat "Key points / Decisions" format.

---

## /ask and /add — Live in Scout Bot
`/ask` and `/add` are handled by Scout (separate bot, separate process).
See `/Users/gabriellakalvaitis-flowers/Desktop/my projects/scout-bot/brains/scout-context.md` for full spec.
Scout bot token is different from Scribe's. Both run on TPC.

## Multi-Server Support
**Status: PLANNED — Scribe needs per-server config**
Scribe will run on both TPC and Women Build Safety (WBS) servers. Needs:
- Config dict per server (channel IDs, user names, inbox rules)
- Currently hardcoded for TPC — needs refactor to support multiple guilds
- **TPC Server ID**: `1478130696739618901` — Discord MCP: `discord` (`mcp__discord__*` tools)
- **WBS Server ID**: `1486220693971669074` — Discord MCP: `discord-wbs` (`mcp__discord-wbs__*` tools)
- Both MCPs use the same bot token, different guild IDs
- WBS MCP fixed 2026-04-01: re-added with `npx -y @quadslab.io/discord-mcp` (old local node path was broken). Needs Claude Code restart to connect.
- WBS channels/inboxes: TBD — need to create channels and decide structure

---

## Architecture Notes

### Audio Pipeline
1. Bot joins voice channel with `VoiceRecvClient` (subclass of discord.py's VoiceClient)
2. `AudioReader` registers as a socket listener, receives raw UDP packets
3. `AudioReader.callback`: RTP decrypt -> Dave decrypt -> feed to `PacketRouter`
4. `PacketRouter` thread: jitter buffer -> opus decode -> calls `sink.write(user, data)`
5. `PCMRecorder` (custom `AudioSink`): collects raw PCM bytes in a bytearray
6. On `/endmeet`: save PCM to WAV (48kHz, 16-bit, stereo) -> Whisper transcribe -> Claude summarize

### PCMRecorder
Custom `AudioSink` subclass. Key: `wants_opus() -> False` so the decoder gives us PCM, not raw opus. WAV params: 2 channels, 2 bytes sample width, 48000Hz framerate.

### Whisper Performance
- `base` model: ~realtime on CPU (1 min audio ~ 1 min processing)
- First run downloads model (~150MB)
- Runs in `asyncio.to_thread()` to not block the bot

### Slash Command Sync
Commands are synced to a specific guild (fast, instant) not globally (takes up to 1 hour). Set `GUILD_ID` in `.env`. The `setup_hook` copies global commands to guild, syncs guild, then clears stale global commands.

---

## Debugging Checklist
1. **Bot won't start**: Check DISCORD_BOT_TOKEN in .env. Check Python version (must be 3.11+)
2. **Commands don't appear**: Bot must be running. Check GUILD_ID matches your server. Wait 1 min after restart
3. **Bot blinks in/out of voice**: Close code 4017 = Dave disabled. Do NOT patch max_dave_protocol_version to 0
4. **Connected but 0 packets**: Dave decryption patch not applied to AudioReader.callback. Check terminal for `[DAVE]` debug logs
5. **Opus decode crash**: PacketRouter._do_run not patched to be resilient. Router thread dies on first bad packet
6. **Transcription empty**: WAV file too small (check recordings/ dir). Audio captured but too quiet, or Dave decryption only partially working
7. **"Application did not respond"**: Bot process not running. Start it with `python3.11 bot.py`
8. **Whisper slow**: Normal on CPU. base model ~realtime. Use `small` for better accuracy (2-3x slower)
9. **Duplicate inbox entries**: Multiple bot instances ran simultaneously. Kill all with `pkill -f "python3.11 bot.py"`, start fresh, then manually dedupe `inbox/inbox.json`

## File Structure
```
discord-scribe/
  bot.py              — main bot code with Dave patches + inbox system
  .env                — secrets (token, API key, channel IDs, guild ID)
  .env.example        — template
  requirements.txt    — pip dependencies
  setup.md            — Discord bot creation instructions
  Dockerfile          — container config for VPS/cloud deployment
  recordings/         — auto-created, stores meeting WAV/transcript/summary files
  inbox/
    inbox.json        — auto-created, all drop-zone items
    inbox.html        — browser viewer with tabs (All/Tasks/Review/Info/Files/Done)
  brains/
    scribe-context.md — this file
```

---

## Hosting & Deployment

### Why Scribe Needs a VPS (Not Free Tier)
Scribe uses **Whisper for local transcription**, which needs real CPU and ~500MB+ RAM. Free tiers (Railway, Render) either don't have enough resources or spin down after inactivity, which kills a Discord bot that needs to be always-on.

**Recommended: $5-6/mo VPS** (DigitalOcean, Hetzner, Linode) — run Scribe in Docker for easy setup.

### Deploy to a VPS with Docker

#### 1. Get a VPS
- DigitalOcean: Create a $6/mo Droplet (1 vCPU, 1GB RAM, Ubuntu 24.04)
- Hetzner: CX22 (~$4/mo, 2 vCPU, 4GB RAM — best value for Whisper)
- Any Linux VPS with Docker support works

#### 2. Install Docker on the VPS
```bash
ssh root@YOUR_VPS_IP
curl -fsSL https://get.docker.com | sh
```

#### 3. Clone and configure
```bash
git clone https://github.com/gabriellaflowers6-pixel/discord-scribe.git /root/discord-scribe
cd /root/discord-scribe
```
Create .env using heredoc (nano mangles long lines on SSH):
```bash
cat > /root/discord-scribe/.env << 'EOF'
DISCORD_BOT_TOKEN=your_token
ANTHROPIC_API_KEY=your_key
MEETING_NOTES_CHANNEL_ID=your_channel_id
WHISPER_MODEL=base
GUILD_ID=your_guild_id
GUILD_IDS=guild1,guild2
DROP_ZONE_CHANNEL_ID=your_channel_id
GABBY_INBOX_CHANNEL_ID=your_channel_id
JOYI_INBOX_CHANNEL_ID=your_channel_id
EOF
```

#### 4. Build and run
```bash
docker build -t scribe .
docker run -d --name scribe --restart unless-stopped --env-file .env scribe
```

#### 5. Verify it's running
```bash
docker logs scribe          # check for "Logged in as ..."
docker ps                   # should show scribe running
```

Use `/ping` in Discord to confirm the bot is alive and capturing audio.

#### 6. Auto-restart on VPS reboot
The `--restart unless-stopped` flag handles this. Docker starts on boot by default on most VPS providers.

#### Updating the bot
```bash
cd discord-scribe
git pull
docker build -t scribe .
docker stop scribe && docker rm scribe
docker run -d --name scribe --restart unless-stopped --env-file .env scribe
```

### Running Locally (Dev Only)
```bash
cd ~/Desktop/my\ projects/discord-scribe
/opt/homebrew/bin/python3.11 bot.py
```
**Remember**: local = bot dies when your Mac sleeps. Only use for development/testing.

---

## Lessons Learned (Don't Repeat These)

### 1. Bot goes offline when Mac sleeps
**Problem**: Running `python3.11 bot.py` in a terminal means the bot only lives while your Mac is on and awake. Other people in the server see the bot as offline.
**Fix**: Deploy to a VPS with Docker and `--restart unless-stopped`. Bot runs 24/7 independent of your machine.

### 2. Dave E2EE broke everything (March 2026)
**Problem**: Discord enforced Dave E2EE on all voice calls. Bot couldn't join voice (close code 4017) or got 0 packets.
**Fix**: Three monkey-patches in bot.py (see Dave E2EE section above). NEVER disable Dave (protocol version 0). This was the hardest part of the entire build.

### 3. Zombie processes cause duplicate inbox entries
**Problem**: If you restart without killing old processes, multiple instances run simultaneously, each processing drop-zone messages.
**Fix**: Always `pkill -9 -f "Python bot.py"` before restarting. Docker solves this too — `docker stop` cleanly kills the old one.

### 4. System Python doesn't work
**Problem**: macOS system Python is 3.9. discord.py 2.7+ requires 3.10+. Voice connections silently fail on 3.9.
**Fix**: Always use `/opt/homebrew/bin/python3.11` locally, or use the Dockerfile (which installs 3.11).

### 5. Opus codec must be installed
**Problem**: Voice recording fails silently without libopus.
**Fix**: On macOS: `brew install opus`. In Docker: `apt-get install libopus0`. The Dockerfile handles this.

### 6. Whisper model downloads on first run
**Problem**: First `/endmeet` takes extra time because Whisper downloads ~150MB model.
**Fix**: The Dockerfile pre-downloads the model during build so it's baked into the image.

### 7. Opus path is hardcoded to macOS — breaks on Linux/Docker
**Problem**: `bot.py` had `discord.opus.load_opus("/opt/homebrew/lib/libopus.dylib")` which only works on macOS with Homebrew. On Linux (Docker/VPS), opus is at `libopus.so.0`. Bot crashes in a restart loop.
**Fix**: Platform detection in bot.py — `if sys.platform == "darwin"` loads the macOS path, else loads `libopus.so.0`. Already fixed in the codebase.

### 8. discord-ext-voice-recv is alpha-only — pip won't install with >=
**Problem**: All versions of `discord-ext-voice-recv` are pre-release (alpha). `pip install discord-ext-voice-recv>=0.5.0` finds nothing because pip ignores pre-release by default.
**Fix**: Pin the exact alpha version in requirements.txt: `discord-ext-voice-recv==0.5.2a179`. Do NOT use `>=`.

### 9. nano mangles long lines over SSH
**Problem**: Pasting env vars with long API keys into `nano` over SSH adds line breaks and spaces, corrupting the values.
**Fix**: Use `cat > .env << 'EOF'` heredoc instead of nano for creating .env files on the server.

### 10. Docker container name conflicts on rebuild
**Problem**: Running `docker run --name scribe` fails if the old container still exists (even if stopped).
**Fix**: Always `docker stop scribe && docker rm scribe` before `docker run`. Full rebuild flow:
```bash
docker stop scribe && docker rm scribe
cd /root/discord-scribe && git pull
docker build -t scribe .
docker run -d --name scribe --restart unless-stopped --env-file .env scribe
```

---

## Current Production Setup
- **Server**: Hetzner CX22 (~$4/mo, 2 vCPU, 4GB RAM, Nuremberg)
- **Server IP**: 135.181.196.231
- **OS**: Ubuntu 24.04
- **Runs alongside**: Scout Bot (same server)
- **GitHub repo**: https://github.com/gabriellaflowers6-pixel/discord-scribe
- **Docker container name**: `scribe`
- **Auto-restart**: yes (`--restart unless-stopped`)

---

## Archive (moved from Second Brain 2026-03-29)

### [2026-03-25] TPC Resources Channel Done

TPC Office Discord — Resources Channel DONE (2026-03-25)

UPDATE: The #resources channel has been created and populated with SB content. This task is complete.

Server: TPC Office (Guild ID: 1478130696739618901)

**What was built:**
- #resources channel created with SB content posted
- Design Resources channel created (left empty as planned)

**What was included:**
Professional knowledge from Second Brain: dev tips, design tools, workflow insights, Claude Code tips, UI generation tools, AI ad tools, mobile preview tips, terminal setup tips, etc.

**What was skipped (as planned):**
- Design inspo sites
- Pebble (pb) related content
- AnchorED session notes

### [2026-03-11] TPC Channel Setup Plan

TPC Office Discord — Channel Setup Plan (2026-03-11)

Server: TPC Office (Guild ID: 1478130696739618901)
Bot: SB Bot#4926

**What to build:** Create a #resources channel (or category with sub-channels) and populate it with SB content.

**What to include:** Post content from Second Brain (open-brain) — all professional knowledge: dev tips, design tools, workflow insights, Claude Code tips, UI generation tools, AI ad tools, mobile preview tips, terminal setup tips, etc.

**What to SKIP:**
- Design inspo sites (e.g. Gabby's favorite site: Yamauchi No.10 y-n10.com — do NOT post)
- Anything Pebble (pb) related — skip all Pebble app notes for now
- AnchorED session notes (project-specific, not resources)

**Design Resources channel:** Create the channel but leave it empty — no content posted there.

**Next step:** After restarting Claude Code (so Discord MCP loads), create the channel structure and post filtered SB content.

### [2026-03-11] Discord MCP Connector

Discord MCP Connector — Now Available and Connected (2026-03-11)

Previous SB note said "Discord has NO MCP connector" — this is outdated and no longer true.

**What was set up:**
- Package: @quadslab.io/discord-mcp (HardHeadHackerHead/discord-mcp)
- 134 admin tools across 20 categories (channels, messages, roles, members, threads, forums, webhooks, etc.)
- Added to Claude Code user config via `claude mcp add`
- Bot name: set up by Gabby in Discord Developer Portal
- Server: TPC Office (Guild ID: 1478130696739618901)
- Invite: https://discord.gg/UuaSGWSF

**How it was configured:**
```
claude mcp add discord -e DISCORD_TOKEN=<token> -e DISCORD_GUILD_ID=1478130696739618901 -s user -- npx -y @quadslab.io/discord-mcp
```
Config stored in ~/.claude.json

Requires restart of Claude Code to activate. After restart, Claude can directly create channels, post messages, manage roles, etc. in the TPC Office Discord server.

### [2026-03-27] Discord Scribe Bot

Discord Scribe Bot ("scribe bot") — Gabby's Discord voice meeting recorder. Records voice channel audio, transcribes with Whisper, summarizes with Claude, posts meeting notes + downloadable files (summary, action items, transcript). Commands: /meet, /endmeet, /ping. Project lives at /Users/gabriellakalvaitis-flowers/Desktop/my projects/discord-scribe/. Full context file with architecture, Dave E2EE patches, debugging checklist, and how to rebuild from scratch: brains/scribe-context.md. ALWAYS open that file before working on this bot. Must run with Python 3.11 (/opt/homebrew/bin/python3.11), NOT system Python. Critical detail: Discord enforces Dave E2EE on voice since March 2026 — voice_recv needs monkey-patches to handle it (all documented in the context file).

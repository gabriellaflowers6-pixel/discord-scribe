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
- **#gabbys-inbox** — Gabby's personal to-do list channel. One clean message at all times showing pending items + last 5 completed.
- **#joyis-inbox** — JoYI's personal to-do list channel. Same format as Gabby's.
- **Self-cleaning channels**: every time the inbox updates, ALL messages in the inbox channel are deleted and one fresh summary is posted. No clutter, ever.
- **Disco ball**: when all items are done, a disco ball GIF embed appears (via `discord.Embed` with `.set_image()`).
- **Smart filtering**: messages with 4+ words always go through as actionable. Only very short messages (3 words or less, no links/attachments) get filtered by Claude — and even then, only true filler like "ok", "yeah", "lol" gets skipped (bot reacts with 👀). Everything else makes the cut.
- **Auto-assignment by person**: Claude reads the message to figure out who it's for (by name, @mention, or context). If unclear, falls back to "the other person" — JoYI drops something → assigned to Gabby, Gabby drops something → assigned to JoYI. Self-tagging (Gabby writes "@Gabby do X") = self-reminder, assigned to Gabby. If meant for both, assigned to General.
- **Summaries are action-focused**: "Test anchorED for bugs" not "JoYI asked Gabby to test..."
- **Concurrency lock**: `_inbox_lock` prevents duplicate items from one message
- Deduplication: Discord message ID tracked in memory — same message never processed twice
- Mark done: `/done` in Discord (autocomplete picks from pending list) or checkbox in `inbox.html`
- Data stored at `inbox/inbox.json` — each item has an `assigned_to` field (Gabby, JoYI, or General)
- View in browser: `inbox/inbox.html` (tabs: All / Tasks / Review / Info / Files / Other / Done)
- **IMPORTANT**: when restarting, kill ALL `Python bot.py` processes first (`pkill -9 -f "Python bot.py"`) — old zombie instances cause duplicate processing

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
- **WBS Server ID**: `1486242055100432437` — Discord MCP: `discord-wbs` (`mcp__discord-wbs__*` tools)
- Both MCPs use the same bot token, different guild IDs
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
git clone YOUR_REPO_URL discord-scribe
cd discord-scribe
cp .env.example .env
nano .env  # fill in your secrets
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

# Discord Scribe — Setup

## 1. Create a Discord Bot

1. Go to https://discord.com/developers/applications
2. Click **New Application** → name it "Scribe" (or whatever)
3. Go to **Bot** tab → click **Reset Token** → copy the token
4. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent**
   - **Server Members Intent**
5. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Connect`, `Speak`, `Send Messages`, `Attach Files`, `Use Slash Commands`
6. Copy the generated URL and open it to invite the bot to your server

## 2. Get a Channel ID for Meeting Notes

1. In Discord, enable Developer Mode (Settings → Advanced → Developer Mode)
2. Right-click the text channel where you want notes posted → **Copy Channel ID**

## 3. Configure the Bot

```bash
cd ~/Desktop/my\ projects/discord-scribe
cp .env.example .env
```

Edit `.env` and fill in:
- `DISCORD_BOT_TOKEN` — from step 1
- `ANTHROPIC_API_KEY` — from https://console.anthropic.com
- `MEETING_NOTES_CHANNEL_ID` — from step 2
- `WHISPER_MODEL` — `base` is fine (fast + decent). Use `small` or `medium` for better accuracy

## 4. Install Dependencies

```bash
pip3 install -r requirements.txt
```

Note: Whisper will download its model files on first run (~150MB for base).

## 5. Run

```bash
python3 bot.py
```

## Usage

1. Join a voice channel
2. Type `/meet` — bot joins and starts recording
3. Have your meeting
4. Type `/endmeet` — bot leaves, transcribes, summarizes, and posts notes to your channel

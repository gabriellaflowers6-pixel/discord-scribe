import os
import io
import json
import uuid
import wave
import struct
import asyncio
import datetime
import re
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import voice_recv
from dotenv import load_dotenv
import whisper
import anthropic

# Load opus codec for voice receive decoding
import sys
if sys.platform == "darwin":
    discord.opus.load_opus("/opt/homebrew/lib/libopus.dylib")
else:
    discord.opus.load_opus("libopus.so.0")
print(f"Opus loaded: {discord.opus.is_loaded()}")

load_dotenv()

# Patch voice_recv to support Dave E2EE decryption on received audio.
# Discord enforces Dave E2EE on all non-stage voice calls (close code 4017 if unsupported).
# voice_recv decrypts the standard RTP layer but not the Dave layer, so we patch its
# AudioReader.callback to apply dave_session.decrypt() after standard decryption.
#
# Key issue: Dave session takes a moment to become ready. During transition,
# some packets are passthrough (unencrypted) and some are encrypted. We must
# handle both gracefully and not let failed decrypts crash the opus decoder.
import davey
from discord.ext.voice_recv.reader import AudioReader as _AudioReader
from discord.ext.voice_recv import rtp as _rtp
from nacl.exceptions import CryptoError as _CryptoError

_original_callback = _AudioReader.callback
_debug_stats = {'packets': 0, 'dave_ok': 0, 'dave_skip': 0, 'dave_err': 0}

def _patched_callback(self, packet_data):
    """Wraps the original callback to add Dave decryption after standard RTP decryption."""
    packet = rtp_packet = rtcp_packet = None
    try:
        if not _rtp.is_rtcp(packet_data):
            packet = rtp_packet = _rtp.decode_rtp(packet_data)
            # Standard RTP decryption (strips transport encryption)
            packet.decrypted_data = self.decryptor.decrypt_rtp(packet)

            _debug_stats['packets'] += 1

            # Apply Dave E2EE decryption on the opus payload
            conn = getattr(self.voice_client, '_connection', None)
            dave_session = getattr(conn, 'dave_session', None) if conn else None

            if dave_session and packet.decrypted_data:
                try:
                    uid = self.voice_client._ssrc_to_id.get(packet.ssrc, 0)
                    decrypted = dave_session.decrypt(uid, davey.MediaType.audio, packet.decrypted_data)
                    if decrypted is not None:
                        packet.decrypted_data = decrypted
                        _debug_stats['dave_ok'] += 1
                    else:
                        _debug_stats['dave_skip'] += 1
                except Exception:
                    # Decryption can fail during session transitions or for
                    # passthrough packets. The packet data may still be valid
                    # unencrypted opus if Dave is in passthrough mode.
                    _debug_stats['dave_err'] += 1
                    if dave_session.can_passthrough:
                        pass  # data is already usable opus
                    else:
                        return  # drop packet, can't decode it

            # Log stats periodically
            total = _debug_stats['packets']
            if total in (1, 10, 50) or total % 500 == 0:
                print(f"[DAVE] stats: {_debug_stats}")

        else:
            packet = rtcp_packet = _rtp.decode_rtcp(self.decryptor.decrypt_rtcp(packet_data))
    except _CryptoError:
        return
    except Exception:
        if len(packet_data) == 74 and packet_data[1] == 0x02:
            return
        return
    finally:
        if getattr(self, 'error', None):
            self.stop()
            return
        if not packet:
            return

    if rtcp_packet:
        self.packet_router.feed_rtcp(rtcp_packet)
    elif rtp_packet:
        ssrc = rtp_packet.ssrc
        if ssrc not in self.voice_client._ssrc_to_id:
            if rtp_packet.is_silence():
                return
        self.speaking_timer.notify(ssrc)
        try:
            self.packet_router.feed_rtp(rtp_packet)
        except Exception as e:
            # Don't let opus decode errors kill the whole router thread.
            # Just drop the bad packet and continue.
            _debug_stats['dave_err'] += 1

_AudioReader.callback = _patched_callback

# Also patch the PacketRouter.run to be crash-resilient
from discord.ext.voice_recv.router import PacketRouter as _PacketRouter

_original_router_do_run = _PacketRouter._do_run

def _resilient_do_run(self):
    """Wraps _do_run to catch opus decode errors without killing the thread."""
    while not self._end_thread.is_set():
        self.waiter.wait()
        with self._lock:
            for decoder in self.waiter.items:
                try:
                    data = decoder.pop_data()
                    if data is not None:
                        self.sink.write(data.source, data)
                except Exception as e:
                    # Reset the decoder state so it doesn't get stuck
                    try:
                        decoder.reset()
                    except Exception:
                        pass

_PacketRouter._do_run = _resilient_do_run

DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
NOTES_CHANNEL_ID = int(os.getenv("MEETING_NOTES_CHANNEL_ID", "0"))
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
GUILD_ID = os.getenv("GUILD_ID")
DROP_ZONE_CHANNEL_ID = int(os.getenv("DROP_ZONE_CHANNEL_ID", "0"))
GABBY_INBOX_CHANNEL_ID = int(os.getenv("GABBY_INBOX_CHANNEL_ID", "0"))
JOYI_INBOX_CHANNEL_ID = int(os.getenv("JOYI_INBOX_CHANNEL_ID", "0"))

INBOX_CHANNELS = {
    "Gabby": GABBY_INBOX_CHANNEL_ID,
    "JoYI": JOYI_INBOX_CHANNEL_ID,
    "General": GABBY_INBOX_CHANNEL_ID,  # General items go to Gabby's inbox
}

RECORDINGS_DIR = Path(__file__).parent / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)

INBOX_DIR = Path(__file__).parent / "inbox"
INBOX_DIR.mkdir(exist_ok=True)
INBOX_JSON = INBOX_DIR / "inbox.json"


# ── Inbox helpers ────────────────────────────────────────────────────────────

def load_inbox():
    if INBOX_JSON.exists():
        return json.loads(INBOX_JSON.read_text())
    return []


def save_inbox(items):
    INBOX_JSON.write_text(json.dumps(items, indent=2))


def classify_and_summarize(text, attachments, sender_name, is_short=False):
    """Ask Claude to classify + summarize a drop-zone message. Returns None if not actionable."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    attachment_note = ""
    if attachments:
        names = ", ".join(a["filename"] for a in attachments)
        attachment_note = f"\nAttachments: {names}"

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": (
                "You're a filter for a shared to-do inbox used by two people: Gabby and JoYI.\n\n"
                f"Message sent by: {sender_name}\n"
                f"Message: {text}{attachment_note}\n\n"
                "STEP 1 — Is this actionable?\n"
                f"{'This is a short message. ONLY say NO if it is literally a one-word reaction with zero meaning (ok, yeah, nice, lol, great). Anything else is YES.' if is_short else 'This message is more than a few words — it is ALWAYS actionable. Say YES.'}\n\n"
                "STEP 2 — Who is this task/item FOR? (who needs to act on it)\n"
                "Think carefully about the sender vs the recipient:\n"
                "- If Gabby writes '@Gabby do X' or 'I need to do X', she's reminding HERSELF. Assign to Gabby.\n"
                "- If Gabby writes '@JoYI do X' or 'JoYI please X', it's for JoYI. Assign to JoYI.\n"
                "- If JoYI writes '@Gabby do X', it's for Gabby. Assign to Gabby.\n"
                "- If someone tags themselves, it's a self-reminder — assign to THEM.\n"
                "- If it says 'both', 'us', or 'we', assign to General.\n"
                "- If you can't tell from the message, write ASSIGN: unknown\n\n"
                "STEP 3 — Write TWO things:\n"
                "TITLE: A short label (max 8 words). Like a subject line. Examples: 'Install Netlify GitHub app', 'Review stat graphic', 'Update Google Docs sharing'.\n"
                "DETAIL: One sentence of context — the why or how. Only if needed. Leave blank if the title says it all.\n\n"
                "IMPORTANT RULES FOR WRITING:\n"
                "- The sender's name is already shown in the inbox. NEVER refer to them in third person ('someone said', 'a user remarked').\n"
                "- Write the title/detail as if YOU are the sender. 'That kangaroo is pretty' NOT 'Note that someone remarked a kangaroo is pretty'.\n"
                "- Just capture WHAT was said, don't narrate or editorialize. No 'no clear action indicated' — that's what the TYPE field is for.\n\n"
                "Reply with exactly five lines:\n"
                "ACTIONABLE: yes or no\n"
                "ASSIGN: Gabby, JoYI, General, or unknown\n"
                "TYPE: <one of: Task, Review, Info, File>\n"
                "TITLE: <short label, max 8 words>\n"
                "DETAIL: <one sentence of context, or blank>"
            ),
        }],
    )
    raw = msg.content[0].text.strip()
    actionable = True
    assigned_to = "unknown"
    item_type = "Task"
    title = text[:60]
    detail = ""
    for line in raw.splitlines():
        if line.startswith("ACTIONABLE:"):
            actionable = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("ASSIGN:"):
            assigned_to = line.split(":", 1)[1].strip()
        elif line.startswith("TYPE:"):
            item_type = line.split(":", 1)[1].strip()
        elif line.startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()
        elif line.startswith("DETAIL:"):
            detail = line.split(":", 1)[1].strip()
        elif line.startswith("SUMMARY:"):
            # Backwards compat with old format
            title = line.split(":", 1)[1].strip()

    if not actionable:
        return None

    # Fallback: if Claude couldn't tell, assign to the other person
    if assigned_to == "unknown":
        sender_lower = sender_name.lower()
        if "joyi" in sender_lower or "joy" in sender_lower:
            assigned_to = "Gabby"
        elif "gabby" in sender_lower or "gabriella" in sender_lower:
            assigned_to = "JoYI"
        else:
            assigned_to = "General"

    return item_type, title, detail, assigned_to


def extract_urls(text):
    return re.findall(r"https?://\S+", text)



def format_inbox_summary(items):
    pending = [i for i in items if not i["done"]]
    type_emoji = {"Task": "📌", "Review": "👀", "Info": "ℹ️", "File": "📎", "Other": "📦"}
    person_emoji = {"Gabby": "🌸", "JoYI": "⚡", "General": "📂"}
    lines = [f"## 📥 Drop Zone Inbox", f"**{len(pending)} pending item{'s' if len(pending) != 1 else ''}**\n"]

    # Group by assigned_to (Gabby, JoYI, General, then unassigned)
    by_person = {}
    for item in pending:
        person = item.get("assigned_to", "Unassigned")
        by_person.setdefault(person, []).append(item)

    for person in ["Gabby", "JoYI", "General", "Unassigned"]:
        bucket = by_person.get(person, [])
        if not bucket:
            continue
        emoji = person_emoji.get(person, "📥")
        lines.append(f"{emoji} **{person} ({len(bucket)})**")
        for item in bucket:
            te = type_emoji.get(item["type"], "📦")
            date = item["submitted_at"][:10]
            lines.append(f"- {te} {item['summary']} — from @{item['submitted_by']} · {date} · ID: `{item['id']}`")
        lines.append("")

    lines.append("*React ✅ to any item to mark it done · or use `/done [id]`*")
    return "\n".join(lines)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.reactions = True
intents.guilds = True
intents.messages = True


class PCMRecorder(voice_recv.AudioSink):
    """Custom sink that collects raw PCM data from all users."""

    def __init__(self):
        super().__init__()
        self.pcm_data = bytearray()
        self.packet_count = 0

    def wants_opus(self) -> bool:
        return False  # We want decoded PCM

    def write(self, user, data):
        self.packet_count += 1
        pcm = data.pcm
        if pcm:
            self.pcm_data.extend(pcm)
        if self.packet_count <= 5 or self.packet_count % 100 == 0:
            print(f"[PCMRecorder] packet #{self.packet_count} from {user}, "
                  f"pcm_len={len(pcm) if pcm else 0}, total={len(self.pcm_data)}")

    def cleanup(self):
        print(f"[PCMRecorder] cleanup: {self.packet_count} packets, "
              f"{len(self.pcm_data)} bytes of PCM")

    def save_to_wav(self, path):
        """Save collected PCM data as a WAV file."""
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(2)       # stereo
            wf.setsampwidth(2)       # 16-bit
            wf.setframerate(48000)   # Discord uses 48kHz
            wf.writeframes(bytes(self.pcm_data))
        print(f"[PCMRecorder] saved {len(self.pcm_data)} bytes to {path}")


class ScribeBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.active_sessions = {}

    async def setup_hook(self):
        # Sync commands to all configured guilds
        guild_ids = [g.strip() for g in os.getenv("GUILD_IDS", GUILD_ID or "").split(",") if g.strip()]
        if guild_ids:
            for gid in guild_ids:
                guild = discord.Object(id=int(gid))
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                print(f"Commands synced to guild {gid}")
            # Clear stale global commands
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
        else:
            await self.tree.sync()
            print("Commands synced globally")


bot = ScribeBot()


@bot.tree.command(name="ping", description="Check if Scribe is alive and can record audio")
async def ping(interaction: discord.Interaction):
    lines = []
    lines.append(f"**Bot:** online as {bot.user}")
    lines.append(f"**Opus:** {'loaded' if discord.opus.is_loaded() else 'NOT LOADED'}")
    lines.append(f"**Whisper model:** {WHISPER_MODEL}")
    lines.append(f"**Active sessions:** {len(bot.active_sessions)}")

    if interaction.user.voice:
        vc_name = interaction.user.voice.channel.name
        members = len(interaction.user.voice.channel.members)
        lines.append(f"**Your voice channel:** {vc_name} ({members} members)")

        # Quick 3-second recording test
        await interaction.response.send_message("\n".join(lines) + "\n\n*Testing audio capture (3 sec)...*")

        voice_channel = interaction.user.voice.channel
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect(force=True)
            await asyncio.sleep(1)

        try:
            log = []
            log.append(f"Connecting to {voice_channel.name}...")
            vc = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
            log.append(f"connect() returned, is_connected={vc.is_connected()}")

            for i in range(30):
                if vc.is_connected():
                    log.append(f"Connected after {i} checks")
                    break
                await asyncio.sleep(0.5)
            else:
                log.append("Never connected after 30 checks!")

            log.append(f"After wait: is_connected={vc.is_connected()}")
            log.append(f"vc type: {type(vc).__name__}")
            log.append(f"vc.channel: {vc.channel}")

            if hasattr(vc, '_connection'):
                conn = vc._connection
                log.append(f"dave_protocol_version: {getattr(conn, 'dave_protocol_version', '?')}")
                log.append(f"mode: {getattr(conn, 'mode', '?')}")
                log.append(f"secret_key set: {getattr(conn, 'secret_key', None) is not None}")

            if not vc.is_connected():
                await interaction.followup.send("**Voice dropped.**\n```\n" + "\n".join(log) + "\n```")
                return

            await asyncio.sleep(2)
            log.append(f"After 2s sleep: is_connected={vc.is_connected()}")

            if not vc.is_connected():
                await interaction.followup.send("**Voice dropped after 2s.**\n```\n" + "\n".join(log) + "\n```")
                return

            sink = PCMRecorder()
            vc.listen(sink)
            log.append(f"Listening started, is_listening={vc.is_listening()}")
            await asyncio.sleep(3)
            vc.stop_listening()
            await vc.disconnect()

            result = (
                f"**Packets received:** {sink.packet_count}\n"
                f"**PCM bytes captured:** {len(sink.pcm_data):,}\n"
            )
            if sink.packet_count > 0:
                result += "**Recording: WORKING**"
            else:
                result += "**Recording: NO PACKETS** — audio capture may be broken"

            result += "\n```\n" + "\n".join(log) + "\n```"
            await interaction.followup.send(result)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"Ping error: {tb}")
            await interaction.followup.send(f"**Test failed:** {e}\n```\n{tb[-500:]}\n```")
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.disconnect(force=True)
    else:
        lines.append("*Join a voice channel and run /ping again to test audio capture.*")
        await interaction.response.send_message("\n".join(lines))


@bot.event
async def on_ready():
    print(f"Scribe is online as {bot.user}")
    print(f"Guilds: {[g.name for g in bot.guilds]}")
    print(f"Drop zone: {DROP_ZONE_CHANNEL_ID} | Gabby inbox: {GABBY_INBOX_CHANNEL_ID} | JoYI inbox: {JOYI_INBOX_CHANNEL_ID}")


def format_person_inbox(items, person):
    """Format inbox summary filtered for one person (+ General items)."""
    pending = [i for i in items if not i["done"] and i.get("assigned_to") in (person, "General")]
    done = [i for i in items if i["done"] and i.get("assigned_to") in (person, "General")]
    # Sort done by timestamp descending, take last 5
    done.sort(key=lambda x: x.get("submitted_at", ""), reverse=True)
    recent_done = done[:5]

    # Split pending into action items vs updates
    action_types = {"Task", "Review", "File"}
    action_items = [i for i in pending if i.get("type") in action_types]
    updates = [i for i in pending if i.get("type") not in action_types]

    type_emoji = {"Task": "\U0001f4cc", "Review": "\U0001f440", "Info": "\u2139\ufe0f", "File": "\U0001f4ce", "Other": "\U0001f4e6"}
    action_count = len(action_items)
    update_count = len(updates)
    lines = [f"## \U0001f4e5 {person}'s Inbox"]
    if action_count:
        lines.append(f"**{action_count} action item{'s' if action_count != 1 else ''}** \u00b7 {update_count} update{'s' if update_count != 1 else ''}\n")
    elif update_count:
        lines.append(f"**0 action items** \u00b7 {update_count} update{'s' if update_count != 1 else ''}\n")

    if not pending:
        lines.append("\U0001fa69 *All clear — nothing to do!* \U0001fa69")
    else:
        if updates:
            lines.append("**Updates**")
            for item in updates:
                te = type_emoji.get(item["type"], "\U0001f4e6")
                lines.append(f"- {te} **{item['summary']}**")
                detail = item.get("detail", "")
                if detail:
                    lines.append(f"  {detail}")
                if item.get("url"):
                    lines.append(f"  [view]({item['url']})")
                lines.append(f"  *from @{item['submitted_by']} \u00b7 {item['submitted_at'][:10]} \u00b7 ID: `{item['id']}`*")
            lines.append("")

        if action_items:
            lines.append("**Action Items**")
            for item in action_items:
                te = type_emoji.get(item["type"], "\U0001f4e6")
                lines.append(f"- {te} **{item['summary']}**")
                detail = item.get("detail", "")
                if detail:
                    lines.append(f"  {detail}")
                if item.get("url"):
                    lines.append(f"  [view]({item['url']})")
                lines.append(f"  *from @{item['submitted_by']} \u00b7 {item['submitted_at'][:10]} \u00b7 ID: `{item['id']}`*")
            lines.append("")

        lines.append("*Use `/done [id]` to mark items complete*")

    if recent_done:
        lines.append("")
        lines.append("**Recently completed**")
        for item in recent_done:
            lines.append(f"- ~~{item['summary']}~~ \u2705")

    return "\n".join(lines)


DISCO_BALL_GIF = "https://media1.tenor.com/m/nCN1ddz8rVMAAAAC/disco-ball.gif"

async def update_inbox_summary():
    """Refresh the inbox channel: delete all old messages, post fresh summary."""
    items = load_inbox()

    for person, channel_id in [("Gabby", GABBY_INBOX_CHANNEL_ID), ("JoYI", JOYI_INBOX_CHANNEL_ID)]:
        if not channel_id:
            continue
        channel = bot.get_channel(channel_id)
        if not channel:
            continue

        content = format_person_inbox(items, person)
        pending = [i for i in items if not i["done"] and i.get("assigned_to") in (person, "General")]

        # Delete ALL messages in the channel (bot + human) — inbox stays clean
        try:
            to_delete = []
            async for msg in channel.history(limit=50):
                to_delete.append(msg)
            for msg in to_delete:
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass
        except discord.Forbidden:
            pass

        # Post fresh summary
        await channel.send(content)

        # If inbox is clear, post disco ball GIF as an embed so it actually shows
        if not pending:
            embed = discord.Embed()
            embed.set_image(url=DISCO_BALL_GIF)
            await channel.send(embed=embed)


_processed_message_ids: set = set()
_inbox_lock = asyncio.Lock()

@bot.event
async def on_message(message):
    # Only care about messages in #drop-zone, ignore the bot itself
    if message.author.bot:
        return
    if not DROP_ZONE_CHANNEL_ID or message.channel.id != DROP_ZONE_CHANNEL_ID:
        return
    # Deduplicate — multiple bot instances or retries can fire this twice
    if message.id in _processed_message_ids:
        return
    _processed_message_ids.add(message.id)

    text = message.content or ""
    urls = extract_urls(text)
    attachments = [{"filename": a.filename, "url": a.url} for a in message.attachments]

    # Short messages (3 words or less, no links, no attachments) get filtered by AI
    # Anything longer just goes straight through — only classify for summary/assignment
    sender_name = message.author.display_name
    word_count = len(text.split()) if text else 0
    is_short = word_count <= 3 and not urls and not attachments

    if ANTHROPIC_API_KEY:
        result = await asyncio.to_thread(classify_and_summarize, text or "(attachment)", attachments, sender_name, is_short)
    else:
        result = ("Task", (text[:60] if text else "Attachment dropped"), "", "General")

    if result is None:
        # Not actionable — skip inbox entirely
        await message.add_reaction("👀")  # acknowledge we saw it
        return

    item_type, title, detail, assigned_to = result

    item_id = uuid.uuid4().hex[:8]
    item = {
        "id": item_id,
        "type": item_type,
        "summary": title,
        "detail": detail,
        "raw": text,
        "url": urls[0] if urls else (attachments[0]["url"] if attachments else ""),
        "jump_url": message.jump_url,
        "submitted_by": sender_name,
        "submitted_at": datetime.datetime.utcnow().isoformat(),
        "assigned_to": assigned_to,
        "done": False,
    }

    # Save to JSON and update pinned summary (locked to prevent dupes)
    async with _inbox_lock:
        items = load_inbox()
        items.append(item)
        save_inbox(items)
        await update_inbox_summary()


async def done_autocomplete(interaction: discord.Interaction, current: str):
    pending = [i for i in load_inbox() if not i["done"]]
    # Filter by channel — only show items assigned to this person's inbox
    # Items without assigned_to default to "General" (visible to both)
    channel_id = interaction.channel_id
    if channel_id == GABBY_INBOX_CHANNEL_ID:
        pending = [i for i in pending if i.get("assigned_to", "General") in ("Gabby", "General")]
    elif channel_id == JOYI_INBOX_CHANNEL_ID:
        pending = [i for i in pending if i.get("assigned_to", "General") in ("JoYI", "General")]
    # From any other channel (like drop-zone): show all pending items
    return [
        app_commands.Choice(
            name=f"[{i['type']}] {i['summary'][:80]} — @{i['submitted_by']}",
            value=i["id"]
        )
        for i in pending
        if current.lower() in i["summary"].lower() or current.lower() in i["id"]
    ][:25]


@bot.tree.command(name="done", description="Mark an inbox item as done")
@app_commands.describe(item="Pick the item to mark done")
@app_commands.autocomplete(item=done_autocomplete)
async def done_cmd(interaction: discord.Interaction, item: str):
    items = load_inbox()
    match = next((i for i in items if i["id"] == item), None)
    if not match:
        await interaction.response.send_message(f"Item not found.", ephemeral=True)
        return
    if match["done"]:
        await interaction.response.send_message(f"Already marked done.", ephemeral=True)
        return
    match["done"] = True
    save_inbox(items)
    await update_inbox_summary()
    await interaction.response.send_message(f"✅ Done: *{match['summary']}*", ephemeral=True)


@bot.tree.command(name="meet", description="Start recording this voice channel meeting")
async def meet(interaction: discord.Interaction):
    if not interaction.user.voice:
        await interaction.response.send_message(
            "You need to be in a voice channel first!", ephemeral=True
        )
        return

    guild_id = interaction.guild_id
    if guild_id in bot.active_sessions:
        await interaction.response.send_message(
            "A meeting is already being recorded!", ephemeral=True
        )
        return

    await interaction.response.defer()

    voice_channel = interaction.user.voice.channel

    # Disconnect any stale voice connection first
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.disconnect(force=True)
        await asyncio.sleep(1)

    # Connect using VoiceRecvClient so we can record
    voice_client = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)

    # Wait until voice is truly connected
    for i in range(30):
        if voice_client.is_connected():
            print(f"Voice connected after {i} checks")
            break
        await asyncio.sleep(0.5)

    await asyncio.sleep(2)
    print(f"Final state: is_connected={voice_client.is_connected()}")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = RECORDINGS_DIR / f"meeting_{timestamp}"
    session_dir.mkdir(exist_ok=True)

    # Use our custom PCM recorder
    sink = PCMRecorder()
    try:
        voice_client.listen(sink)
        print(f"Listening started, is_listening={voice_client.is_listening()}")
    except Exception as e:
        print(f"listen failed: {e}")
        import traceback
        traceback.print_exc()
        await interaction.followup.send(f"Failed to start recording: {e}")
        await voice_client.disconnect(force=True)
        return

    bot.active_sessions[guild_id] = {
        "voice_client": voice_client,
        "sink": sink,
        "session_dir": session_dir,
        "start_time": datetime.datetime.now(),
        "channel_name": voice_channel.name,
        "started_by": interaction.user.display_name,
        "text_channel": interaction.channel,
        "status_task": None,
    }

    status_msg = await interaction.followup.send(
        f"\U0001f4dd Recording in **{voice_channel.name}**...\n"
        f"Packets: 0 | Audio: 0s\n"
        f"Use `/endmeet` when you're done.",
        wait=True,
    )

    # Background task to update status with live capture stats
    async def update_status():
        try:
            while guild_id in bot.active_sessions:
                await asyncio.sleep(10)
                if guild_id not in bot.active_sessions:
                    break
                s = bot.active_sessions[guild_id]["sink"]
                elapsed = datetime.datetime.now() - bot.active_sessions[guild_id]["start_time"]
                elapsed_str = str(elapsed).split(".")[0]
                audio_secs = len(s.pcm_data) / (48000 * 2 * 2)  # 48kHz, 16-bit, stereo
                await status_msg.edit(
                    content=(
                        f"\U0001f534 Recording in **{voice_channel.name}** ({elapsed_str})\n"
                        f"Packets: {s.packet_count:,} | Audio captured: {audio_secs:.0f}s\n"
                        f"Use `/endmeet` when you're done."
                    )
                )
        except Exception:
            pass  # channel deleted, permissions, etc.

    bot.active_sessions[guild_id]["status_task"] = asyncio.create_task(update_status())


@bot.tree.command(
    name="endmeet", description="Stop recording and generate meeting summary"
)
async def endmeet(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    if guild_id not in bot.active_sessions:
        # No tracked session — force-disconnect if still in voice
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.disconnect(force=True)
            await interaction.response.send_message(
                "No active recording found, but I've left the voice channel."
            )
        else:
            await interaction.response.send_message(
                "No meeting is being recorded.", ephemeral=True
            )
        return

    session = bot.active_sessions[guild_id]
    voice_client = session["voice_client"]
    sink = session["sink"]

    # Cancel the live status updater
    if session.get("status_task"):
        session["status_task"].cancel()

    await interaction.response.send_message("\u23f9\ufe0f Stopping recording...")

    print(f"Stopping: {sink.packet_count} packets, {len(sink.pcm_data)} bytes captured")

    # Stop listening and disconnect
    voice_client.stop_listening()
    await voice_client.disconnect()

    # Save PCM to WAV
    wav_path = session["session_dir"] / "recording.wav"
    sink.save_to_wav(wav_path)

    # Process the recording
    await process_recording(session, interaction.channel, wav_path)


async def process_recording(session, channel, wav_path):
    """Process recorded audio: transcribe and summarize."""
    guild_id = channel.guild.id
    session_dir = session["session_dir"]
    start_time = session["start_time"]
    end_time = datetime.datetime.now()
    duration = end_time - start_time
    channel_name = session["channel_name"]

    status_msg = await channel.send("\u23f3 Processing meeting audio...")

    # Check if we got any audio
    file_size = wav_path.stat().st_size if wav_path.exists() else 0
    print(f"WAV file size: {file_size} bytes")
    if file_size < 100:
        await status_msg.edit(
            content="No audio was captured. Was anyone talking?"
        )
        del bot.active_sessions[guild_id]
        return

    await status_msg.edit(content="\u23f3 Transcribing audio with Whisper...")

    # Transcribe
    transcript = await asyncio.to_thread(transcribe_audio, wav_path)

    if not transcript.strip():
        await status_msg.edit(
            content="Transcription came back empty — audio may have been too quiet."
        )
        del bot.active_sessions[guild_id]
        return

    # Save transcript
    transcript_path = session_dir / "transcript.txt"
    transcript_path.write_text(transcript)

    await status_msg.edit(content="\u23f3 Generating summary with Claude...")

    # Summarize
    summary, action_items = await asyncio.to_thread(
        summarize_transcript, transcript, channel_name, duration
    )

    # Post results — find meeting-notes channel in the same guild, fall back to env var, then current channel
    notes_channel = None
    if channel.guild:
        notes_channel = discord.utils.get(channel.guild.text_channels, name="meeting-notes")
    if not notes_channel and NOTES_CHANNEL_ID:
        notes_channel = bot.get_channel(NOTES_CHANNEL_ID)
    if not notes_channel:
        notes_channel = channel

    duration_str = str(duration).split(".")[0]
    date_str = start_time.strftime("%B %d, %Y at %I:%M %p")

    header = (
        f"# Meeting Notes — {channel_name}\n"
        f"**Date:** {date_str}\n"
        f"**Duration:** {duration_str}\n"
        f"**Started by:** {session['started_by']}\n\n"
    )

    full_post = header + summary
    if len(full_post) > 2000:
        chunks = split_message(full_post, 2000)
        for chunk in chunks:
            await notes_channel.send(chunk)
    else:
        await notes_channel.send(full_post)

    # Save summary and action items to files
    summary_with_header = header + summary
    summary_path = session_dir / "summary.txt"
    summary_path.write_text(summary_with_header)

    action_items_path = session_dir / "action_items.txt"
    action_items_with_header = (
        f"Action Items — {channel_name}\n"
        f"Date: {date_str}\n"
        f"{'=' * 40}\n\n"
        f"{action_items}"
    )
    action_items_path.write_text(action_items_with_header)

    # Post downloadable files
    files = [
        discord.File(io.BytesIO(summary_with_header.encode()), filename="meeting_summary.txt"),
        discord.File(io.BytesIO(action_items_with_header.encode()), filename="action_items.txt"),
        discord.File(io.BytesIO(transcript.encode()), filename="transcript.txt"),
    ]
    await notes_channel.send(
        "\U0001f4ce **Downloads:** summary, action items, and full transcript",
        files=files,
    )

    await status_msg.edit(content="\u2705 Meeting notes posted!")
    del bot.active_sessions[guild_id]


def transcribe_audio(audio_path):
    """Transcribe audio using local Whisper."""
    print(f"Loading Whisper model: {WHISPER_MODEL}")
    model = whisper.load_model(WHISPER_MODEL)
    print("Transcribing...")
    result = model.transcribe(str(audio_path))
    return result["text"]


def summarize_transcript(transcript, channel_name, duration):
    """Summarize transcript using Claude API. Returns (summary, action_items)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[
            {
                "role": "user",
                "content": (
                    f"You're summarizing a Discord voice meeting in "
                    f"'{channel_name}' ({duration} long).\n\n"
                    f"Write meeting notes that sound like a smart teammate jotted them down — "
                    f"natural, specific, and useful. NOT like a corporate AI summary.\n\n"
                    f"Rules:\n"
                    f"- **Use people's names.** If someone said they'd do something, name them. "
                    f"'Gabby will look into X' not 'no specific assignee mentioned.'\n"
                    f"- **Be specific.** Include exact tools, terms, project names, and details "
                    f"people mentioned — even if you're not sure what they mean. "
                    f"If someone says 'gitrees' or 'worktrees' or any technical term, include it. "
                    f"Transcription may be imperfect — keep the original wording when in doubt.\n"
                    f"- **Don't pad.** Skip sections that don't apply. Don't write "
                    f"'None identified' or 'No specific details mentioned' — just leave it out.\n"
                    f"- **Don't hedge.** If something was discussed, state it. Don't add "
                    f"'specific details about...' as an open question if the details were actually said.\n"
                    f"- **Keep the voice human.** Short sentences. No filler. "
                    f"Write like you're catching someone up in Slack, not drafting a formal report.\n\n"
                    f"Format:\n"
                    f"**What happened** — 2-3 sentence overview\n\n"
                    f"Then organize the rest **by topic/subject**. Identify the distinct topics "
                    f"that came up in the conversation (e.g. coworking plans, project updates, "
                    f"scheduling, tech stuff, personal catch-up, business ideas — whatever was "
                    f"actually discussed). Create a section for each topic:\n\n"
                    f"**[Topic Name]**\n- bullets with key points, decisions, and action items for that topic\n\n"
                    f"Only create sections for topics that were actually discussed. "
                    f"Use short, clear topic names. If something was only mentioned in passing, "
                    f"fold it into the closest topic — don't create a section for one bullet.\n\n"
                    f"After all topic sections, add:\n\n"
                    f"**Action items** (all of them collected together, with names attached)\n- bullets\n\n"
                    f"**Still open** (only genuine unresolved questions)\n- bullets\n\n"
                    f"After the main notes, add a section starting with exactly "
                    f"\"---ACTION_ITEMS---\" on its own line, followed by ONLY the "
                    f"action items as a plain bullet list (no headers, no other text). "
                    f"If there are genuinely no action items, write \"None.\"\n\n"
                    f"TRANSCRIPT:\n{transcript}"
                ),
            }
        ],
    )

    full_text = message.content[0].text

    # Split out action items
    if "---ACTION_ITEMS---" in full_text:
        summary, action_items = full_text.split("---ACTION_ITEMS---", 1)
        summary = summary.strip()
        action_items = action_items.strip()
    else:
        summary = full_text
        action_items = "None identified."

    return summary, action_items


def split_message(text, limit):
    """Split a message into chunks that fit Discord's character limit."""
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("ERROR: Set DISCORD_BOT_TOKEN in your .env file")
        exit(1)
    if not ANTHROPIC_API_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY in your .env file")
        exit(1)

    bot.run(DISCORD_TOKEN)

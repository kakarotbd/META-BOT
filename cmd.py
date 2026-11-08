"""Voice-only Gemini assistant and its allowed Discord control actions."""
import asyncio
import base64
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
import wave
from array import array
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import voice_recv

from app_config import APP_CONFIG, PROJECT_ROOT


ACTIVE_CHAT_SESSIONS = {}


def _api_key():
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if key:
        return key
    try:
        from dotenv import dotenv_values
        key = str(dotenv_values(PROJECT_ROOT / ".env").get("GEMINI_API_KEY", "") or "").strip()
    except Exception:
        key = ""
    if key:
        os.environ["GEMINI_API_KEY"] = key
    return key


class _SpeechSink(voice_recv.AudioSink):
    """Buffer only the /chat invoker's PCM speech and split after a pause."""
    def __init__(self, user_id):
        super().__init__()
        self.user_id = int(user_id)
        self.lock = threading.Lock()
        self.buffer = bytearray()
        self.last_speech_at = 0.0

    def wants_opus(self):
        return False

    def write(self, user, data):
        if not user or user.bot or int(user.id) != self.user_id or not data.pcm:
            return
        pcm = data.pcm
        try:
            samples = array("h")
            samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
            if not samples:
                return
            # Ignore decoded silence and room noise; audio is retained only for the authorized caller.
            level = sum(abs(value) for value in samples) / len(samples)
            if level < 220:
                return
        except (ValueError, OverflowError):
            return
        with self.lock:
            if len(self.buffer) < 8_000_000:
                self.buffer.extend(pcm)
                self.last_speech_at = time.monotonic()

    def take_after_pause(self):
        with self.lock:
            if not self.buffer or time.monotonic() - self.last_speech_at < 1.0:
                return None
            chunk = bytes(self.buffer)
            self.buffer.clear()
            self.last_speech_at = 0.0
            return chunk if len(chunk) >= 48_000 else None

    def cleanup(self):
        with self.lock:
            self.buffer.clear()


def _gemini_audio_request(pcm, history, pending_action=None):
    wav_buffer = BytesIO()
    with wave.open(wav_buffer, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(pcm)
    system = (
        "You are a helpful Discord voice assistant. Understand the caller's spoken English, Bangla, or Banglish, "
        "and answer in clear natural English. The caller is speaking live in a voice channel. Use action=answer for "
        "ordinary questions, action=kick or action=ban only for an explicit kick/ban request, action=move only for an explicit voice "
        "move request, and action=send_message only for an explicit request to post text in a server text channel. "
        "For Discord actions, extract the exact target member name, destination voice-channel name, text-channel name, "
        "and exact message. Never invent names or perform an action unless the caller explicitly asked. Unsupported "
        "Discord commands must be explained conversationally, not executed. Keep replies concise and suitable for speech."
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [
            {"text": "Recent conversation for context: " + json.dumps(history[-8:], ensure_ascii=False) + "\nPending action: " + json.dumps(pending_action or {}, ensure_ascii=False) + ". If a pending action exists, accurately transcribe the caller's new utterance; do not invent a different action."},
            {"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(wav_buffer.getvalue()).decode("ascii")}},
        ]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "heard_text": {"type": "STRING"},
                    "action": {"type": "STRING", "enum": ["answer", "kick", "ban", "move", "send_message"]},
                    "reply": {"type": "STRING"},
                    "target_name": {"type": "STRING"},
                    "destination_name": {"type": "STRING"},
                    "channel_name": {"type": "STRING"},
                    "message_text": {"type": "STRING"},
                },
                "required": ["heard_text", "action", "reply", "target_name", "destination_name", "channel_name", "message_text"],
            },
            "maxOutputTokens": 500,
        },
    }
    # Make web grounding available every turn; Gemini decides when a search is useful.
    payload["tools"] = [{"google_search": {}}]
    model = os.getenv("VC_AI_MODEL", "gemini-3.8-flash").strip()
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers={
        "x-goog-api-key": _api_key(), "Content-Type": "application/json",
    }, method="POST")
    with urllib.request.urlopen(req, timeout=45) as response:
        body = json.loads(response.read().decode())
    candidate = body["candidates"][0]
    result = json.loads(candidate["content"]["parts"][0]["text"])
    sources = []
    for chunk in candidate.get("groundingMetadata", {}).get("groundingChunks", []):
        web = chunk.get("web") or {}
        if web.get("uri"):
            sources.append({"title": web.get("title") or web["uri"], "url": web["uri"]})
    return result, sources


def _normalize(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


async def _find_member(guild, name):
    key = _normalize(name)
    if not key:
        return None
    cached = {member.id: member for member in guild.members}
    for channel in guild.voice_channels:
        cached.update({member.id: member for member in channel.members})
    members = list(cached.values())
    matches = [member for member in members if not member.bot and key in {
        _normalize(member.display_name), _normalize(member.name), _normalize(member.global_name),
    }]
    if not matches:
        try:
            queried = await guild.query_members(query=str(name)[:32], limit=10, cache=True)
            matches = [member for member in queried if not member.bot and key in {
                _normalize(member.display_name), _normalize(member.name), _normalize(member.global_name),
            }]
        except (discord.Forbidden, discord.HTTPException):
            pass
    return matches[0] if len(matches) == 1 else None


def _find_channel(channels, name):
    key = _normalize(name)
    if not key:
        return None
    matches = [channel for channel in channels if _normalize(channel.name) == key]
    if len(matches) == 1:
        return matches[0]
    partial = [channel for channel in channels if key in _normalize(channel.name)]
    return partial[0] if len(partial) == 1 else None


async def _speak(vc, text):
    import edge_tts
    temp = tempfile.NamedTemporaryFile(prefix="chat-ai-", suffix=".mp3", delete=False)
    path = temp.name
    temp.close()
    try:
        await edge_tts.Communicate(text[:500], os.getenv("VC_AI_VOICE", "en-US-AnaNeural"), rate="+5%", pitch="+8Hz").save(path)
        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        if vc.is_playing():
            vc.stop()
        vc.play(discord.FFmpegPCMAudio(path), after=lambda _error: loop.call_soon_threadsafe(done.set))
        await asyncio.wait_for(done.wait(), timeout=60)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def _confirmable_action(guild, result, session):
    action = str(result.get("action", "answer")).lower()
    if action not in {"kick", "ban", "move"}:
        return str(result.get("reply", "Sorry, I didn't understand that. Please say it again."))[:500]
    target_name = str(result.get("target_name", "")).strip()
    if not target_name:
        session["pending_action"] = {"action": action, "phase": "target"}
        return "Sir, tell me the username you want me to " + ("move." if action == "move" else action + ".")
    target = await _find_member(guild, target_name)
    if not target or target.id == APP_CONFIG.admin_user_id or target == guild.owner or target.bot:
        return f"I couldn't find one eligible server member named {target_name}. Please say the exact username again."
    pending = {"action": action, "phase": "confirm", "target_id": target.id}
    if action == "move":
        destination_name = str(result.get("destination_name", "")).strip()
        destination = _find_channel(guild.voice_channels, destination_name)
        if not destination:
            pending["phase"] = "destination"
            session["pending_action"] = pending
            return f"I found {target.display_name}. Which voice channel should I move them to?"
        pending["destination_id"] = destination.id
    session["pending_action"] = pending
    if action == "move":
        destination = guild.get_channel(pending["destination_id"])
        return f"Sir, are you sure you want me to move {target.display_name} to {destination.name}? Say yes or no."
    return f"Sir, are you sure you want me to {action} {target.display_name}? Say yes or no."


async def _advance_pending_action(guild, caller, session, heard_text):
    pending = session.get("pending_action")
    if not pending:
        return None
    heard = str(heard_text or "").strip()
    if not heard:
        return "Sorry, I didn't catch that. Please say it again."
    if pending["phase"] == "target":
        target = await _find_member(guild, heard)
        if not target or target.id == APP_CONFIG.admin_user_id or target == guild.owner or target.bot:
            session["pending_action"] = None
            return f"I couldn't find one eligible server member named {heard}. Please start again and say the exact username."
        pending["target_id"] = target.id
        if pending["action"] == "move":
            pending["phase"] = "destination"
            return f"I found {target.display_name}. Which voice channel should I move them to?"
        pending["phase"] = "confirm"
        return f"Sir, are you sure you want me to {pending['action']} {target.display_name}? Say yes or no."
    if pending["phase"] == "destination":
        destination = _find_channel(guild.voice_channels, heard)
        if not destination:
            session["pending_action"] = None
            return f"I couldn't find one voice channel named {heard}. Please start the move request again with the exact channel name."
        pending["destination_id"] = destination.id
        pending["phase"] = "confirm"
        target = guild.get_member(pending["target_id"])
        return f"Sir, are you sure you want me to move {target.display_name} to {destination.name}? Say yes or no."
    normalized = _normalize(heard)
    if normalized in {"no", "nope", "cancel", "dont", "dontdoit", "negative"}:
        session["pending_action"] = None
        return "Okay, I cancelled that action."
    if normalized not in {"yes", "yeah", "yep", "confirm", "confirmed", "doit", "goahead", "okay", "ok"}:
        return "Please say yes to confirm or no to cancel."

    action = pending["action"]
    target = guild.get_member(pending["target_id"])
    session["pending_action"] = None
    if not target or target.id in {caller.id, APP_CONFIG.admin_user_id} or target == guild.owner or target.bot:
        return "I couldn't verify that target anymore, so I cancelled the action."
    me = guild.me
    if not me or target.top_role >= me.top_role:
        return "I can't do that because of the bot's role hierarchy."
    if action == "kick":
        if not me.guild_permissions.kick_members:
            return "I don't have the Kick Members permission."
        await target.kick(reason=f"Confirmed by voice /chat user {caller} ({caller.id})")
        return f"{target.display_name} has been kicked from the server."
    if action == "ban":
        if not me.guild_permissions.ban_members:
            return "I don't have the Ban Members permission."
        await target.ban(reason=f"Confirmed by voice /chat user {caller} ({caller.id})", delete_message_seconds=0)
        return f"{target.display_name} has been banned from the server."
    destination = guild.get_channel(pending.get("destination_id"))
    if not destination or not target.voice or not target.voice.channel:
        return "That member is no longer in a voice channel, so I couldn't move them."
    if not me.guild_permissions.move_members:
        return "I don't have the Move Members permission."
    await target.move_to(destination, reason=f"Confirmed by voice /chat user {caller} ({caller.id})")
    return f"{target.display_name} has been moved to {destination.name}."


async def _perform_action(guild, caller, result, session):
    action = str(result.get("action", "answer")).lower()
    me = guild.me
    reply = str(result.get("reply", "Sorry, I didn't understand that. Please say it again."))[:500]
    if action in {"kick", "ban", "move"}:
        return await _confirmable_action(guild, result, session)
    if action == "send_message":
        channel = _find_channel(guild.text_channels, result.get("channel_name"))
        content = str(result.get("message_text", "")).strip()
        if not channel or not content:
            return "I couldn't identify the text channel or exact message. Please say the channel name and message again."
        if len(content) > 1800:
            return "That message is too long to post. Please keep it under 1800 characters."
        if not me or not channel.permissions_for(me).send_messages:
            return f"I don't have permission to send messages in {channel.name}."
        await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
        return f"I posted that message in {channel.name}."
    return reply


async def _voice_chat_worker(moderator, guild, caller, vc, sink, session):
    history = []
    try:
        while session["active"] and vc.is_connected():
            await asyncio.sleep(0.2)
            pcm = sink.take_after_pause()
            if not pcm:
                continue
            try:
                result, sources = await asyncio.to_thread(_gemini_audio_request, pcm, history, session.get("pending_action"))
                heard = str(result.get("heard_text", "")).strip()[:500]
                if session.get("pending_action"):
                    reply = await _advance_pending_action(guild, caller, session, heard)
                else:
                    reply = await _perform_action(guild, caller, result, session)
                if heard:
                    history.extend(["User: " + heard, "Assistant: " + reply[:500]])
                if sources:
                    reply += " Sources: " + "; ".join(source["title"] for source in sources[:3])
                await _speak(vc, reply)
            except Exception as exc:
                moderator.log(f"Voice /chat turn failed in {guild.name}: {exc}")
                if vc.is_connected():
                    try:
                        await _speak(vc, "I had trouble processing that. Please say it again in a moment.")
                    except Exception:
                        pass
    except asyncio.CancelledError:
        pass
    finally:
        sink.cleanup()
        if ACTIVE_CHAT_SESSIONS.get(guild.id) is session:
            ACTIVE_CHAT_SESSIONS.pop(guild.id, None)


def register(moderator, bot):
    """Register the single /chat command; it toggles voice AI on/off."""
    moderator.log(f"Voice AI config loaded: Gemini key={'present' if _api_key() else 'missing'}, model={os.getenv('VC_AI_MODEL', 'gemini-3.8-flash')}.")
    @bot.tree.command(name="chat", description="Start or stop the voice AI chat in your current voice channel")
    @app_commands.guild_only()
    async def chat(interaction: discord.Interaction):
        if not moderator.vc_ai_allowed(interaction.user):
            return await interaction.response.send_message("You do not have an allowed Voice AI role. GUI-te VC AI Permissions set korte hobe.", ephemeral=True)
        guild = interaction.guild
        active = ACTIVE_CHAT_SESSIONS.get(guild.id)
        if active:
            if active["owner_id"] != interaction.user.id:
                return await interaction.response.send_message("Voice AI is already active in this server; only the member who started it can stop it with /chat.", ephemeral=True)
            active["active"] = False
            active["vc"].stop_listening()
            active["task"].cancel()
            await active["vc"].disconnect(force=True)
            ACTIVE_CHAT_SESSIONS.pop(guild.id, None)
            return await interaction.response.send_message("Voice AI stopped and left the voice channel.", ephemeral=True)

        state = getattr(interaction.user, "voice", None)
        if not state or not state.channel:
            return await interaction.response.send_message("Age voice channel-e join koro, tarpor /chat dao.", ephemeral=True)
        if not _api_key():
            return await interaction.response.send_message("Gemini key process-e load hoyni. Railway Variables-e exact GEMINI_API_KEY name diye set kore latest deployment redeploy koro; local-e AI tab theke .env-e save koro.", ephemeral=True)
        me = guild.me
        permissions = state.channel.permissions_for(me) if me else None
        if not permissions or not permissions.connect or not permissions.speak:
            return await interaction.response.send_message("Bot-er oi VC-te Connect ar Speak permission lagbe.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            current = guild.voice_client
            if current and current.is_connected():
                await current.disconnect(force=True)
            vc = await state.channel.connect(cls=voice_recv.VoiceRecvClient)
            sink = _SpeechSink(interaction.user.id)
            vc.listen(sink)
            session = {"owner_id": interaction.user.id, "active": True, "vc": vc, "sink": sink}
            task = asyncio.create_task(_voice_chat_worker(moderator, guild, interaction.user, vc, sink, session))
            session["task"] = task
            ACTIVE_CHAT_SESSIONS[guild.id] = session
            await interaction.followup.send(
                f"Joined **{state.channel.name}** and started listening to your voice. Speak naturally; run /chat again to stop. Your speech is sent to Gemini to generate replies.",
                ephemeral=True,
            )
        except Exception as exc:
            moderator.log(f"Could not start voice /chat in {guild.name}: {exc}")
            await interaction.followup.send("Could not start voice chat. Check voice receive dependency, bot permissions, and Railway logs.", ephemeral=True)


"""Voice-only Gemini assistant and its allowed Discord control actions."""
import asyncio
import base64
import ctypes.util
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
try:
    from discord.ext import voice_recv
    _VOICE_RECV_IMPORT_ERROR = None
except ImportError as exc:
    # Keep the desktop control panel usable when its local environment has not
    # installed the optional voice-receive extension; Railway may still provide it.
    voice_recv = None
    _VOICE_RECV_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

from app_config import APP_CONFIG, PROJECT_ROOT


ACTIVE_CHAT_SESSIONS = {}
IDLE_DISCONNECT_SECONDS = 300
_WEB_QUERY = re.compile(r"\b(search|google|internet|web|latest|current|today|right now|news|weather|price|who won|look up|find online|this week|this year)\b", re.I)
_API_KEY_SOURCE = "missing"


def _api_key(store=None):
    global _API_KEY_SOURCE
    key_names = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GEMINI_API_KEY", "GEMINI_KEY")
    # GUI saves this value in Fernet-encrypted shared Cloudflare settings so the
    # desktop bot and Railway worker can use the same key without a Railway copy.
    if store is not None:
        try:
            shared_key = str(store.get_settings().get("vc_ai_api_key", "") or "").strip()
            if shared_key:
                _API_KEY_SOURCE = "encrypted Cloudflare settings"
                return shared_key
        except Exception as exc:
            _voice_log(f"[voice-ai] shared API key lookup failed: {type(exc).__name__}: {str(exc)[:240]}")
    for name in key_names:
        key = os.getenv(name, "").strip()
        if key:
            _API_KEY_SOURCE = "Railway/process environment (" + name + ")"
            return key
    try:
        from dotenv import dotenv_values
        file_values = dotenv_values(PROJECT_ROOT / ".env")
        key = ""
        for name in key_names:
            key = str(file_values.get(name, "") or "").strip()
            if key:
                _API_KEY_SOURCE = "project .env (" + name + ")"
                break
    except Exception:
        key = ""
    if key:
        os.environ["GEMINI_API_KEY"] = key
    else:
        _API_KEY_SOURCE = "missing"
    return key


def _voice_log(message):
    """Keep voice-AI diagnostics in Railway stdout only, never the GUI/main chat log."""
    print(message, flush=True)


_AudioSinkBase = voice_recv.AudioSink if voice_recv is not None else object


class _SpeechSink(_AudioSinkBase):
    """Buffer only the /call invoker's PCM speech and split after a pause."""
    def __init__(self, user_id):
        if voice_recv is not None:
            super().__init__()
        self.user_id = int(user_id)
        self.lock = threading.Lock()
        self.buffer = bytearray()
        self.last_speech_at = 0.0
        self.last_activity_at = time.monotonic()
        self.packet_count = 0

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
            if level < 55:
                return
        except (ValueError, OverflowError):
            return
        with self.lock:
            self.packet_count += 1
            self.last_activity_at = time.monotonic()
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
            return chunk if len(chunk) >= 16_000 else None

    def cleanup(self):
        with self.lock:
            self.buffer.clear()


class _MeteredPCMSource(discord.AudioSource):
    """Report whether FFmpeg decoded real, non-silent TTS PCM before Discord sends it."""
    def __init__(self, source):
        self.source = source
        self.byte_count = 0
        self.sample_count = 0
        self.peak = 0
        self.sum_squares = 0

    def read(self):
        pcm = self.source.read()
        if not pcm:
            rms = (self.sum_squares / self.sample_count) ** 0.5 if self.sample_count else 0
            print(f"[voice-ai] TTS PCM meter bytes={self.byte_count} samples={self.sample_count} peak={self.peak} rms={rms:.1f}", flush=True)
            return pcm
        samples = array("h")
        samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
        if samples:
            self.byte_count += len(pcm)
            self.sample_count += len(samples)
            self.peak = max(self.peak, max(abs(sample) for sample in samples))
            self.sum_squares += sum(sample * sample for sample in samples)
        return pcm

    def is_opus(self):
        return False

    def cleanup(self):
        self.source.cleanup()


def _gemini_audio_request(pcm, history, pending_action=None, store=None):
    wav_buffer = BytesIO()
    with wave.open(wav_buffer, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(pcm)
    system = (
        "You are a helpful Discord voice assistant. Understand the caller's spoken English, Bangla, or Banglish, "
        "Carefully transcribe the caller's complete English, Bangla, or Banglish utterance, including repeated short greetings and surrounding words. Never skip a clear 'hi', 'hello', or 'hey' just because it is repeated or followed by 'Meta Bot'. "
        "Answer in clear natural English. Your owner is Rafin; use a friendly, playful, confident conversational style inspired by Rafin, but never claim to be Rafin or impersonate him. "
        "If a question is genuinely funny, react briefly and naturally with 'Haha!' before answering; do not force laughter. "
        "If asked who you are, say you are Meta AI, an AI assistant made of code with no physical body, then explain warmly that you can chat and help. "
        "If the caller says repeated greetings such as 'hi hi Meta Bot', treat them as a greeting even if there are small pauses or filler words. If a greeting comes with a real question/request, greet briefly and still answer the request. "
        "The caller is speaking live in a voice channel. Use action=answer for "
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
    model = os.getenv("VC_AI_MODEL", "gemini-3.8-flash").strip()
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers={
        "x-goog-api-key": _api_key(store), "Content-Type": "application/json",
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


def _gemini_web_answer(heard_text, history, store=None):
    model = os.getenv("VC_AI_MODEL", "gemini-3.8-flash").strip()
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = {
        "systemInstruction": {"parts": [{"text": "Answer in clear, concise, natural English using current web information, and cite the most useful source names. Your owner is Rafin; use a friendly, playful, confident style inspired by Rafin without pretending to be him. If the question is genuinely funny, react briefly with 'Haha!' and then answer. If asked who you are, say you are Meta AI, an AI assistant made of code with no physical body."}]},
        "contents": [{"role": "user", "parts": [{"text": f"Context: {json.dumps(history[-6:], ensure_ascii=False)}\nQuestion: {heard_text}"}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": 500},
    }
    request = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers={
        "x-goog-api-key": _api_key(store), "Content-Type": "application/json",
    }, method="POST")
    with urllib.request.urlopen(request, timeout=45) as response:
        body = json.loads(response.read().decode())
    candidate = body["candidates"][0]
    answer = " ".join(part.get("text", "") for part in candidate["content"].get("parts", []) if part.get("text")).strip()
    sources = []
    for chunk in candidate.get("groundingMetadata", {}).get("groundingChunks", []):
        web = chunk.get("web") or {}
        if web.get("title"):
            sources.append(web["title"])
    if sources:
        answer += " Sources: " + "; ".join(dict.fromkeys(sources[:3]))
    return answer


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
        print(f"[voice-ai] TTS start chars={len(text)} voice={os.getenv('VC_AI_VOICE', 'en-US-AnaNeural')}", flush=True)
        await edge_tts.Communicate(text[:500], os.getenv("VC_AI_VOICE", "en-US-AnaNeural"), rate="+5%", pitch="+8Hz").save(path)
        print(f"[voice-ai] TTS audio ready bytes={os.path.getsize(path)}; sending to Discord", flush=True)
        if not discord.opus.is_loaded():
            opus_library = ctypes.util.find_library("opus") or ("libopus.so.0" if os.name == "posix" else "opus")
            discord.opus.load_opus(opus_library)
        print(f"[voice-ai] Discord Opus encoder loaded={discord.opus.is_loaded()}", flush=True)
        connection = getattr(vc, "_connection", None)
        started_waiting = time.monotonic()
        saw_dave = False
        dave_checked = bool(getattr(vc, "_voice_ai_dave_checked", False))
        while connection is not None and time.monotonic() - started_waiting < 15:
            protocol = int(getattr(connection, "dave_protocol_version", 0) or 0)
            dave_session = getattr(connection, "dave_session", None)
            can_encrypt = bool(getattr(connection, "can_encrypt", False))
            if protocol > 0:
                saw_dave = True
                if can_encrypt:
                    vc._voice_ai_dave_checked = True
                    print(f"[voice-ai] DAVE ready protocol={protocol} session_ready={bool(getattr(dave_session, 'ready', False))}; sending TTS", flush=True)
                    break
            elif dave_checked:
                print("[voice-ai] DAVE status already checked; sending in negotiated legacy mode", flush=True)
                break
            elif not saw_dave and time.monotonic() - started_waiting >= 3:
                vc._voice_ai_dave_checked = True
                print("[voice-ai] no DAVE protocol negotiated after grace period; sending in negotiated legacy mode", flush=True)
                break
            await asyncio.sleep(0.1)
        else:
            if saw_dave:
                raise RuntimeError("Discord DAVE encryption did not become ready; refusing to send inaudible voice audio.")
        done = asyncio.Event()
        loop = asyncio.get_running_loop()
        playback_error = []
        if vc.is_playing():
            vc.stop()
        def _after_play(error):
            if error:
                playback_error.append(error)
                print(f"[voice-ai] Discord audio player callback error: {type(error).__name__}: {error}", flush=True)
            loop.call_soon_threadsafe(done.set)
        source = _MeteredPCMSource(discord.FFmpegPCMAudio(path, options="-vn -af volume=2.0"))
        vc.play(source, after=_after_play)
        await asyncio.wait_for(done.wait(), timeout=60)
        if playback_error:
            raise RuntimeError(f"Discord audio playback failed: {playback_error[0]}")
        print("[voice-ai] TTS playback finished without FFmpeg/player error", flush=True)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def _confirmable_action(guild, caller, result, session):
    action = str(result.get("action", "answer")).lower()
    if action not in {"kick", "ban", "move"}:
        return str(result.get("reply", "Sorry, I didn't understand that. Please say it again."))[:500]
    target_name = str(result.get("target_name", "")).strip()
    if not target_name:
        session["pending_action"] = {"action": action, "phase": "target"}
        return "Sir, tell me the username you want me to " + ("move." if action == "move" else action + ".")
    target = await _find_member(guild, target_name)
    if not target or target.id in {caller.id, APP_CONFIG.admin_user_id} or target == guild.owner or target.bot:
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
        if not target or target.id in {caller.id, APP_CONFIG.admin_user_id} or target == guild.owner or target.bot:
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
        await target.kick(reason=f"Confirmed by voice /call user {caller} ({caller.id})")
        return f"{target.display_name} has been kicked from the server."
    if action == "ban":
        if not me.guild_permissions.ban_members:
            return "I don't have the Ban Members permission."
        await target.ban(reason=f"Confirmed by voice /call user {caller} ({caller.id})", delete_message_seconds=0)
        return f"{target.display_name} has been banned from the server."
    destination = guild.get_channel(pending.get("destination_id"))
    if not destination or not target.voice or not target.voice.channel:
        return "That member is no longer in a voice channel, so I couldn't move them."
    if not me.guild_permissions.move_members:
        return "I don't have the Move Members permission."
    await target.move_to(destination, reason=f"Confirmed by voice /call user {caller} ({caller.id})")
    return f"{target.display_name} has been moved to {destination.name}."


async def _perform_action(guild, caller, result, session):
    action = str(result.get("action", "answer")).lower()
    me = guild.me
    reply = str(result.get("reply", "Sorry, I didn't understand that. Please say it again."))[:500]
    if action in {"kick", "ban", "move"}:
        return await _confirmable_action(guild, caller, result, session)
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
    last_packet_log = 0.0
    last_packet_count = 0
    try:
        while session["active"] and vc.is_connected():
            await asyncio.sleep(0.2)
            now = time.monotonic()
            with sink.lock:
                packet_count = sink.packet_count
                last_activity = sink.last_activity_at
            if now - last_packet_log >= 30:
                _voice_log(f"[voice-ai] receive health guild={guild.name!r} caller={caller.id} speech_packets_total={packet_count} speech_packets_30s={packet_count-last_packet_count} silence_seconds={int(now-last_activity)} connected={vc.is_connected()}.")
                last_packet_count = packet_count
                last_packet_log = now
            if now - last_activity >= IDLE_DISCONNECT_SECONDS:
                session["active"] = False
                _voice_log(f"Voice AI disconnected from {guild.name} after five minutes without caller speech.")
                try:
                    vc.stop_listening()
                    await vc.disconnect(force=True)
                except (discord.ClientException, discord.HTTPException):
                    pass
                break
            pcm = sink.take_after_pause()
            if not pcm:
                continue
            duration = len(pcm) / (48000 * 2 * 2)
            _voice_log(f"[voice-ai] utterance buffered guild={guild.name!r} caller={caller.id} pcm_bytes={len(pcm)} approx_seconds={duration:.2f}; sending audio to Gemini model={os.getenv('VC_AI_MODEL', 'gemini-3.8-flash')}.")
            try:
                result, _sources = await asyncio.to_thread(_gemini_audio_request, pcm, history, session.get("pending_action"), moderator.store)
                heard = str(result.get("heard_text", "")).strip()[:500]
                greeting_only = bool(re.fullmatch(r"[\W_]*(?:(?:hi|hello|hey|namaste|meta\s*bot|meta\s*ai|there)[\W_]*)+", heard, re.I))
                if greeting_only and str(result.get("action", "answer")).lower() == "answer":
                    result["reply"] = "Hi, hello! I'm Meta AI. How can I help you?"
                _voice_log(f"[voice-ai] Gemini audio success guild={guild.name!r} heard={heard[:180]!r} greeting_detected={greeting_only} action={result.get('action', 'answer')} reply_chars={len(str(result.get('reply', '')))}.")
                if session.get("pending_action"):
                    reply = await _advance_pending_action(guild, caller, session, heard)
                else:
                    reply = await _perform_action(guild, caller, result, session)
                    if str(result.get("action", "answer")).lower() == "answer" and heard and _WEB_QUERY.search(heard):
                        await _speak(vc, "Hold on, just a minute.")
                        reply = await asyncio.to_thread(_gemini_web_answer, heard, history, moderator.store)
                if heard:
                    history.extend(["User: " + heard, "Assistant: " + reply[:500]])
                _voice_log(f"[voice-ai] response prepared guild={guild.name!r} reply={reply[:180]!r}; starting TTS playback.")
                await _speak(vc, reply)
            except Exception as exc:
                _voice_log(f"[voice-ai] turn failed guild={guild.name!r} error_type={type(exc).__name__} http_status={getattr(exc, 'code', None)} reason={getattr(exc, 'reason', None)!r} detail={str(exc)[:500]!r}")
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
    """Register the single /call command; it toggles voice AI on/off."""
    api_key = _api_key(moderator.store)
    _voice_log(f"[voice-ai] startup config Gemini key={'present' if api_key else 'missing'} source={_API_KEY_SOURCE!r} (value hidden), model={os.getenv('VC_AI_MODEL', 'gemini-3.8-flash')}, TTS voice={os.getenv('VC_AI_VOICE', 'en-US-AnaNeural')}, idle_disconnect_seconds={IDLE_DISCONNECT_SECONDS}.")
    if voice_recv is None:
        _voice_log(f"[voice-ai] voice receive extension unavailable in {moderator.source} runtime: {_VOICE_RECV_IMPORT_ERROR}. Bot controls can still connect; /call is handled by a runtime with voice receive installed.")
    @bot.tree.command(name="call", description="Start or stop the voice AI chat in your current voice channel")
    @app_commands.guild_only()
    async def chat(interaction: discord.Interaction):
        if voice_recv is None:
            # If GUI and Railway both receive the same command, let the Railway
            # process acknowledge it; this desktop environment lacks the sink.
            if moderator.source == "gui":
                return
            return await interaction.response.send_message(
                "Voice receive is not installed in this bot environment. Install the project requirements and restart the bot.",
                ephemeral=True,
            )
        # GUI and Railway may both receive the same Gateway dispatch. A short
        # shared claim ensures only one process starts/stops the voice session.
        try:
            claimed = await asyncio.to_thread(
                moderator.store.claim_rate_limit,
                "voice_ai_call_dispatch",
                f"{interaction.guild_id}_{interaction.user.id}",
                2,
            )
        except Exception as exc:
            _voice_log(f"[voice-ai] shared /call dispatch claim failed: {type(exc).__name__}: {str(exc)[:200]}")
            claimed = True
        if not claimed:
            return
        if not moderator.vc_ai_allowed(interaction.user):
            return await interaction.response.send_message("You do not have an allowed Voice AI role. GUI-te VC AI Permissions set korte hobe.", ephemeral=True)
        guild = interaction.guild
        active = ACTIVE_CHAT_SESSIONS.get(guild.id)
        if active:
            if active["owner_id"] != interaction.user.id:
                return await interaction.response.send_message("Voice AI is already active in this server; only the member who started it can stop it with /call.", ephemeral=True)
            active["active"] = False
            active["vc"].stop_listening()
            active["task"].cancel()
            await active["vc"].disconnect(force=True)
            ACTIVE_CHAT_SESSIONS.pop(guild.id, None)
            return await interaction.response.send_message("Voice AI stopped and left the voice channel.", ephemeral=True)

        state = getattr(interaction.user, "voice", None)
        if not state or not state.channel:
            return await interaction.response.send_message("Age voice channel-e join koro, tarpor /call dao.", ephemeral=True)
        if not _api_key(moderator.store):
            return await interaction.response.send_message("Gemini API key shared Cloudflare settings, environment, ba local .env kono jaygay paoa jayni. GUI > VC AI Permissions-e key diye Save AI settings to encrypted Cloudflare koro. GUI ar Railway-te same BOT_CONTROL_ID, BOT_ENCRYPTION_KEY, D1/R2 config thakte hobe. Startup log-e key source dekho; secret value log hoy na.", ephemeral=True)
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
            connection = getattr(vc, "_connection", None)
            dave_protocol = getattr(connection, "dave_protocol_version", 0) if connection else 0
            dave_session = getattr(connection, "dave_session", None) if connection else None
            dave_ready = bool(getattr(connection, "can_encrypt", False)) if connection else False
            _voice_log(f"[voice-ai] connected guild={guild.name!r} channel={state.channel.name!r} guild_id={guild.id} caller={interaction.user.id} dave_protocol={dave_protocol} dave_ready={dave_ready} dave_session_ready={bool(getattr(dave_session, 'ready', False))}.")
            sink = _SpeechSink(interaction.user.id)
            vc.listen(sink)
            _voice_log(f"[voice-ai] receive listener started guild={guild.name!r} caller_only=true input_format=48kHz_stereo_s16le mean_abs_threshold=55 min_utterance_bytes=16000.")
            session = {"owner_id": interaction.user.id, "active": True, "vc": vc, "sink": sink, "last_activity": time.monotonic()}
            task = asyncio.create_task(_voice_chat_worker(moderator, guild, interaction.user, vc, sink, session))
            session["task"] = task
            ACTIVE_CHAT_SESSIONS[guild.id] = session
            await interaction.followup.send(
                f"Joined **{state.channel.name}** and started listening to your voice. Speak naturally; run /call again to stop. I will leave after 5 minutes without speech. Your speech is sent to Gemini to generate replies.",
                ephemeral=True,
            )
            try:
                await _speak(vc, "Hi, hello, namaste! I'm Meta AI, ready to chat. What would you like to talk about?")
            except Exception as exc:
                _voice_log(f"[voice-ai] initial greeting failed guild={guild.name!r} error_type={type(exc).__name__} detail={str(exc)[:500]!r}")
        except Exception as exc:
            _voice_log(f"[voice-ai] start failed guild={guild.name!r} error_type={type(exc).__name__} http_status={getattr(exc, 'code', None)} detail={str(exc)[:700]!r}")
            await interaction.followup.send("Could not start voice chat. Check voice receive dependency, bot permissions, and Railway logs.", ephemeral=True)


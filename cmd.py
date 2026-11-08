"""Gemini powered /call assistant and its narrowly scoped Discord actions."""
import asyncio
import json
import os
import re
import tempfile
import urllib.request

import discord
from discord import app_commands
from app_config import APP_CONFIG


_WEB_QUERY = re.compile(r"\b(search|google|internet|web|latest|current|today|right now|news|weather|price|who won|look up|find online)\b", re.I)


def _gemini_request(request_text, target, destination, text_channel):
    """Ask Gemini to classify a request and draft either an answer or a text post."""
    system = (
        "You are a helpful English voice assistant for a Discord server. Use action=answer for normal questions, "
        "action=kick only for an explicit kick request, action=move only for an explicit voice move request, "
        "and action=send_message only when the user clearly asks you to post a message in a server text channel. "
        "Never execute an unrelated Discord command: explain it in reply instead. Never invent a target, channel, "
        "or message. For send_message, provide the requested channel name and exact message text. Do not add mentions. "
        "Keep spoken replies concise, natural, and in English."
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps({
            "request": request_text,
            "selected_target": target.display_name if target else None,
            "selected_voice_destination": destination.name if destination else None,
            "selected_text_channel": text_channel.name if text_channel else None,
        })}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "action": {"type": "STRING", "enum": ["answer", "kick", "move", "send_message"]},
                    "reply": {"type": "STRING"},
                    "channel_name": {"type": "STRING"},
                    "message_text": {"type": "STRING"},
                },
                "required": ["action", "reply", "channel_name", "message_text"],
            },
            "maxOutputTokens": 500,
        },
    }
    # Google Search grounding is used for requests that ask for live/current web information.
    if _WEB_QUERY.search(request_text):
        payload["tools"] = [{"google_search": {}}]
    model = os.getenv("VC_AI_MODEL", "gemini-3.8-flash").strip()
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=35) as response:
        body = json.loads(response.read().decode())
    candidate = body["candidates"][0]
    result = json.loads(candidate["content"]["parts"][0]["text"])
    sources = []
    for chunk in candidate.get("groundingMetadata", {}).get("groundingChunks", []):
        web = chunk.get("web") or {}
        uri = web.get("uri")
        if uri and all(item["url"] != uri for item in sources):
            sources.append({"title": web.get("title") or uri, "url": uri})
    return result, sources


def _find_text_channel(guild, chosen, requested_name):
    if chosen:
        return chosen
    key = re.sub(r"[^a-z0-9]", "", requested_name.casefold())
    if not key:
        return None
    channels = [channel for channel in guild.text_channels if re.sub(r"[^a-z0-9]", "", channel.name.casefold()) == key]
    if len(channels) == 1:
        return channels[0]
    partial = [channel for channel in guild.text_channels if key in re.sub(r"[^a-z0-9]", "", channel.name.casefold())]
    return partial[0] if len(partial) == 1 else None


def register(moderator, bot):
    """Register the only Gemini assistant command. Discord actions stay in this module."""
    @bot.tree.command(name="call", description="Join your voice channel and answer an English request")
    @app_commands.guild_only()
    @app_commands.describe(
        request="Ask anything, request a Discord action, or ask for current web information",
        target="Member for a kick or move request",
        destination="Voice channel for a move request",
        text_channel="Text channel where you want the bot to post",
    )
    async def call(
        interaction: discord.Interaction,
        request: str = "Join the voice channel and greet everyone in English.",
        target: discord.Member = None,
        destination: discord.VoiceChannel = None,
        text_channel: discord.TextChannel = None,
    ):
        if not moderator.vc_ai_allowed(interaction.user):
            return await interaction.response.send_message("You do not have an allowed VC AI role. GUI-te VC AI Permissions set korte hobe.", ephemeral=True)
        state = getattr(interaction.user, "voice", None)
        if not state or not state.channel:
            return await interaction.response.send_message("/call use korte hole age voice channel-e join koro.", ephemeral=True)
        if not os.getenv("GEMINI_API_KEY", "").strip():
            return await interaction.response.send_message("AI setup baki: Railway/.env-e GEMINI_API_KEY set korte hobe.", ephemeral=True)
        if len(request) > 1000:
            return await interaction.response.send_message("Request 1000 character-er moddhe rakho.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            guild = interaction.guild
            me = guild.me
            channel_perms = state.channel.permissions_for(me) if me else None
            if not channel_perms or not channel_perms.connect or not channel_perms.speak:
                return await interaction.followup.send("I need Connect and Speak permissions in your voice channel.", ephemeral=True)
            voice_client = guild.voice_client
            if voice_client and voice_client.is_connected():
                if voice_client.channel != state.channel:
                    await voice_client.move_to(state.channel)
            else:
                voice_client = await state.channel.connect()

            result, sources = await asyncio.to_thread(_gemini_request, request, target, destination, text_channel)
            action = str(result.get("action", "answer")).lower()
            if action not in {"answer", "kick", "move", "send_message"}:
                action = "answer"
            if action in {"kick", "move"}:
                if not target or target.bot or target.id == interaction.user.id or target.id == APP_CONFIG.admin_user_id or target == guild.owner:
                    reply = "Select a valid target member for that action."
                elif not me or target.top_role >= me.top_role:
                    reply = "I cannot act on that member because of the Discord role hierarchy."
                elif action == "kick":
                    if not me.guild_permissions.kick_members:
                        reply = "I do not have the Kick Members permission."
                    else:
                        await target.kick(reason=f"Requested via /call by {interaction.user} ({interaction.user.id})")
                        reply = f"{target.display_name} has been kicked from the server."
                elif not destination or destination.guild.id != guild.id:
                    reply = "Select a destination voice channel for the move request."
                elif not me.guild_permissions.move_members:
                    reply = "I do not have the Move Members permission."
                else:
                    await target.move_to(destination, reason=f"Requested via /call by {interaction.user} ({interaction.user.id})")
                    reply = f"{target.display_name} has been moved to {destination.name}."
            elif action == "send_message":
                channel = _find_text_channel(guild, text_channel, str(result.get("channel_name", "")))
                content = str(result.get("message_text", "")).strip()
                if not channel or not content:
                    reply = "I couldn't identify the channel or exact message. Select the text_channel option and tell me exactly what to post."
                elif len(content) > 1800:
                    reply = "That message is too long to post safely. Keep it under 1800 characters."
                elif not channel.permissions_for(me).send_messages:
                    reply = f"I do not have permission to send messages in #{channel.name}."
                else:
                    await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
                    reply = f"I posted your message in #{channel.name}."
            else:
                reply = str(result.get("reply", "I understood. What would you like to know?"))[:500]

            import edge_tts
            audio_file = tempfile.NamedTemporaryFile(prefix="call-", suffix=".mp3", delete=False)
            audio_path = audio_file.name
            audio_file.close()
            try:
                await edge_tts.Communicate(reply[:500], os.getenv("VC_AI_VOICE", "en-US-AnaNeural"), rate="+5%", pitch="+8Hz").save(audio_path)
                done = asyncio.Event()
                loop = asyncio.get_running_loop()
                voice_client.play(discord.FFmpegPCMAudio(audio_path), after=lambda _error: loop.call_soon_threadsafe(done.set))
                await asyncio.wait_for(done.wait(), timeout=60)
            finally:
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass

            response = f"Voice reply: {reply}"
            if sources:
                response += "\n\nWeb sources:\n" + "\n".join(f"• [{source['title']}]({source['url']})" for source in sources[:5])
            await interaction.followup.send(response[:1900], ephemeral=True)
        except Exception as exc:
            moderator.log(f"/call failed: {exc}")
            await interaction.followup.send("/call-e problem hoyeche. Gemini key/quota, VC permissions, voice packages ebong FFmpeg check koro.", ephemeral=True)


import asyncio
import copy
import functools
import io
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from config.app_config import APP_CONFIG
from features.leaderboard import render_leaderboard_png
from features.formatters import image_extension
from features.chat import ChatSendModal
from features.tickets import (
    DEFAULT_TICKET_CLOSE_NOTE,
    TicketCreateView,
    TicketModal,
    TicketCancelView,
    TicketCancelConfirmView,
    TicketStaffActionView,
    TicketTransferView,
    TicketStaffNoteModal,
    TicketRatingView,
    TicketRatingModal,
)
from features.birthdays import BirthdayDashboardView, BirthdayChangeConfirmView, BirthdayModal
from features.voice import TempVoiceNameModal, TempVoiceLimitModal, TempVoiceControlView, TempVoiceInviteModal
from features.move import MoveActiveVCView, DragMemberView, DragTargetView
from features.help import HELP_PAGES, make_help_embeds, HelpPagerView
from features.anti_spam import BotSpamGuard
from features.custom_slash import CustomSlashCommands
from features.word_block import WordBlockFeature

LINK_PATTERN = re.compile(r"(https?://\S+|www\.\S+|discord\.gg/\S+|\S+\.(com|net|org|io|xyz|gg)\S*)", re.I)
DISCORD_INVITE_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+", re.I)
# Administrator is intentional: temporary voice rooms require channel management and moving members.
INVITE_PERMISSIONS = discord.Permissions(administrator=True)
# The configured admin is protected from every Kick/Ban action issued by this bot.
# This does not affect what Discord human moderators can do manually.
PROTECTED_USER_IDS = {APP_CONFIG.admin_user_id}
BANGLADESH_TIMEZONE = "Asia/Dhaka"
DEFAULT_NOTIFICATION_TIMEZONE = BANGLADESH_TIMEZONE
# datetime.weekday(): Monday=0 ... Sunday=6. Locale-independent (unlike strftime("%a")).
WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
EXTERNAL_GUILD_OWNER_NOTICE = (
    f"Hello! {APP_CONFIG.bot_app_name} is configured for its designated Discord server and is not available here. "
    "This bot will now leave this server."
)


class ModeratorBot:
    """Coordinate Discord events and feature modules for GUI and Railway."""
    def __init__(self, store, source, log=print, event=None):
        self.store, self.source, self.log = store, source, log
        self.event = event or (lambda *_: None)
        self.settings = store.get_settings()
        self.applied_settings = copy.deepcopy(self.settings)
        self.bot = None
        self.loop = None
        self.last_action, self.actions = {}, []
        self.counting_locks = {}
        self.open_ticket_channels = {}
        self.dashboard_views_registered = set()
        self.protected_user_ids = set(PROTECTED_USER_IDS)
        self.custom_slash_commands = CustomSlashCommands(self.log)
        self.custom_commands_registered = copy.deepcopy(
            self.settings.get("custom_commands", {})
        )
        self.word_block = WordBlockFeature(self)
        self.custom_command_sync_lock = asyncio.Lock()
        # `on_ready` may be called again after a Discord gateway resume. The
        # GUI inventory should be refreshed each time, but background loops must
        # only exist once or a reconnect would create duplicate workers.
        self.background_tasks_started = False
        self.bot_spam_guard = BotSpamGuard()

    async def ban_spamming_bot(self, message, reason):
        guild, member = message.guild, message.author
        ban_reason = f"{APP_CONFIG.bot_app_name} anti-spam: {reason}"
        banned = False
        try:
            await guild.ban(member, reason=ban_reason, delete_message_seconds=0)
            banned = True
        except discord.Forbidden:
            self.log(f"Could not ban suspected spam bot {member} ({member.id}) in {guild.name}: missing ban permission or role hierarchy.")
        except discord.HTTPException as exc:
            self.log(f"Could not ban suspected spam bot {member} ({member.id}) in {guild.name}: {exc}")
        admin = guild.get_member(APP_CONFIG.admin_user_id)
        if admin is None:
            try:
                admin = await self.bot.fetch_user(APP_CONFIG.admin_user_id)
            except (discord.HTTPException, discord.NotFound) as exc:
                self.log(f"Could not find anti-spam administrator {APP_CONFIG.admin_user_id}: {exc}")
        if admin is not None:
            status = "banned" if banned else "could not be banned; check bot permissions and role hierarchy"
            try:
                await admin.send(
                    f"{APP_CONFIG.bot_app_name} anti-spam alert\n"
                    f"Bot: {member} ({member.id})\nServer: {guild.name} ({guild.id})\n"
                    f"Channel: #{getattr(message.channel, 'name', 'unknown')}\n"
                    f"Reason: {reason}\nAction: {status}."
                )
            except discord.HTTPException as exc:
                self.log(f"Could not DM anti-spam alert to administrator {APP_CONFIG.admin_user_id}: {exc}")
        else:
            self.log(f"Anti-spam alert for {member} ({member.id}) in {guild.name}: {reason}; {ban_reason}.")

    async def notify_external_guild_owner(self, guild):
        """Post one notice in an unauthorized guild, then leave it."""
        if guild.id == APP_CONFIG.allowed_guild_id:
            return False
        try:
            claimed = await asyncio.to_thread(self.store.claim_external_guild_notice, guild.id)
        except Exception as exc:
            self.log(f"Could not claim external-server notice for {guild.name}: {exc}")
            claimed = False
        if claimed:
            candidates = [guild.system_channel] + list(guild.text_channels)
            for channel in candidates:
                if channel is None:
                    continue
                try:
                    await channel.send(EXTERNAL_GUILD_OWNER_NOTICE, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
                    self.log(f"Posted {APP_CONFIG.bot_app_name} server notice in #{channel.name} for {guild.name} ({guild.id}).")
                    break
                except (discord.Forbidden, discord.HTTPException):
                    continue
        try:
            await guild.leave()
            self.log(f"Left unauthorized server {guild.name} ({guild.id}).")
            return True
        except discord.HTTPException as exc:
            self.log(f"Could not leave unauthorized server {guild.name} ({guild.id}): {exc}")
            return False
    def _role_allowed(self, member, setting_name):
        """Check one independently managed per-guild role permission."""
        if not member or not getattr(member, "guild", None):
            return False
        allowed = set(map(str, self.settings.get(setting_name, {}).get(str(member.guild.id), [])))
        member_roles = {str(role.id) for role in getattr(member, "roles", [])}
        return bool(allowed & member_roles)

    def chat_allowed(self, member):
        """Permission to use the /chat message feature."""
        return self._role_allowed(member, "chat_allowed_roles")

    def link_allowed(self, member):
        """Permission to share links in normal server messages."""
        return self._role_allowed(member, "link_allowed_roles")

    def drag_allowed(self, member):
        """Permission to use the /drag voice-member mover."""
        return self._role_allowed(member, "drag_allowed_roles")

    def role_is_manageable(self, guild, role):
        """Whether this bot can safely add or remove ``role`` in this guild."""
        me = guild.me
        return bool(
            role
            and not role.is_default()
            and not role.managed
            and me
            and me.guild_permissions.manage_roles
            and role < me.top_role
        )

    def unmanageable_role_message(self, guild, roles):
        names = ", ".join(role.name for role in roles[:5])
        suffix = " â€¦" if len(roles) > 5 else ""
        return (
            f"Cannot manage Auto Role(s): {names}{suffix}. Move the bot's highest role above "
            "these roles in Server Settings â†’ Roles, and ensure it has Manage Roles."
        )

    def guild_info(self, guild):
        # The GUI uses the manageable flag only for Auto Roles; permission lists retain every role.
        return {"id": guild.id, "name": guild.name, "member_count": guild.member_count,
                "text_channels": [{"id": str(c.id), "name": c.name} for c in guild.text_channels],
                "voice_channels": [{"id": str(c.id), "name": c.name} for c in guild.voice_channels],
                "roles": [{"id": str(r.id), "name": r.name, "manageable": self.role_is_manageable(guild, r)} for r in guild.roles if not r.is_default()],
                "members": [{"id": str(m.id), "name": str(m), "display_name": m.display_name, "is_admin": m.id == APP_CONFIG.admin_user_id} for m in guild.members if not m.bot]}

    def configured_timezone(self, config):
        """Return a timezone that works on both the desktop GUI and Railway.

        the slim Linux container can occasionally be missing the system IANA zone
        database. Bangladesh time is fixed at UTC+6 (it has no DST), so handle it
        directly instead of letting a missing ``Asia/Dhaka`` entry break every
        scheduled background tick.
        """
        # Ignore legacy per-entry PC/Railway timezones. Old UTC records made an
        # 8 PM Bangladesh schedule fire at 2 AM and midnight birthdays at 6 AM.
        return timezone(timedelta(hours=6), name=BANGLADESH_TIMEZONE)
    def temp_lobby_id(self, guild_id):
        config = self.settings.get("temp_voice_lobbies", {}).get(str(guild_id), {})
        return str(config.get("channel_id", ""))

    def level_config(self, guild_id):
        """Return safe per-server leveling settings shared from the desktop GUI."""
        config = self.settings.get("leveling", {}).get(str(guild_id), {})
        return {
            "enabled": bool(config.get("enabled", False)),
            "channel_id": str(config.get("channel_id", "")),
            "xp_per_message": max(1, min(100, int(config.get("xp_per_message", 15)))),
            "cooldown_seconds": max(5, min(3600, int(config.get("cooldown_seconds", 60)))),
            "level_base": max(10, min(10000, int(config.get("level_base", 100)))),
            "level_multiplier": max(1.0, min(3.0, float(config.get("level_multiplier", 1.5)))),
            # Voice-call XP: members actively in a voice channel earn XP on their own
            # timer, independent of the text-message cooldown, but it adds to the same
            # level. Defaults are on whenever leveling is enabled.
            "voice_xp_amount": max(1, min(100, int(config.get("voice_xp_amount", 10)))),
            "voice_xp_interval_seconds": max(30, min(1800, int(config.get("voice_xp_interval_seconds", 60)))),
            "voice_xp_require_others": bool(config.get("voice_xp_require_others", True)),
            "voice_xp_ignore_deafened": bool(config.get("voice_xp_ignore_deafened", True)),
        }

    def default_role_ids(self, guild_id):
        """Return the configured auto-role ids for a server (strings)."""
        return list(self.settings.get("default_roles", {}).get(str(guild_id), []))

    def managed_default_role_ids(self, guild_id):
        active = self.default_role_ids(guild_id)
        return list(dict.fromkeys(map(str, self.settings.get("default_role_managed_ids", {}).get(str(guild_id), active))))

    async def apply_default_roles(self, member):
        """Give a newly-joined member their server's configured default role(s).

        Safe to call even if nothing is configured, a role was deleted, or the
        member already has some of the roles â€” those are simply skipped.
        """
        role_ids = self.default_role_ids(member.guild.id)
        if not role_ids:
            return
        roles = [member.guild.get_role(int(rid)) for rid in role_ids if str(rid).isdigit()]
        blocked = [role for role in roles if role and not self.role_is_manageable(member.guild, role)]
        if blocked:
            self.log(self.unmanageable_role_message(member.guild, blocked))
        roles = [role for role in roles if self.role_is_manageable(member.guild, role)]
        missing = [role for role in roles if role and role not in member.roles]
        if not missing:
            return
        try:
            await member.add_roles(*missing, reason="Default role on join")
            self.log(f"Applied default role(s) to {member} in {member.guild.name}.")
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.log(f"Could not add default role(s) to {member}: {exc}")

    async def verify_guild_member(self, guild_id, user_id):
        """Fetch and validate a human member from an ID entered in the GUI."""
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            raise RuntimeError("That server is not available to this GUI bot.")
        if not str(user_id).isdigit():
            raise ValueError("Enter a numeric Discord user ID.")
        try:
            member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
        except discord.NotFound as exc:
            raise RuntimeError("That user is not a member of the selected server.") from exc
        if member.bot:
            raise RuntimeError("Bot accounts cannot be selected here.")
        return member

    async def apply_default_roles_to_member_id(self, guild_id, user_id):
        """Verify one entered user ID and immediately apply saved Auto Roles."""
        role_ids = self.default_role_ids(guild_id)
        if not role_ids:
            raise RuntimeError("Save at least one Auto Role first.")
        member = await self.verify_guild_member(guild_id, user_id)
        roles = [member.guild.get_role(int(role_id)) for role_id in role_ids if str(role_id).isdigit()]
        roles = [role for role in roles if role]
        if not roles:
            raise RuntimeError("The saved Auto Role no longer exists in this server.")
        blocked = [role for role in roles if not self.role_is_manageable(member.guild, role)]
        if blocked:
            raise RuntimeError(self.unmanageable_role_message(member.guild, blocked))
        missing = [role for role in roles if role not in member.roles]
        if missing:
            try:
                await member.add_roles(*missing, reason="Auto Role applied by verified GUI member ID")
            except (discord.Forbidden, discord.HTTPException) as exc:
                raise RuntimeError(f"Could not add Auto Role(s): {exc}") from exc
            self.log(f"Applied saved Auto Role(s) to verified member {member} in {member.guild.name}.")
        return {"id": str(member.id), "name": str(member), "display_name": member.display_name,
                "already_assigned": not bool(missing)}

    async def apply_default_roles_bulk(self, guild_id, progress_callback=None):
        """Backfill the configured default role(s) onto every existing member.

        Used by the GUI's "Apply to all existing members" button. `progress_callback`,
        if given, is called as progress_callback(done, total) after each member is
        processed so the caller (e.g. a Tk progress bar) can show a live percentage.
        Returns (updated_count, total_count, added_count, removed_count, skipped_count).
        """
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            raise RuntimeError("That server was not found (is the bot still in it?).")
        role_ids = set(self.default_role_ids(guild_id)); managed_ids = set(self.managed_default_role_ids(guild_id))
        if not managed_ids: raise RuntimeError("Save at least one Auto Role first, then apply the change.")
        requested_add = [role for role in (guild.get_role(int(rid)) for rid in role_ids if rid.isdigit()) if role]
        requested_remove = [role for role in (guild.get_role(int(rid)) for rid in (managed_ids - role_ids) if rid.isdigit()) if role]
        blocked = [role for role in requested_add + requested_remove if not self.role_is_manageable(guild, role)]
        if blocked:
            # Saved settings can outlive a role-hierarchy change or contain
            # integration/bot roles. Skip those permanently unassignable roles
            # instead of failing the entire backfill for every valid role.
            self.log(self.unmanageable_role_message(guild, blocked) + " Skipping them for this run.")
        blocked_ids = {role.id for role in blocked}
        add_roles = [role for role in requested_add if role.id not in blocked_ids]
        remove_roles = [role for role in requested_remove if role.id not in blocked_ids]
        await guild.chunk(cache=True)
        members = [member for member in guild.members if not member.bot]
        total = len(members)
        updated = added = removed = skipped = 0
        for index, member in enumerate(members, start=1):
            missing = [role for role in add_roles if role not in member.roles]
            obsolete = [role for role in remove_roles if role in member.roles]
            if missing or obsolete:
                try:
                    if missing:
                        await member.add_roles(*missing, reason="Auto Roles bulk apply"); added += len(missing)
                    if obsolete:
                        await member.remove_roles(*obsolete, reason="Auto Roles bulk removal"); removed += len(obsolete)
                    updated += 1
                except discord.Forbidden as exc:
                    skipped += 1
                    self.log(f"Could not reconcile Auto Roles for {member}: Discord denied the update ({exc.code}).")
                except discord.HTTPException as exc:
                    skipped += 1
                    self.log(f"Could not reconcile Auto Roles for {member}: {exc}")
                await asyncio.sleep(0.35)
            if progress_callback: progress_callback(index, total)
        return updated, total, added, removed, skipped

    async def award_xp(self, guild, config, member, xp, cooldown_seconds, source, fallback_channel=None):
        """Shared XP grant + level-up announcement used by both text and voice activity.

        Cloudflare R2's transaction in `award_activity_xp` makes this safe even if the GUI
        and Railway worker briefly overlap: only one award per cooldown window per
        source is ever applied, so XP and levels stay perfectly in sync between them.
        """
        avatar_url = str(member.display_avatar.url)
        try:
            result = await asyncio.to_thread(
                self.store.award_activity_xp, guild.id, member.id, str(member), avatar_url,
                xp, cooldown_seconds, config["level_base"], config["level_multiplier"], source,
            )
        except Exception as exc:
            self.log(f"Could not award {source} XP: {exc}")
            return
        if not result or not result["leveled_up"]:
            return
        channel = guild.get_channel(int(config["channel_id"])) if config["channel_id"].isdigit() else None
        if (channel is None or not hasattr(channel, "send")) and fallback_channel is not None:
            channel = fallback_channel
        if channel is None or not hasattr(channel, "send"):
            return
        embed = discord.Embed(
            title="\U0001f389 Level Up!",
            description=f"Congratulations {member.mention}!\nYou reached level **{result['level']}**.",
            colour=discord.Colour.gold(),
        )
        embed.set_thumbnail(url=avatar_url)
        footer = "Keep chatting to earn XP" if source == "text" else "Keep talking in voice to earn XP"
        embed.set_footer(text=f"{guild.name} â€¢ {footer}")
        try:
            await channel.send(embed=embed)
            self.log(f"{member} reached level {result['level']} in {guild.name}.")
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.log(f"Could not send level-up message: {exc}")

    async def handle_counting_message(self, message):
        """Validate 1, 2, 3â€¦ in the configured channel with a shared Cloudflare R2 state."""
        configured_channel = str(self.settings.get("counting_channels", {}).get(str(message.guild.id), ""))
        if configured_channel != str(message.channel.id):
            return
        text = message.content.strip()
        # Accept both English and Bangla digits. Normalize before comparing.
        # before comparing so a valid reset "1" is treated exactly like "1".
        normalized_text = text.translate(str.maketrans("\u09e6\u09e7\u09e8\u09e9\u09ea\u09eb\u09ec\u09ed\u09ee\u09ef", "0123456789"))
        valid_number = bool(re.fullmatch(r"[0-9]+", normalized_text)) and str(int(normalized_text)) == normalized_text
        submitted = int(normalized_text) if valid_number else -1
        lock = self.counting_locks.setdefault((message.guild.id, message.channel.id), asyncio.Lock())
        async with lock:
            try:
                result = await asyncio.to_thread(
                    self.store.submit_counting_number, message.guild.id, message.channel.id,
                    message.author.id, message.id, submitted,
                )
            except Exception as exc:
                self.log(f"Counting check failed in {message.guild.name}: {exc}")
                return
        if result.get("duplicate"):
            return
        if result["correct"]:
            try:
                await message.add_reaction("\u2705")
            except (discord.Forbidden, discord.HTTPException) as exc:
                self.log(f"Could not react to counting message in {message.guild.name}: {exc}")
            self.log(f"Counting: {message.author} correctly sent {submitted} in {message.guild.name}.")
            return
        # Show the invalid attempt briefly, then remove it so the counting channel
        # keeps only valid progress messages.
        try:
            await message.add_reaction("\u274c")
            await asyncio.sleep(2)
            await message.delete()
        except discord.NotFound:
            pass  # The other shared worker already removed this invalid message.
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.log(f"Could not mark/remove wrong counting message in {message.guild.name}: {exc}")
        self.log(f"Counting kept at {result['expected']} in {message.guild.name}: {message.author} sent {text!r}.")
    async def handle_level_activity(self, message):
        """Award bounded, cooldown-protected XP for a meaningful text message."""
        config = self.level_config(message.guild.id)
        if not config["enabled"]:
            return
        # Meaningful text gets a small length bonus; repeated rapid messages get no XP.
        text = message.content.strip()
        if len(text) < 3:
            return
        xp = min(config["xp_per_message"] + len(text) // 40, config["xp_per_message"] + 10)
        await self.award_xp(
            message.guild, config, message.author, xp, config["cooldown_seconds"], "text",
            fallback_channel=message.channel,
        )

    async def voice_xp_tick(self):
        """Background loop: award XP to members actively on a voice call.

        Runs every 15 seconds, but each member's own `voice_xp_interval_seconds`
        cooldown (enforced atomically in Cloudflare R2) decides how often they actually
        earn XP, so this is safe to run from both the GUI and Railway.
        """
        while not self.bot.is_closed():
            await asyncio.sleep(15)
            try:
                await self.award_voice_xp_for_all_guilds()
            except Exception as exc:
                self.log(f"Voice XP tick failed: {exc}")

    async def award_voice_xp_for_all_guilds(self):
        for guild in self.bot.guilds:
            config = self.level_config(guild.id)
            if not config["enabled"]:
                continue
            afk_channel_id = guild.afk_channel.id if guild.afk_channel else None
            for channel in guild.voice_channels:
                if afk_channel_id and channel.id == afk_channel_id:
                    continue
                humans = [member for member in channel.members if not member.bot]
                if not humans:
                    continue
                # Guard against a member sitting alone in an empty room to farm XP.
                if config["voice_xp_require_others"] and len(humans) < 2:
                    continue
                for member in humans:
                    voice_state = member.voice
                    if config["voice_xp_ignore_deafened"] and voice_state and (voice_state.self_deaf or voice_state.deaf):
                        continue
                    await self.award_xp(
                        guild, config, member, config["voice_xp_amount"],
                        config["voice_xp_interval_seconds"] * 3, "voice",
                    )

    async def role_notification_tick(self):
        """Background loop: fire scheduled role-ping notifications at their configured time.

        Runs every 20 seconds and checks each server's saved schedule against the
        the timezone saved with that notification. The GUI detects the operator's local timezone and Railway uses the same saved timezone. A Cloudflare R2 transaction makes sure the reminder is
        sent exactly once per day even if the GUI and Railway both happen to check in
        the same minute.
        """
        while not self.bot.is_closed():
            await asyncio.sleep(20)
            try:
                await self.check_role_notifications()
            except Exception as exc:
                self.log(f"Role notification tick failed: {exc}")

    async def check_role_notifications(self):
        for guild_id_str, entries in self.settings.get("role_notifications", {}).items():
            if not entries:
                continue
            guild = self.bot.get_guild(int(guild_id_str))
            if not guild:
                continue
            for entry in entries:
                now = datetime.now(self.configured_timezone(entry))
                current_hm, today = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
                today_weekday = WEEKDAY_CODES[now.weekday()]
                if not entry.get("enabled", True) or entry.get("time") != current_hm:
                    continue
                # Scheduling: a specific one-time "date" always wins over weekday
                # selection. Without a date, "days" is either ["everyday"] or a list
                # of weekday codes (e.g. ["mon", "wed"]); missing/empty "days" means
                # "everyday" for backward compatibility with entries saved before
                # this scheduling feature existed.
                entry_date = (entry.get("date") or "").strip()
                if entry_date:
                    if entry_date != today:
                        continue
                else:
                    days = entry.get("days") or ["everyday"]
                    if "everyday" not in days and today_weekday not in days:
                        continue
                entry_id = entry.get("id")
                channel_id = entry.get("channel_id")
                role_ids = entry.get("role_ids") or []
                if not entry_id or not channel_id or not role_ids:
                    continue
                channel = guild.get_channel(int(channel_id))
                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    continue
                roles = [guild.get_role(int(role_id)) for role_id in role_ids]
                roles = [role for role in roles if role]
                if not roles:
                    continue
                try:
                    claimed = await asyncio.to_thread(self.store.claim_role_notification_fire, entry_id, today)
                except Exception as exc:
                    self.log(f"Could not check role notification fire-state: {exc}")
                    continue
                if not claimed:
                    continue
                mentions = " ".join(role.mention for role in roles)
                # Empty custom text deliberately means a clean role-only ping.
                text = (entry.get("message") or "").strip()
                embed = discord.Embed(title="\u2728 Server Announcement", description=text or "A new server announcement is here!", colour=discord.Colour.blurple())
                file = None
                try:
                    image = await asyncio.to_thread(self.store.get_notification_image, entry.get("image_id"))
                    if image:
                        content_type, data = image
                        extension = image_extension(content_type, data)
                        filename = f"notification.{extension}"
                        file = discord.File(io.BytesIO(data), filename=filename)
                        embed.set_image(url=f"attachment://{filename}")
                except Exception as exc:
                    self.log(f"Could not load role notification image: {exc}")
                try:
                    await channel.send(mentions or None, embed=embed, file=file,
                                       allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False))
                    self.log(f"Sent scheduled role notification in {guild.name} #{channel.name}.")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    self.log(f"Could not send role notification in {guild.name}: {exc}")

    async def movie_announcement_tick(self):
        """Check one-time movie announcements in their saved timezone."""
        while not self.bot.is_closed():
            await asyncio.sleep(20)
            try:
                await self.check_movie_announcements()
            except Exception as exc:
                self.log(f"Movie announcement tick failed: {exc}")

    async def check_movie_announcements(self):
        for guild_id_str, config in self.settings.get("movie_announcements", {}).items():
            if not config or not config.get("enabled", True):
                continue
            now = datetime.now(self.configured_timezone(config))
            today, current_hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
            if config.get("date") != today or config.get("time") != current_hm:
                continue
            entry_id, channel_id = config.get("id"), config.get("channel_id")
            if not entry_id or not channel_id:
                continue
            guild = self.bot.get_guild(int(guild_id_str))
            channel = guild and guild.get_channel(int(channel_id))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            roles = [guild.get_role(int(role_id)) for role_id in config.get("role_ids", [])]
            mentions = " ".join(role.mention for role in roles if role)
            try:
                claimed = await asyncio.to_thread(self.store.claim_movie_announcement_fire, entry_id, today)
            except Exception as exc:
                self.log(f"Could not claim movie announcement: {exc}")
                continue
            if not claimed:
                continue
            title = (config.get("title") or "Movie Night").strip()
            embed = discord.Embed(title=f"\U0001f3ac {title}", description=(config.get("message") or "Join us for movie time!").strip(), colour=discord.Colour.blurple())
            embed.add_field(name="Movie time", value=now.strftime("%I:%M %p %Z"), inline=False)
            file = None
            try:
                image = await asyncio.to_thread(self.store.get_notification_image, config.get("image_id"))
                if image:
                    content_type, data = image
                    extension = image_extension(content_type, data)
                    filename = f"movie.{extension}"
                    file = discord.File(io.BytesIO(data), filename=filename)
                    embed.set_image(url=f"attachment://{filename}")
            except Exception as exc:
                self.log(f"Could not load movie poster: {exc}")
            try:
                await channel.send(mentions or None, embed=embed, file=file, allowed_mentions=discord.AllowedMentions(users=False, everyone=False, roles=True))
                self.log(f"Sent movie announcement in {guild.name} #{channel.name}.")
            except (discord.Forbidden, discord.HTTPException) as exc:
                self.log(f"Could not send movie announcement in {guild.name}: {exc}")
    async def birthday_tick(self):
        """Background loop: check for member birthdays roughly once a minute.

        Fires exactly once per member per calendar day in the saved timezone
        thanks to the Cloudflare R2 claim in `check_birthdays`, so GUI and Railway never
        double-post the same wish even if both are online at once.
        """
        while not self.bot.is_closed():
            await asyncio.sleep(30)
            try:
                await self.check_birthdays()
            except Exception as exc:
                self.log(f"Birthday tick failed: {exc}")

    async def check_birthdays(self):
        for guild_id_str, config in self.settings.get("birthdays", {}).items():
            today = datetime.now(self.configured_timezone(config))
            date_str = today.strftime("%Y-%m-%d")
            wish_channel_id = str(config.get("wish_channel_id") or config.get("channel_id", ""))
            if not wish_channel_id:
                continue
            guild = self.bot.get_guild(int(guild_id_str))
            if not guild:
                continue
            channel = guild.get_channel(int(wish_channel_id))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            try:
                birthdays = await asyncio.to_thread(self.store.list_birthdays, guild_id_str)
            except Exception as exc:
                self.log(f"Could not read birthdays for {guild.name}: {exc}")
                continue
            for record in birthdays:
                if int(record.get("month", 0)) != today.month or int(record.get("day", 0)) != today.day:
                    continue
                user_id = record.get("user_id")
                member = guild.get_member(int(user_id)) if user_id else None
                if not member:
                    continue
                try:
                    claimed = await asyncio.to_thread(self.store.claim_birthday_fire, guild_id_str, user_id, date_str)
                except Exception as exc:
                    self.log(f"Could not check birthday fire-state for {member}: {exc}")
                    continue
                if not claimed:
                    continue
                await self.send_birthday_wish(channel, member, config.get("image_id"), config.get("role_ids") or [])

    async def send_birthday_wish(self, channel, member, image_id, role_ids):
        guild = channel.guild
        roles = [guild.get_role(int(role_id)) for role_id in role_ids]
        roles = [role for role in roles if role]
        embed = discord.Embed(
            title="\U0001f389 Happy Birthday! \U0001f389",
            description=(
                f"Today we celebrate **{member.display_name}**! \U0001f382\n\n"
                "From everyone at **Metaverse**, thank you for helping make this community welcoming, "
                "creative, and full of great memories. May your new year bring happiness, good health, success, "
                "and exciting new adventures.\n\n"
                "Wishing you a truly wonderful birthday! \U0001f973\u2728"
            ),
            colour=discord.Colour.gold(),
        )
        embed.set_thumbnail(url=str(member.display_avatar.url))
        file = None
        try:
            image = await asyncio.to_thread(self.store.get_birthday_image, image_id)
            if image:
                content_type, data = image
                extension = image_extension(content_type, data)
                filename = f"birthday.{extension}"
                file = discord.File(io.BytesIO(data), filename=filename)
                embed.set_image(url=f"attachment://{filename}")
        except Exception as exc:
            self.log(f"Could not load birthday image: {exc}")
        try:
            await channel.send(
                content=" ".join(role.mention for role in roles) or None, embed=embed, file=file,
                allowed_mentions=discord.AllowedMentions(users=False, everyone=False, roles=True),
            )
            self.log(f"Sent birthday wish for {member} in {channel.guild.name}.")
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.log(f"Could not send birthday wish for {member}: {exc}")

    async def ensure_birthday_dashboards(self):
        """Post (or move) the persistent 'Add Birthday' button in each configured birthday channel."""
        for guild_id, config in self.settings.get("birthdays", {}).items():
            channel_id = str(config.get("channel_id", ""))
            if not channel_id:
                continue
            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                continue
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            try:
                record = await asyncio.to_thread(self.store.get_birthday_dashboard, guild_id)
            except Exception as exc:
                self.log(f"Could not read birthday dashboard state for {guild.name}: {exc}")
                continue
            if record and str(record.get("channel_id")) == channel_id:
                try:
                    await channel.fetch_message(int(record["message_id"]))
                    continue  # Dashboard message already exists in the right channel.
                except (discord.NotFound, discord.Forbidden, KeyError, ValueError, TypeError):
                    pass  # Message was deleted or invalid; repost below.
            if record and str(record.get("channel_id")) != channel_id:
                old_channel = guild.get_channel(int(record["channel_id"])) if str(record.get("channel_id", "")).isdigit() else None
                if isinstance(old_channel, (discord.TextChannel, discord.Thread)) and record.get("message_id"):
                    try:
                        old_message = await old_channel.fetch_message(int(record["message_id"]))
                        await old_message.delete()
                    except (discord.NotFound, discord.Forbidden, ValueError, TypeError):
                        pass
            try:
                message = await channel.send(
                    "## \U0001f382 Birthdays\nAdd your birthday so the server can celebrate with you! Click below.",
                    view=BirthdayDashboardView(self),
                )
                await asyncio.to_thread(self.store.save_birthday_dashboard, guild_id, channel_id, message.id)
                self.log(f"Birthday dashboard posted in {guild.name}.")
            except (discord.Forbidden, discord.HTTPException) as exc:
                self.log(f"Could not post birthday dashboard in {guild.name}: {exc}")

    def dashboard_text(self, room, locked=None):
        locked = room.get("locked", False) if locked is None else locked
        state = "\U0001f512 Locked" if locked else "\U0001f513 Open"
        limit = room.get("user_limit", 0) or "Unlimited"
        return f'## \U0001f39b\ufe0f Temporary Voice Controls\nOwner: <@{room.get("owner_id")}>\nStatus: **{state}** \u2022 Limit: **{limit}**\nOnly the room owner can use these controls.'

    async def dashboard_room_for_interaction(self, interaction, channel_id):
        """Authorize the owner only when clicking inside their active voice-channel chat."""
        if not interaction.guild or str(interaction.channel_id) != str(channel_id):
            raise PermissionError("Use the control dashboard inside the temporary voice chat.")
        voice = getattr(interaction.user, "voice", None)
        if not voice or not voice.channel or str(voice.channel.id) != str(channel_id):
            raise PermissionError("Join this temporary voice room before using its controls.")
        room = await asyncio.to_thread(self.store.get_temp_voice_room, interaction.guild.id, channel_id)
        if not room or room.get("status") != "active" or str(room.get("owner_id")) != str(interaction.user.id):
            raise PermissionError("Only this temporary room's owner can use the controls.")
        channel = interaction.guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.VoiceChannel):
            raise PermissionError("This temporary voice room is no longer available.")
        return room

    async def rename_temp_room(self, interaction, channel_id, name):
        if not name or len(name) > 100:
            raise PermissionError("Room name must be between 1 and 100 characters.")
        room = await self.dashboard_room_for_interaction(interaction, channel_id)
        channel = interaction.guild.get_channel(int(channel_id))
        await channel.edit(name=name, reason="Temporary room owner renamed the room")
        room["name"] = name
        await asyncio.to_thread(self.store.save_temp_voice_room, room)

    async def set_temp_room_limit(self, interaction, channel_id, number):
        room = await self.dashboard_room_for_interaction(interaction, channel_id)
        channel = interaction.guild.get_channel(int(channel_id))
        await channel.edit(user_limit=number, reason="Temporary room owner changed member limit")
        room["user_limit"] = number
        await asyncio.to_thread(self.store.save_temp_voice_room, room)

    async def invite_temp_room_member(self, interaction, room, user_id):
        if not str(user_id).isdigit():
            raise PermissionError("Enter a numeric Discord user ID.")
        member = interaction.guild.get_member(int(user_id))
        if not member:
            try:
                member = await interaction.guild.fetch_member(int(user_id))
            except discord.NotFound as exc:
                raise PermissionError("That user is not a member of this server.") from exc
        if member.bot or member.id == interaction.user.id:
            raise PermissionError("Choose a normal server member other than yourself.")
        channel = interaction.guild.get_channel(int(room["channel_id"]))
        if not isinstance(channel, discord.VoiceChannel):
            raise PermissionError("This temporary voice room is no longer available.")
        await channel.set_permissions(member, view_channel=True, connect=True,
                                      reason="Temporary room owner invitation")
        invited = list(dict.fromkeys([str(x) for x in room.get("invited_user_ids", [])] + [str(member.id)]))
        room["invited_user_ids"] = invited
        await asyncio.to_thread(self.store.save_temp_voice_room, room)
        return member

    async def set_temp_room_lock(self, interaction, room, locked):
        channel = interaction.guild.get_channel(int(room["channel_id"]))
        await channel.set_permissions(interaction.guild.default_role, view_channel=True, connect=not locked,
                                      reason=f"Temporary room {'locked' if locked else 'unlocked'}")
        await channel.set_permissions(interaction.user, manage_channels=True, move_members=True, view_channel=True, connect=True,
                                      reason="Keep temporary room owner access")
        for invited_id in room.get("invited_user_ids", []):
            invited = interaction.guild.get_member(int(invited_id))
            if invited:
                await channel.set_permissions(invited, view_channel=True, connect=True,
                                              reason="Temporary room owner invitation")
        room["locked"] = locked
        await asyncio.to_thread(self.store.save_temp_voice_room, room)

    async def restore_temp_voice_dashboards(self):
        """Re-register stored button dashboards after a GUI/Railway restart."""
        try:
            rooms = await asyncio.to_thread(self.store.list_active_temp_voice_rooms)
            for room in rooms:
                message_id = room.get("dashboard_message_id")
                key = (str(room.get("channel_id")), str(message_id))
                guild = self.bot.get_guild(int(room["guild_id"]))
                channel = guild and guild.get_channel(int(room["channel_id"]))
                if not message_id or not isinstance(channel, discord.VoiceChannel) or key in self.dashboard_views_registered:
                    continue
                self.bot.add_view(TempVoiceControlView(self, room), message_id=int(message_id))
                self.dashboard_views_registered.add(key)
            if rooms:
                self.log(f"Restored {len(self.dashboard_views_registered)} temporary voice dashboard(s).")
        except Exception as exc:
            self.log(f"Temporary voice dashboard restore failed: {exc}")

    async def send_member_greeting(self, member, kind):

        try:
            # Always use the newest GUI-saved welcome configuration from R2.
            self.settings = await asyncio.to_thread(self.store.get_settings)
        except Exception as exc:
            self.log(f"Could not refresh welcome settings: {exc}")
        config = self.settings.get("welcome_goodbye", {}).get(str(member.guild.id), {})
        channel_id = config.get(f"{kind}_channel_id")
        channel = None
        if channel_id:
            try:
                channel = member.guild.get_channel(int(channel_id))
                if channel is None:
                    channel = await member.guild.fetch_channel(int(channel_id))
            except (ValueError, TypeError, discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                self.log(f"Configured {kind} channel unavailable in {member.guild.name}: {exc}")
        # If GUI config is missing/stale, use the server system channel or first text channel.
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            channel = member.guild.system_channel
        if not isinstance(channel, discord.TextChannel):
            me = member.guild.me
            channel = next((item for item in member.guild.text_channels if me and item.permissions_for(me).send_messages), None)
        if channel is None:
            self.log(f"No writable welcome channel found in {member.guild.name}.")
            return
        try:
            claimed = await asyncio.to_thread(self.store.claim_member_greeting, member.guild.id, member.id, kind)
        except Exception as exc:
            # Sending without the shared claim can create a duplicate when both
            # the GUI and Railway receive the same gateway event.
            self.log(f"Could not claim {kind} greeting event; skipped to prevent a duplicate: {exc}")
            return
        if not claimed:
            self.log(f"Skipped duplicate {kind} greeting event for {member} in {member.guild.name}.")
            return
        heading = "Welcome" if kind == "welcome" else "Goodbye"
        custom = config.get(f"{kind}_message", "").strip()
        color = discord.Colour.green() if kind == "welcome" else discord.Colour.orange()
        embed = discord.Embed(title=f"\u2728 {heading}", description=custom or ("Welcome to our premium community!" if kind == "welcome" else "We will miss you!"), colour=color)
        embed.set_thumbnail(url=str(member.display_avatar.url))
        image_id = config.get(f"{kind}_image_id")
        file = None
        if image_id:
            try:
                image = await asyncio.to_thread(self.store.get_welcome_goodbye_image, image_id)
                if image:
                    content_type, data = image
                    extension = image_extension(content_type, data)
                    filename = f"{kind}.{extension}"
                    file = discord.File(io.BytesIO(data), filename=filename)
                    embed.set_image(url=f"attachment://{filename}")
            except Exception as exc:
                self.log(f"Could not load {kind} image; sending text greeting: {exc}")
        try:
            await channel.send(member.mention, embed=embed, file=file,
                               allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        except Exception as exc:
            self.log(f"Could not send {kind} message in {member.guild.name}: {exc}")

    async def create_temp_room(self, member, lobby):
        guild = member.guild

        if lobby is None:
            raise RuntimeError("The configured temporary voice lobby is unavailable. Re-select and save the lobby in the GUI.")
        # A display name can contain characters Discord rejects in channel names; Discord applies its own final validation.
        room_name = f"{member.display_name}'s Room"[:100]
        # New rooms are joinable by default.  Copying a restricted lobby/category overwrite
        # here can create a visible room that nobody is allowed to enter.
        everyone = discord.PermissionOverwrite(view_channel=True, connect=True)
        overwrites = {
            guild.default_role: everyone,
            member: discord.PermissionOverwrite(manage_channels=True, move_members=True, view_channel=True, connect=True),
        }
        room = await guild.create_voice_channel("Temp Room", category=getattr(lobby, "category", None), overwrites=overwrites,
                                                reason="Temporary voice room")
        record = {"guild_id": str(guild.id), "channel_id": str(room.id), "lobby_id": str(lobby.id),
                  "owner_id": str(member.id), "owner_name": str(member), "name": room.name,
                  "status": "active", "invited_user_ids": [], "created_at": discord.utils.utcnow().isoformat()}
        try:
            # Move first; rename, R2 persistence, and panel follow.
            await member.move_to(room, reason="Moved into their temporary voice room")
            await room.edit(name=room_name, reason="Set temporary room name")
            record["name"] = room.name
            await asyncio.to_thread(self.store.save_temp_voice_room, record)
            dashboard = await room.send(self.dashboard_text(record), view=TempVoiceControlView(self, record))
            record["dashboard_message_id"] = str(dashboard.id)
            await asyncio.to_thread(self.store.save_temp_voice_room, record)
        except Exception:
            await room.delete(reason="Temporary room setup failed")
            raise
        self.log(f"Temporary voice room created for {member} in {guild.name}.")

    async def handle_temp_voice_update(self, member, before, after):
        if member.bot:
            return
        lobby_id = self.temp_lobby_id(member.guild.id)
        if lobby_id and after.channel and str(after.channel.id) == lobby_id and before.channel != after.channel:
            try:
                # GUI and Railway can briefly overlap during configuration.  The Cloudflare R2
                # claim makes this event idempotent across both processes.
                claimed = await asyncio.to_thread(
                    self.store.claim_temp_voice_creation, member.guild.id, after.channel.id, member.id
                )
                if claimed:
                    await self.create_temp_room(member, after.channel)
            except discord.Forbidden:
                self.log("Missing Administrator/Manage Channels/Move Members permission for temporary voice.")
                await asyncio.to_thread(
                    self.store.release_temp_voice_creation, member.guild.id, after.channel.id, member.id
                )
            except Exception as exc:
                self.log(f"Temporary voice room creation failed: {exc}")
                await asyncio.to_thread(
                    self.store.release_temp_voice_creation, member.guild.id, after.channel.id, member.id
                )
        if before.channel and not before.channel.members:
            room = await asyncio.to_thread(self.store.get_temp_voice_room, member.guild.id, before.channel.id)
            if room and room.get("status") == "active":
                try:
                    await before.channel.delete(reason="Empty temporary voice room")
                    await asyncio.to_thread(self.store.close_temp_voice_room, member.guild.id, before.channel.id)
                    self.log(f"Empty temporary voice room removed in {member.guild.name}.")
                except (discord.Forbidden, discord.NotFound) as exc:
                    self.log(f"Could not remove temporary voice room: {exc}")

    async def ticket_escalation_tick(self):
        """Remind assigned staff after 15 minutes and warn both parties after 1 hour."""
        while not self.bot.is_closed():
            await asyncio.sleep(30)
            try:
                tickets=await asyncio.to_thread(self.store.list_tickets,500)
                now=datetime.now(timezone.utc)
                for ticket in tickets:
                    if ticket.get("status")!="open" or ticket.get("staff_replied_at"):
                        continue
                    try:
                        created=datetime.fromisoformat(str(ticket.get("staff_assigned_at") or ticket["created_at"]).replace("Z","+00:00"))
                        if created.tzinfo is None:created=created.replace(tzinfo=timezone.utc)
                    except (KeyError,TypeError,ValueError):
                        continue
                    age=(now-created.astimezone(timezone.utc)).total_seconds()
                    if age<900:continue
                    guild=self.bot.get_guild(int(ticket["guild_id"]))
                    channel=guild.get_channel(int(ticket.get("ticket_channel_id",""))) if guild and str(ticket.get("ticket_channel_id","")).isdigit() else None
                    if not isinstance(channel,(discord.TextChannel,discord.Thread)):
                        continue
                    stages=[]
                    if age>=900:stages.append("15m")
                    if age>=3600:stages.append("1h")
                    for stage in stages:
                        try:
                            claimed=await asyncio.to_thread(self.store.claim_ticket_escalation,ticket["document_id"],stage)
                            if not claimed:continue
                            staff_id=str(ticket.get("assigned_moderator_id",""))
                            user_id=str(ticket.get("user_id",""))
                            if stage=="15m":
                                content=f"<@{staff_id}> **Ticket #{ticket['ticket_id']} has been waiting for 15 minutes without a staff reply.** Please respond when available."
                                allowed=discord.AllowedMentions(users=[discord.Object(id=int(staff_id))] if staff_id.isdigit() else [],roles=False,everyone=False)
                                await channel.send(content,allowed_mentions=allowed)
                                self.log(f"Sent 15-minute unanswered-ticket reminder for #{ticket['ticket_id']}.")
                            else:
                                mentions=[discord.Object(id=int(value)) for value in (staff_id,user_id) if value.isdigit()]
                                content=f"<@{staff_id}> <@{user_id}> **Warning: ticket #{ticket['ticket_id']} has been open for over 1 hour without a staff reply.** The assigned moderator should respond as soon as possible."
                                await channel.send(content,allowed_mentions=discord.AllowedMentions(users=mentions,roles=False,everyone=False))
                                self.log(f"Sent 1-hour ticket warning to staff and owner for #{ticket['ticket_id']}.")
                        except Exception as exc:
                            try:await asyncio.to_thread(self.store.release_ticket_escalation,ticket["document_id"],stage)
                            except Exception:pass
                            self.log(f"Could not send {stage} reminder for ticket #{ticket.get('ticket_id')}: {exc}")
            except Exception as exc:
                self.log(f"Ticket escalation check failed: {exc}")

    async def ensure_ticket_dashboards(self):
        """Post (or move) the persistent 'Create Ticket' button in each configured ticket channel."""
        for guild_id, channel_id in self.settings.get("ticket_channels", {}).items():
            channel_id = str(channel_id)
            if not channel_id:
                continue
            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                continue
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            try:
                record = await asyncio.to_thread(self.store.get_ticket_dashboard, guild_id)
            except Exception as exc:
                self.log(f"Could not read ticket dashboard state for {guild.name}: {exc}")
                continue
            if record and str(record.get("channel_id")) == channel_id:
                try:
                    await channel.fetch_message(int(record["message_id"]))
                    continue  # Dashboard message already exists in the right channel.
                except (discord.NotFound, discord.Forbidden, KeyError, ValueError, TypeError):
                    pass  # Message was deleted or invalid; repost below.
            if record and str(record.get("channel_id")) != channel_id:
                old_channel = guild.get_channel(int(record["channel_id"])) if str(record.get("channel_id", "")).isdigit() else None
                if isinstance(old_channel, (discord.TextChannel, discord.Thread)) and record.get("message_id"):
                    try:
                        old_message = await old_channel.fetch_message(int(record["message_id"]))
                        await old_message.delete()
                    except (discord.NotFound, discord.Forbidden, ValueError, TypeError):
                        pass
            try:
                message = await channel.send(
                    "## \U0001f3ab Support Tickets\nNeed help? Click below to describe your issue and open a private ticket.",
                    view=TicketCreateView(self),
                )
                await asyncio.to_thread(self.store.save_ticket_dashboard, guild_id, channel_id, message.id)
                self.log(f"Ticket dashboard posted in {guild.name}.")
            except (discord.Forbidden, discord.HTTPException) as exc:
                self.log(f"Could not post ticket dashboard in {guild.name}: {exc}")


    def _register_custom_commands(self, bot):
        """Register GUI-defined slash commands from the shared settings."""
        self.custom_slash_commands.register(bot, self.settings)

    async def _sync_custom_commands(self, previous_settings, current_settings):
        async with self.custom_command_sync_lock:
            await self.custom_slash_commands.sync(
                self.bot, previous_settings, current_settings
            )
            self.custom_commands_registered = copy.deepcopy(
                current_settings.get("custom_commands", {})
            )

    async def _apply_settings_update(self, previous_settings, current_settings):
        if previous_settings.get("custom_commands", {}) != current_settings.get("custom_commands", {}):
            await self._sync_custom_commands(previous_settings, current_settings)
        if previous_settings.get("ticket_channels", {}) != current_settings.get("ticket_channels", {}):
            await self.ensure_ticket_dashboards()
        if previous_settings.get("birthdays", {}) != current_settings.get("birthdays", {}):
            await self.ensure_birthday_dashboards()

    def update_settings(self, settings):
        """Apply settings locally and publish GUI changes to Discord immediately."""
        previous = self.applied_settings
        settings_snapshot = copy.deepcopy(settings)
        self.settings = settings
        if previous == settings_snapshot:
            return
        self.applied_settings = settings_snapshot
        if self.bot is None or self.loop is None or not self.loop.is_running():
            return

        future = asyncio.run_coroutine_threadsafe(
            self._apply_settings_update(previous, settings_snapshot),
            self.loop,
        )

        def report_sync_error(done):
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error:
                self.log(f"Could not apply saved feature settings: {error}")

        future.add_done_callback(report_sync_error)

    async def refresh_settings(self):
        while not self.bot.is_closed():
            await asyncio.sleep(10)
            try:
                fresh = self.store.get_settings()
                if fresh["settings_revision"] != self.settings["settings_revision"]:
                    previous = self.applied_settings
                    self.settings = fresh
                    await self._apply_settings_update(previous, fresh)
                    self.applied_settings = copy.deepcopy(fresh)
                    self.log("Settings synchronized from Cloudflare R2.")
                elif self.custom_commands_registered != self.settings.get("custom_commands", {}):
                    await self._sync_custom_commands(
                        {"custom_commands": self.custom_commands_registered}, self.settings
                    )
            except Exception as exc:
                self.log(f"Cloudflare R2 settings refresh failed: {exc}")

    def build(self):
        intents = discord.Intents.default(); intents.message_content = True; intents.members = True
        bot = commands.Bot(command_prefix="!", intents=intents)
        self.bot = bot
        self._register_custom_commands(bot)

        @bot.event
        async def on_ready():
            self.loop = asyncio.get_running_loop()
            self.log(f"Logged in as {bot.user} ({self.source})")
            self.log(f"Welcome diagnostics: members_intent={bot.intents.members}, guilds={len(bot.guilds)}")
            self.event("invite", discord.utils.oauth_url(bot.user.id, permissions=INVITE_PERMISSIONS, scopes=("bot", "applications.commands")))
            self.event("guilds", [(g.id, g.name) for g in bot.guilds])
            for guild in bot.guilds:
                if guild.id != APP_CONFIG.allowed_guild_id:
                    asyncio.create_task(self.notify_external_guild_owner(guild))
                    continue
                # Roles and channels arrive with the guild and do not need a full
                # member-cache request. Waiting for guild.chunk() here could
                # leave the GUI connected but blank for a long time (or forever)
                # and delay every Railway background feature. Member IDs are now
                # verified directly with Discord when an action needs one.
                try:
                    self.event("guild", self.guild_info(guild))
                except Exception as exc:
                    self.log(f"Could not load server data for {guild.name}: {exc}")
            if self.custom_commands_registered != self.settings.get("custom_commands", {}):
                await self._sync_custom_commands(
                    {"custom_commands": self.custom_commands_registered}, self.settings
                )
            if not self.background_tasks_started:
                self.background_tasks_started = True
                asyncio.create_task(self.refresh_settings())
                asyncio.create_task(self.ticket_escalation_tick())
                asyncio.create_task(self.restore_temp_voice_dashboards())
                # Railway is the sole voice-XP scheduler; it shares Cloudflare R2 state
                # with the GUI without creating duplicate transaction writers.
                if self.source == "railway":
                    asyncio.create_task(self.voice_xp_tick())
                asyncio.create_task(self.role_notification_tick())
                asyncio.create_task(self.movie_announcement_tick())
                bot.add_view(TicketCreateView(self))
                asyncio.create_task(self.ensure_ticket_dashboards())
                asyncio.create_task(self.restore_ticket_staff_dashboards())
                bot.add_view(BirthdayDashboardView(self))
                asyncio.create_task(self.ensure_birthday_dashboards())
                asyncio.create_task(self.birthday_tick())
            if not getattr(self, "commands_synced", False):
                try:
                    await bot.tree.sync()
                    for guild in bot.guilds:
                        guild_scope = discord.Object(id=guild.id)
                        bot.tree.copy_global_to(guild=guild_scope)
                        await bot.tree.sync(guild=guild_scope)
                    self.commands_synced = True; self.log("Slash commands synchronized.")
                except Exception as exc: self.log(f"Slash command sync failed: {exc}")

        @bot.event
        async def on_guild_join(guild):
            self.log(f"Joined {guild.name}")
            if guild.id != APP_CONFIG.allowed_guild_id:
                await self.notify_external_guild_owner(guild)
                return
            self.event("guilds", [(g.id, g.name) for g in bot.guilds])
            self.event("guild", self.guild_info(guild))
        @bot.event
        async def on_voice_state_update(member, before, after):
            await self.handle_temp_voice_update(member, before, after)
        @bot.event
        async def on_member_join(member):
            self.log(f"Member join event received: {member} ({member.id}) in {member.guild.name}; bot={member.bot}")
            # Welcome is sent for every joining member, including bot accounts.
            await self.send_member_greeting(member, "welcome")
            if member.bot:
                return
            await self.apply_default_roles(member)
        @bot.event
        async def on_member_remove(member):
            await self.send_member_greeting(member, "goodbye")

        @bot.event
        async def on_message(message):
            if message.author.bot:
                if bot.user is not None and message.author.id == bot.user.id:
                    return
                if await self.bot_spam_guard.inspect(message, self.ban_spamming_bot):
                    return
                return await bot.process_commands(message)
            if not message.guild:
                rating_requests = {
                    "start", "star", "rating", "ratings", "start rating",
                    "my rating", "my ratings",
                }
                if message.content.strip().casefold() in rating_requests:
                    try:
                        is_staff = await asyncio.to_thread(
                            self.store.is_ticket_staff, message.author.id
                        )
                    except Exception as exc:
                        self.log(f"Could not verify ticket staff {message.author.id}: {exc}")
                        return await message.channel.send(
                            "I could not check your ticket staff access right now. Please try again."
                        )
                    if not is_staff:
                        return await message.channel.send(
                            "This summary is available to current ticket staff and past ticket assignees."
                        )
                    try:
                        rating=await asyncio.to_thread(self.store.get_ticket_staff_rating,message.author.id)
                    except Exception as exc:
                        self.log(f"Could not load ticket rating summary for staff {message.author.id}: {exc}")
                        return await message.channel.send(
                            "I could not load your rating summary right now. Please try again in a moment."
                        )
                    description=(
                        "No members have rated your closed tickets yet."
                        if not rating["reviews"]
                        else "Ratings submitted by users after their tickets were closed."
                    )
                    embed=discord.Embed(title="Your Support Rating Summary",description=description,colour=discord.Colour.gold())
                    embed.add_field(name="Total star points",value=f"**{rating['total_stars']}**",inline=True)
                    embed.add_field(name="Reviews",value=f"**{rating['reviews']}**",inline=True)
                    embed.add_field(name="Average",value=f"**{rating['average']:.2f} / 5**",inline=True)
                    return await message.channel.send(embed=embed)
                return await bot.process_commands(message)
            ticket=self.open_ticket_channels.get(str(message.channel.id))
            if ticket and str(message.author.id)!=str(ticket.get("user_id")):
                staff_ids={str(ticket.get("assigned_moderator_id",""))}
                staff_ids.update(map(str,self.settings.get("ticket_moderators",{}).get(str(message.guild.id),[])))
                if str(message.author.id) in staff_ids:
                    try:
                        marked=await asyncio.to_thread(self.store.mark_ticket_staff_reply,ticket["document_id"],message.author.id)
                        if marked:
                            ticket["staff_replied_at"]=datetime.now(timezone.utc).isoformat()
                            self.log(f"Staff reply received on ticket #{ticket['ticket_id']}; unanswered-ticket reminders stopped.")
                    except Exception as exc:
                        self.log(f"Could not mark staff reply on ticket #{ticket['ticket_id']}: {exc}")
            if await self.word_block.handle_message(message):
                return
            # Discord invites are always blocked. Discord does not expose an invite's
            # destination without resolving it, so this safely blocks all server invites.
            if DISCORD_INVITE_PATTERN.search(message.content):
                try:
                    await message.delete()
                except (discord.Forbidden, discord.NotFound) as exc:
                    self.log(f"Cannot delete Discord invite: {exc}")
                return
            # Link Send Permissions are independent from /chat. Only the selected
            # roles may post a URL; normal messages are never affected.
            if LINK_PATTERN.search(message.content) and not self.link_allowed(message.author):
                try:
                    await message.delete()
                    self.log(f"Deleted unauthorized link from {message.author} in {message.guild.name}.")
                except (discord.Forbidden, discord.NotFound) as exc:
                    self.log(f"Cannot delete unauthorized link: {exc}")
                return
            await self.handle_counting_message(message)
            asyncio.create_task(self.handle_level_activity(message))
            await bot.process_commands(message)
        @bot.tree.command(name="chat", description="Open a form and send a message in this channel")
        @app_commands.guild_only()
        async def chat(interaction: discord.Interaction):
            """Open the text sender only for Chat Permissions roles."""
            if not self.chat_allowed(interaction.user):
                return await interaction.response.send_message(
                    "You do not have permission to use /chat.",
                    ephemeral=True,
                )
            if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                return await interaction.response.send_message("Use /chat in a text channel or thread.", ephemeral=True)
            await interaction.response.send_modal(ChatSendModal(self))

        @bot.tree.command(name="help", description=f"Browse the English {APP_CONFIG.bot_app_name} feature guide")
        @app_commands.guild_only()
        async def help_command(interaction: discord.Interaction):
            view=HelpPagerView(interaction.user.id,make_help_embeds(interaction.guild))
            await interaction.response.send_message(embed=view.embeds[0],view=view,ephemeral=True)
            try:
                view.message=await interaction.original_response()
            except discord.HTTPException:
                pass

        @bot.tree.command(name="leaderboard", description="Show the server's top 10 members by total leveling XP")
        @app_commands.guild_only()
        async def leaderboard(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            try:
                # Resolve the live member list from Discord so members who left
                # are excluded even when this worker's member cache is partial.
                current_members = {
                    member.id: member
                    async for member in interaction.guild.fetch_members(limit=None)
                }
                records = await asyncio.to_thread(
                    self.store.top_activity_users,
                    interaction.guild.id,
                    10,
                    current_members.keys(),
                )
                if not records:
                    return await interaction.followup.send("There are no XP records for this server yet.", ephemeral=True)
                entries = []
                for record in records:
                    member_id = str(record.get("user_id", ""))
                    member = current_members.get(int(member_id)) if member_id.isdigit() else None
                    if member is None:
                        continue
                    entries.append({
                        "name": member.display_name,
                        "avatar_url": str(member.display_avatar.url),
                        "level": int(record.get("level", 1)),
                        "total_xp": int(record.get("total_xp", 0)),
                    })
                png = await asyncio.to_thread(
                    render_leaderboard_png,
                    interaction.guild.name,
                    str(interaction.guild.icon.url) if interaction.guild.icon else "",
                    entries,
                )
                file = discord.File(png, filename="level-leaderboard.png")
                await interaction.followup.send(file=file)
            except Exception as exc:
                self.log(f"Could not create leveling leaderboard for {interaction.guild.name}: {exc}")
                await interaction.followup.send("The leaderboard could not be generated. Please try again later.", ephemeral=True)

        @bot.tree.command(name="move", description="Select voice members and move them to another voice channel")
        @app_commands.guild_only()
        async def move(interaction: discord.Interaction):
            if not self.drag_allowed(interaction.user):
                return await interaction.response.send_message("You do not have an allowed Drag Permissions role.", ephemeral=True)
            if not interaction.guild.me.guild_permissions.move_members:
                return await interaction.response.send_message("The bot needs the Move Members permission to use /drag.", ephemeral=True)
            members = [member for channel in interaction.guild.voice_channels for member in channel.members if not member.bot]
            if not members:
                return await interaction.response.send_message("There are no members currently in a voice channel.", ephemeral=True)
            active_channels = [channel for channel in interaction.guild.voice_channels if any(not member.bot for member in channel.members)]
            if not active_channels:
                return await interaction.response.send_message("There are no occupied voice channels with human members right now.", ephemeral=True)
            view = MoveActiveVCView(self, interaction.guild, interaction.user.id, active_channels)
            await interaction.response.send_message(embed=view.make_embed(), ephemeral=True, view=view)
        return bot

    async def run(self, token):
        self.build()
        await self.bot.start(token)

    async def reset_counting_channel(self, guild_id, channel_id):
        """Clear the configured counting channel and persistently restart at 1."""
        guild = self.bot.get_guild(int(guild_id))
        channel = guild.get_channel(int(channel_id)) if guild else None
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("The selected counting channel is unavailable.")
        deleted = await channel.purge(limit=None, bulk=True, reason="Counting counter reset from GUI")
        await asyncio.to_thread(self.store.reset_counting_state, guild_id, channel_id)
        self.log(f"Counting reset in {guild.name}; deleted {len(deleted)} message(s) from #{channel.name}.")
        return len(deleted)
    async def create_ticket_channel(self, guild, source_channel, ticket):
        """Create the ticket's locked private chat beside the ticket dashboard."""
        parent = source_channel.parent if isinstance(source_channel, discord.Thread) else source_channel
        category = getattr(parent, "category", None)
        owner = guild.get_member(int(ticket["user_id"])) or await guild.fetch_member(int(ticket["user_id"]))
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            owner: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        configured_staff = await asyncio.to_thread(self.store.ticket_moderators, guild.id)
        staff_ids={int(staff_id) for staff_id in configured_staff if str(staff_id).isdigit()}
        assigned_id=str(ticket.get("assigned_moderator_id") or "")
        if assigned_id.isdigit(): staff_ids.add(int(assigned_id))
        for staff_id in staff_ids:
            try:
                staff = guild.get_member(staff_id) or await guild.fetch_member(staff_id)
                overwrites[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            except discord.NotFound:
                self.log(f"Ticket staff account {staff_id} is not in {guild.name}.")
        channel = await guild.create_text_channel(
            name=f"ticket-{str(ticket['ticket_id']).lower()}", category=category, overwrites=overwrites,
            reason=f"Support ticket #{ticket['ticket_id']} opened by {owner}",
        )
        await channel.send(
            f"## Ticket #{ticket['ticket_id']}\n**Category:** {ticket.get('category') or '-'}\n**Opened by:** {owner.mention}\n\n{ticket.get('problem') or '-'}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        updated_ticket = await asyncio.to_thread(self.store.update_ticket_fields, ticket["document_id"], {"ticket_channel_id": str(channel.id)})
        ticket.clear(); ticket.update(updated_ticket)
        self.open_ticket_channels[str(channel.id)]=dict(ticket)
        self.log(f"Created private chat #{channel.name} for ticket #{ticket['ticket_id']} in {guild.name}.")
        return channel

    async def delete_ticket_channel(self, ticket):
        """Remove the private chat when its ticket is cancelled, resolved, or closed."""
        channel_id, guild_id = str(ticket.get("ticket_channel_id") or ""), str(ticket.get("guild_id") or "")
        if not channel_id.isdigit() or not guild_id.isdigit():
            return
        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            self.log(f"Could not delete ticket #{ticket.get('ticket_id')}'s chat: guild is unavailable.")
            return
        channel = guild.get_channel(int(channel_id))
        self.open_ticket_channels.pop(channel_id,None)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(int(channel_id))
            except discord.NotFound:
                return
            except discord.HTTPException as exc:
                self.log(f"Could not find private chat for ticket #{ticket.get('ticket_id')}: {exc}")
                return
        try:
            await channel.delete(reason=f"Support ticket #{ticket.get('ticket_id')} {ticket.get('status')}")
            self.log(f"Deleted private chat for ticket #{ticket.get('ticket_id')} ({ticket.get('status')}).")
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.log(f"Could not delete private chat for ticket #{ticket.get('ticket_id')}: {exc}")

    async def finalize_ticket(self, ticket):
        """Shared GUI completion path: delete the chat and notify the ticket owner."""
        await self.delete_ticket_channel(ticket)
        await self.notify_ticket_status(ticket)
    def ticket_staff_embed(self,ticket):
        category=str(ticket.get("category") or "Uncategorized")
        if ticket.get("other_category"):category+=f" â€” {ticket['other_category']}"
        embed=discord.Embed(title=f"Support Ticket #{ticket.get('ticket_id')}",description=str(ticket.get("problem") or "No problem details were provided."),colour=discord.Colour.blurple())
        embed.add_field(name="Submitted by",value=f"{ticket.get('username','Unknown user')}\n`{ticket.get('user_id','-')}`",inline=True)
        embed.add_field(name="Category",value=category[:1024],inline=True)
        if ticket.get("contact"):
            embed.add_field(name="Contact details",value=str(ticket["contact"])[:1024],inline=False)
        channel_id=str(ticket.get("ticket_channel_id") or "")
        if channel_id.isdigit():embed.add_field(name="Private support chat",value=f"<#{channel_id}>",inline=False)
        embed.add_field(name="Status",value=str(ticket.get("status","open")).title(),inline=True)
        embed.add_field(name="Current assigned staff",value=f"<@{ticket.get('assigned_moderator_id')}>" if ticket.get("assigned_moderator_id") else "Unassigned",inline=True)
        embed.set_footer(text="Use the controls below to resolve, close, or transfer this ticket.")
        return embed

    async def send_ticket_copy_to_staff(self, ticket):
        """DM the complete ticket and persistent actions to its assigned moderator."""
        staff_id = str(ticket.get("assigned_moderator_id", ""))
        if not staff_id.isdigit():
            self.log(f"Ticket #{ticket['ticket_id']} has no assigned moderator.")
            return False
        try:
            staff = self.bot.get_user(int(staff_id)) or await self.bot.fetch_user(int(staff_id))
        except discord.HTTPException as exc:
            self.log(f"Could not find assigned ticket moderator {staff_id}: {exc}")
            return False
        try:
            message = await staff.send(embed=self.ticket_staff_embed(ticket), view=TicketStaffActionView(self, ticket), allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            self.log(f"Could not DM ticket #{ticket['ticket_id']} to staff {staff_id} (their DMs are closed).")
            return False
        except discord.HTTPException as exc:
            self.log(f"Could not DM ticket #{ticket['ticket_id']} to staff {staff_id}: {exc}")
            return False
        try:
            await asyncio.to_thread(self.store.update_ticket_fields, ticket["document_id"], {"staff_dm_channel_id": str(message.channel.id), "staff_dm_message_id": str(message.id)})
        except Exception as exc:
            self.log(f"Could not save the ticket's staff DM reference: {exc}")
            try:await message.edit(view=None)
            except discord.HTTPException:pass
            return False
        return True

    async def transfer_ticket(self,ticket,new_staff_id,actor_id,old_staff_message=None):
        """Reassign an open ticket, move private-channel access, and notify both sides."""
        old_staff_id=str(ticket.get("assigned_moderator_id",""))
        new_staff_id=str(new_staff_id)
        guild=self.bot.get_guild(int(ticket["guild_id"]))
        if not guild:raise RuntimeError("The support server is unavailable.")
        new_staff=guild.get_member(int(new_staff_id)) if new_staff_id.isdigit() else None
        if not new_staff and new_staff_id.isdigit():
            try:new_staff=await guild.fetch_member(int(new_staff_id))
            except discord.HTTPException as exc:raise RuntimeError("The selected moderator is no longer available in this server.") from exc
        channel_id=str(ticket.get("ticket_channel_id") or "")
        channel=guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
        old_staff=guild.get_member(int(old_staff_id)) if old_staff_id.isdigit() else None
        if not old_staff and old_staff_id.isdigit():
            try:old_staff=await guild.fetch_member(int(old_staff_id))
            except discord.HTTPException:old_staff=None
        updated=await asyncio.to_thread(self.store.reassign_ticket,ticket["document_id"],actor_id,new_staff_id)
        if channel_id:self.open_ticket_channels[channel_id]=dict(updated)
        new_access_set=False
        try:
            if isinstance(channel,discord.TextChannel):
                await channel.set_permissions(new_staff,view_channel=True,send_messages=True,read_message_history=True,reason=f"Ticket #{ticket['ticket_id']} reassigned by staff")
                new_access_set=True
                if old_staff and old_staff_id!=new_staff_id:
                    await channel.set_permissions(old_staff,overwrite=None,reason=f"Ticket #{ticket['ticket_id']} reassigned to another moderator")
        except discord.HTTPException as exc:
            await asyncio.to_thread(self.store.reassign_ticket,ticket["document_id"],new_staff_id,old_staff_id)
            if channel_id:self.open_ticket_channels[channel_id]=dict(ticket)
            if new_access_set and isinstance(channel,discord.TextChannel):
                try:
                    await channel.set_permissions(new_staff,overwrite=None)
                    if old_staff:await channel.set_permissions(old_staff,view_channel=True,send_messages=True,read_message_history=True)
                except discord.HTTPException as rollback_exc:self.log(f"Could not restore ticket #{ticket['ticket_id']} channel permissions after transfer failure: {rollback_exc}")
            raise RuntimeError(f"Could not update private support chat access: {exc}") from exc
        delivered=await self.send_ticket_copy_to_staff(updated)
        if not delivered:
            await asyncio.to_thread(self.store.reassign_ticket,ticket["document_id"],new_staff_id,old_staff_id)
            if channel_id:self.open_ticket_channels[channel_id]=dict(ticket)
            if new_access_set and isinstance(channel,discord.TextChannel):
                try:
                    if old_staff:await channel.set_permissions(old_staff,view_channel=True,send_messages=True,read_message_history=True)
                    await channel.set_permissions(new_staff,overwrite=None)
                except discord.HTTPException as exc:self.log(f"Could not restore ticket #{ticket['ticket_id']} channel permissions after transfer rollback: {exc}")
            raise RuntimeError("The new moderator could not receive the ticket in their Discord inbox. The previous assignment was restored.")
        if isinstance(channel,discord.TextChannel):
            notice=discord.Embed(title="Support Team Update",description=f"Your ticket is now assigned to **{new_staff.display_name}**. They have received your original request and will continue helping you in this support chat.",colour=discord.Colour.blurple())
            notice.add_field(name="New support moderator",value=new_staff.mention,inline=False)
            try:await channel.send(embed=notice,allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:self.log(f"Could not post the reassignment notice in ticket #{ticket['ticket_id']}: {exc}")
            updated["ticket_channel_id"]=channel_id
            self.open_ticket_channels[channel_id]=dict(updated)
        if old_staff_message:
            try:
                embed=old_staff_message.embeds[0].copy() if old_staff_message.embeds else discord.Embed(title=f"Support Ticket #{ticket['ticket_id']}")
                embed.colour=discord.Colour.orange()
                embed.add_field(name="Assignment update",value=f"Transferred to {new_staff.mention}. This ticket's controls are now with the new assigned moderator.",inline=False)
                await old_staff_message.edit(embed=embed,view=None,allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as exc:self.log(f"Could not update old staff inbox controls for ticket #{ticket['ticket_id']}: {exc}")
        self.log(f"Ticket #{ticket['ticket_id']} transferred from {old_staff_id} to {new_staff_id} by {actor_id}.")
        return updated
    async def restore_ticket_staff_dashboards(self):
        """Restore persistent staff controls, rating prompts, and open ticket tracking."""
        try:
            tickets = await asyncio.to_thread(self.store.list_tickets, 500)
            restored = 0; restored_ratings=0
            for ticket in tickets:
                if ticket.get("status")=="open":
                    channel_id=str(ticket.get("ticket_channel_id",""))
                    if channel_id:self.open_ticket_channels[channel_id]=dict(ticket)
                    if ticket.get("staff_dm_message_id"):
                        self.bot.add_view(TicketStaffActionView(self, ticket), message_id=int(ticket["staff_dm_message_id"]))
                        restored += 1
                elif ticket.get("status")=="closed" and ticket.get("rating_message_id"):
                    review=await asyncio.to_thread(self.store.get_ticket_review,ticket["document_id"])
                    if not review:
                        self.bot.add_view(TicketRatingView(self,ticket),message_id=int(ticket["rating_message_id"]))
                        restored_ratings+=1
            if restored:
                self.log(f"Restored {restored} ticket staff dashboard button(s).")
            if restored_ratings:self.log(f"Restored {restored_ratings} ticket rating prompt(s).")
        except Exception as exc:
            self.log(f"Ticket staff dashboard restore failed: {exc}")

    async def notify_ticket_status(self, ticket):
        user = self.bot.get_user(int(ticket["user_id"])) or await self.bot.fetch_user(int(ticket["user_id"]))
        note = ticket.get("resolution_note") or "No additional note."
        if ticket["status"] == "resolved":
            await user.send(f"Your support ticket **#{ticket['ticket_id']}** has been resolved. Note: {note}")
        elif ticket["status"] == "closed":
            message=await user.send(
                f"Your support ticket **#{ticket['ticket_id']}** has been closed.\n\n{note}\n\n"
                "Please rate your support experience below. Your rating and optional feedback will be sent to the assigned support moderator.",
                view=TicketRatingView(self,ticket),
            )
            await asyncio.to_thread(self.store.update_ticket_fields,ticket["document_id"],{"rating_message_id":str(message.id)})
        else:
            await user.send(f"Your support ticket **#{ticket['ticket_id']}** status changed to **{ticket['status']}**. Note: {note}")

    async def send_ticket_review_to_staff(self,ticket,review):
        staff_id=str(review.get("staff_id") or ticket.get("assigned_moderator_id") or "")
        if not staff_id.isdigit():
            self.log(f"Ticket #{ticket.get('ticket_id')} has no valid assigned moderator for review delivery.")
            return False
        staff=self.bot.get_user(int(staff_id))
        if not staff:
            try:staff=await self.bot.fetch_user(int(staff_id))
            except discord.HTTPException as exc:
                self.log(f"Could not find ticket staff member {staff_id} for review delivery: {exc}"); return False
        stars=int(review.get("stars",0))
        embed=discord.Embed(title=f"Ticket Review â€¢ #{ticket.get('ticket_id')}",description=f"**Rating:** {'â­' * stars}{'â˜†' * (5-stars)}  (**{stars}/5**)",colour=discord.Colour.gold())
        embed.add_field(name="From",value=f"{ticket.get('username','User')} (`{review.get('user_id')}`)",inline=False)
        embed.add_field(name="Feedback",value=str(review.get("note") or "No written feedback provided.")[:1000],inline=False)
        embed.set_footer(text="Type 'start rating' in this DM to view your rating summary.")
        await staff.send(embed=embed)
        self.log(f"Delivered {stars}-star review for ticket #{ticket.get('ticket_id')} to assigned staff {staff_id}.")
        return True

    async def notify_ticket_resolved(self, ticket):
        """Backward-compatible alias for notify_ticket_status."""
        await self.notify_ticket_status(ticket)

    async def moderate_member(self, action, guild_id, user_id, reason):
        if int(user_id) in PROTECTED_USER_IDS:
            raise RuntimeError("This member is protected and can never be kicked or banned by this bot.")
        if not self.store.claim_rate_limit(f"gui_{action}", user_id, 10):
            raise RuntimeError("This member was moderated recently. Wait 10 seconds.")
        guild = self.bot.get_guild(int(guild_id))
        if not guild: raise RuntimeError("Guild is not available to this GUI bot.")
        member = guild.get_member(int(user_id)) or await guild.fetch_member(int(user_id))
        if action == "kick": await member.kick(reason=reason or "GUI moderation action")
        elif action == "ban": await member.ban(reason=reason or "GUI moderation action", delete_message_seconds=0)
        else: raise RuntimeError("Unsupported moderation action.")
        self.log(f"{action.title()}ed {member} from {guild.name}.")

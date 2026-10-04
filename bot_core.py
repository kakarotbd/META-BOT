import asyncio
import io
import re
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from app_config import APP_CONFIG

LINK_PATTERN = re.compile(r"(https?://\S+|www\.\S+|discord\.gg/\S+|\S+\.(com|net|org|io|xyz|gg)\S*)", re.I)
DISCORD_INVITE_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/[A-Za-z0-9-]+", re.I)
# Administrator is intentional: temporary voice rooms require channel management and moving members.
INVITE_PERMISSIONS = discord.Permissions(administrator=True)
# The configured admin is protected from every Kick/Ban action issued by this bot.
# This does not affect what Discord human moderators can do manually.
PROTECTED_USER_IDS = {APP_CONFIG.admin_user_id}
BANGLADESH_TIMEZONE = "Asia/Dhaka"
DEFAULT_NOTIFICATION_TIMEZONE = BANGLADESH_TIMEZONE
TICKET_CHANNEL_STAFF_IDS = {1022113587851960431, 975756582170296382}
# datetime.weekday(): Monday=0 ... Sunday=6. Locale-independent (unlike strftime("%a")).
WEEKDAY_CODES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]


def image_extension(content_type, data=None):
    """Return an attachment extension that matches the stored image MIME type."""
    # Prefer the actual GIF signature for legacy records with a wrong MIME value.
    if data and bytes(data[:6]) in (b"GIF87a", b"GIF89a"):
        return "gif"
    return {
        "image/gif": "gif",
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }.get((content_type or "").lower(), "jpg")


def format_birthday(record):
    """Render a saved {day, month, year} birthday record as e.g. 'March 25' or 'March 25, 2000'."""
    month_index = int(record.get("month", 0))
    month_name = MONTH_NAMES[month_index - 1] if 1 <= month_index <= 12 else "?"
    year = record.get("year")
    return f"{month_name} {record.get('day')}" + (f", {year}" if year else "")


DEFAULT_TICKET_CLOSE_NOTE = (
    "Sorry for the inconvenience. This ticket has been closed. If you still need help, "
    "please feel free to open a new ticket and our support team will be happy to assist you."
)
EXTERNAL_GUILD_OWNER_NOTICE = (
    "Hello! This bot was designed exclusively for the MetaVerse Bangladesh Discord server.\n\n"
    "It is not available for use in other servers. If you would like a custom Discord bot for your own server, "
    "please contact <@1022113587851960431> (kakarotbd) to discuss a contract. This bot will now leave this server."
)


class ChatSendModal(discord.ui.Modal, title="Text Send"):
    """Role-restricted form used by /chat to post text in its current channel."""
    def __init__(self, moderator):
        super().__init__()
        self.moderator = moderator
        self.message_text = discord.ui.TextInput(
            label="Message",
            placeholder="Write text here. Discord Markdown and pasted Unicode fonts are supported.",
            style=discord.TextStyle.paragraph,
            max_length=2000,
        )
        self.add_item(self.message_text)

    async def on_submit(self, interaction):
        # Re-check in case the GUI permission setting changed while the modal was open.
        if not interaction.guild or not self.moderator.chat_allowed(interaction.user):
            return await interaction.response.send_message("You no longer have permission to use /chat.", ephemeral=True)
        content = self.message_text.value.strip()
        if not content:
            return await interaction.response.send_message("Write a message before sending.", ephemeral=True)
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("Use /chat in a text channel or thread.", ephemeral=True)
        try:
            # The shared Cloudflare R2 claim prevents duplicate posts when gateway sessions overlap.
            claimed = await asyncio.to_thread(self.moderator.store.claim_rate_limit, "chat_send", interaction.user.id, 2)
            if not claimed:
                return await interaction.response.send_message("That message is already being sent. Please wait a moment.", ephemeral=True)
            await interaction.channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.moderator.log(f"/chat could not send in {interaction.guild.name}: {exc}")
            return await interaction.response.send_message("I could not send that message here. Check my channel permissions.", ephemeral=True)
        self.moderator.log(f"/chat message sent by {interaction.user} in {interaction.guild.name}.")
        await interaction.response.send_message("Message sent.", ephemeral=True)

class TicketCreateView(discord.ui.View):
    """Persistent 'Create Ticket' dashboard button posted in the configured ticket channel."""
    def __init__(self, moderator):
        super().__init__(timeout=None); self.moderator = moderator
        button = discord.ui.Button(label="Create Ticket", style=discord.ButtonStyle.primary, emoji="\U0001f3ab", custom_id="ticket_dashboard:create")
        button.callback = self.open_ticket_modal; self.add_item(button)

    async def open_ticket_modal(self, interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in the configured support server.", ephemeral=True)
        configured_channel = self.moderator.settings.get("ticket_channels", {}).get(str(interaction.guild.id))
        if not configured_channel or str(interaction.channel_id) != str(configured_channel):
            return await interaction.response.send_message("Ticket creation is not available in this channel anymore.", ephemeral=True)
        await interaction.response.send_modal(TicketModal(self.moderator))


class TicketModal(discord.ui.Modal, title="Create Support Ticket"):
    def __init__(self, moderator):
        super().__init__(); self.moderator = moderator
        self.category = discord.ui.TextInput(label="Category", placeholder="Write your own support category", max_length=80)
        self.add_item(self.category)
        self.problem = discord.ui.TextInput(label="Describe your problem", style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(self.problem)

    async def on_submit(self, interaction):
        category = self.category.value.strip()
        if not category:
            return await interaction.response.send_message("Write your category before submitting.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            permitted = await asyncio.to_thread(self.moderator.store.claim_rate_limit, "ticket", interaction.user.id, 300)
            if not permitted:
                return await interaction.followup.send("You can create one support token every 5 minutes.", ephemeral=True)
            ticket = await asyncio.to_thread(self.moderator.store.create_ticket, interaction.guild.id, interaction.user.id, str(interaction.user), category, "", "", self.problem.value.strip())
        except Exception as exc:
            self.moderator.log(f"Ticket save failed: {exc}")
            return await interaction.followup.send("Ticket could not be saved. Please try again later.", ephemeral=True)
        try:
            ticket_channel = await self.moderator.create_ticket_channel(interaction.guild, interaction.channel, ticket)
        except Exception as exc:
            ticket_channel = None
            self.moderator.log(f"Could not create private channel for ticket #{ticket['ticket_id']}: {exc}")
        channel_line = f"\n\nPrivate chat: {ticket_channel.mention}" if ticket_channel else "\n\nA private chat could not be created; staff have been notified."
        confirmation = (
            f"Support ticket **#{ticket['ticket_id']}** was created. Category: **{category}**.\n\n"
            "Your ticket has been assigned to a support moderator. They will contact you on Discord shortly."
            + channel_line
        )
        ui_cancel_view = TicketCancelView(self.moderator, ticket["document_id"], interaction.user.id, ticket["ticket_id"])
        # Inbox is confirmation-only. The cancellation control stays in the server interaction.
        try: await interaction.user.send(confirmation)
        except discord.Forbidden: pass
        self.moderator.log(f"Ticket #{ticket['ticket_id']} created by {interaction.user}.")
        await interaction.followup.send(confirmation, ephemeral=True, view=ui_cancel_view)
        try:
            await self.moderator.send_ticket_copy_to_staff(ticket)
        except Exception as exc:
            self.moderator.log(f"Could not deliver ticket copy to staff: {exc}")


class TicketCancelView(discord.ui.View):
    def __init__(self, moderator, document_id, owner_id, ticket_id):
        super().__init__(timeout=86400); self.moderator, self.document_id, self.owner_id, self.ticket_id = moderator, document_id, owner_id, ticket_id
        button = discord.ui.Button(label="Cancel Ticket", style=discord.ButtonStyle.danger, emoji="\u274c")
        button.callback = self.request_cancel; self.add_item(button)

    async def request_cancel(self, interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Only the ticket owner can cancel this ticket.", ephemeral=True)
        # Re-check the live status first. Without this, a second click on an already-cancelled
        # ticket's button (e.g. two clicks before the first finished) would still open a confirm dialog.
        try:
            current = await asyncio.to_thread(self.moderator.store.get_ticket, self.document_id)
        except Exception as exc:
            return await interaction.response.send_message(f"Could not check the ticket's status: {exc}", ephemeral=True)
        if not current or current.get("status") != "open":
            status = current.get("status") if current else "missing"
            return await interaction.response.edit_message(content=f"Ticket #{self.ticket_id} is already **{status}**.", view=None)
        await interaction.response.send_message(
            f"Cancel support ticket #{self.ticket_id}? This cannot be undone.",
            view=TicketCancelConfirmView(self.moderator, self.document_id, self.owner_id, self.ticket_id, interaction.message),
            ephemeral=True,
        )


class TicketCancelConfirmView(discord.ui.View):
    def __init__(self, moderator, document_id, owner_id, ticket_id, origin_message=None):
        super().__init__(timeout=120); self.moderator, self.document_id, self.owner_id, self.ticket_id = moderator, document_id, owner_id, ticket_id
        # The original message that showed the "Cancel Ticket" button, so it can be stripped
        # of that button once the cancellation actually goes through or not otherwise the button
        # stays visible and clickable on a ticket that's already cancelled.
        self.origin_message = origin_message
        confirm = discord.ui.Button(label="Yes, Cancel Ticket", style=discord.ButtonStyle.danger)
        keep = discord.ui.Button(label="Keep Ticket Open", style=discord.ButtonStyle.secondary)
        confirm.callback = self.confirm_cancel; keep.callback = self.keep_ticket
        self.add_item(confirm); self.add_item(keep)

    async def _remove_cancel_button(self):
        if not self.origin_message:
            return
        try:
            await self.origin_message.edit(view=None)
        except Exception as exc:
            self.moderator.log(f"Could not remove the Cancel Ticket button after cancellation: {exc}")

    async def confirm_cancel(self, interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Only the ticket owner can cancel this ticket.", ephemeral=True)
        try:
            ticket = await asyncio.to_thread(self.moderator.store.cancel_ticket, self.document_id, self.owner_id)
        except Exception as exc:
            self.moderator.log(f"Ticket cancellation failed: {exc}")
            return await interaction.response.edit_message(content=f"Could not cancel the ticket: {exc}", view=None)
        # Database write succeeded or not remove the button now, regardless of whether the DM below succeeds,
        # so the ticket owner can never click "Cancel Ticket" again on a ticket that's already cancelled.
        await self._remove_cancel_button()
        try:
            await self.moderator.delete_ticket_channel(ticket)
        except Exception as exc:
            self.moderator.log(f"Could not delete cancelled ticket chat #{self.ticket_id}: {exc}")
        self.moderator.log(f"Ticket #{self.ticket_id} cancelled by its owner.")
        try:
            await interaction.user.send(f"Your support ticket **#{self.ticket_id}** has been cancelled successfully.")
            await interaction.response.edit_message(content=f"Ticket #{self.ticket_id} cancelled. A confirmation was sent to your inbox.", view=None)
        except discord.Forbidden:
            await interaction.response.edit_message(content=f"Ticket #{self.ticket_id} cancelled. Your DMs are closed, so inbox confirmation could not be sent.", view=None)

    async def keep_ticket(self, interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("Only the ticket owner can manage this ticket.", ephemeral=True)
        await interaction.response.edit_message(content="Ticket remains open.", view=None)


class BirthdayDashboardView(discord.ui.View):
    """Persistent 'Add Birthday' dashboard button posted in the configured birthday channel."""
    def __init__(self, moderator):
        super().__init__(timeout=None); self.moderator = moderator
        button = discord.ui.Button(label="Add Birthday", style=discord.ButtonStyle.primary, emoji="\U0001f382", custom_id="birthday_dashboard:add")
        button.callback = self.open_birthday_flow; self.add_item(button)

    async def open_birthday_flow(self, interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in the configured server.", ephemeral=True)
        configured_channel = self.moderator.settings.get("birthdays", {}).get(str(interaction.guild.id), {}).get("channel_id")
        if not configured_channel or str(interaction.channel_id) != str(configured_channel):
            return await interaction.response.send_message("Setting a birthday is not available in this channel anymore.", ephemeral=True)
        try:
            existing = await asyncio.to_thread(self.moderator.store.get_birthday, interaction.guild.id, interaction.user.id)
        except Exception as exc:
            return await interaction.response.send_message(f"Could not check your saved birthday: {exc}", ephemeral=True)
        if existing:
            return await interaction.response.send_message(
                f"You already have a birthday saved: **{format_birthday(existing)}**. Do you want to change it?",
                view=BirthdayChangeConfirmView(self.moderator, interaction.guild.id),
                ephemeral=True,
            )
        await interaction.response.send_modal(BirthdayModal(self.moderator, interaction.guild.id))


class BirthdayChangeConfirmView(discord.ui.View):
    def __init__(self, moderator, guild_id):
        super().__init__(timeout=120); self.moderator, self.guild_id = moderator, guild_id
        change = discord.ui.Button(label="Yes, Change Date", style=discord.ButtonStyle.danger)
        keep = discord.ui.Button(label="No, Keep It", style=discord.ButtonStyle.secondary)
        change.callback = self.change_date; keep.callback = self.keep_date
        self.add_item(change); self.add_item(keep)

    async def change_date(self, interaction):
        # A Modal can only be the *first* response to an interaction, so this has to
        # come from the button click itself rather than a follow-up edit.
        await interaction.response.send_modal(BirthdayModal(self.moderator, self.guild_id))

    async def keep_date(self, interaction):
        await interaction.response.edit_message(content="Your saved birthday was not changed.", view=None)


class BirthdayModal(discord.ui.Modal, title="Set Your Birthday"):
    def __init__(self, moderator, guild_id):
        super().__init__(); self.moderator, self.guild_id = moderator, guild_id
        self.day = discord.ui.TextInput(label="Day (1-31)", placeholder="e.g. 25", max_length=2)
        self.month = discord.ui.TextInput(label="Month (1-12)", placeholder="e.g. 12", max_length=2)
        self.year = discord.ui.TextInput(label="Year (optional)", placeholder="e.g. 2000", max_length=4, required=False)
        self.add_item(self.day); self.add_item(self.month); self.add_item(self.year)

    async def on_submit(self, interaction):
        try:
            day, month = int(self.day.value.strip()), int(self.month.value.strip())
            year_text = self.year.value.strip()
            year = int(year_text) if year_text else None
            datetime(year or 2000, month, day)  # Raises ValueError on an impossible day/month (or Feb 29 with a given non-leap year).
        except (ValueError, TypeError):
            return await interaction.response.send_message("Enter a valid day, month, and (if provided) year.", ephemeral=True)
        try:
            await asyncio.to_thread(self.moderator.store.save_birthday, self.guild_id, interaction.user.id, str(interaction.user), day, month, year)
        except Exception as exc:
            self.moderator.log(f"Could not save birthday: {exc}")
            return await interaction.response.send_message("Could not save your birthday. Please try again later.", ephemeral=True)
        shown = format_birthday({"day": day, "month": month, "year": year})
        self.moderator.log(f"{interaction.user} set their birthday to {shown}.")
        await interaction.response.send_message(f"\U0001f382 Your birthday has been set to **{shown}**. Only you can see this confirmation.", ephemeral=True)


class TicketStaffActionView(discord.ui.View):
    """Persistent Resolved/Closed controls sent alongside the full ticket copy in the staff DM."""
    def __init__(self, moderator, ticket):
        super().__init__(timeout=None)
        self.moderator, self.ticket = moderator, dict(ticket)
        document_id = self.ticket["document_id"]
        resolved_button = discord.ui.Button(label="Resolved", style=discord.ButtonStyle.success, emoji="\u2705",
                                             custom_id=f"ticket_staff:{document_id}:resolved")
        closed_button = discord.ui.Button(label="Closed", style=discord.ButtonStyle.danger, emoji="\U0001f512",
                                           custom_id=f"ticket_staff:{document_id}:closed")
        resolved_button.callback = self.mark_resolved
        closed_button.callback = self.mark_closed
        self.add_item(resolved_button); self.add_item(closed_button)

    async def _authorized_open_ticket(self, interaction):
        try:
            current = await asyncio.to_thread(self.moderator.store.get_ticket, self.ticket["document_id"])
        except Exception as exc:
            await interaction.response.send_message(f"Could not check the ticket's status: {exc}", ephemeral=True)
            return None
        if not current or current.get("status") != "open":
            status = current.get("status") if current else "missing"
            await interaction.response.send_message(f"This ticket is already **{status}**.", ephemeral=True)
            return None
        if str(interaction.user.id) != str(current.get("assigned_moderator_id", "")):
            await interaction.response.send_message("Only this ticket's assigned moderator can use these controls.", ephemeral=True)
            return None
        return current

    async def mark_resolved(self, interaction):
        ticket = await self._authorized_open_ticket(interaction)
        if ticket:
            await interaction.response.send_modal(TicketStaffNoteModal(self.moderator, ticket, "resolved", interaction.message))

    async def mark_closed(self, interaction):
        ticket = await self._authorized_open_ticket(interaction)
        if ticket:
            await interaction.response.send_modal(TicketStaffNoteModal(self.moderator, ticket, "closed", interaction.message))


class TicketStaffNoteModal(discord.ui.Modal):
    """Note the staff member writes when marking a ticket Resolved or Closed from their DM."""
    def __init__(self, moderator, ticket, action, origin_message):
        super().__init__(title="Resolve Ticket" if action == "resolved" else "Close Ticket")
        self.moderator, self.ticket, self.action, self.origin_message = moderator, ticket, action, origin_message
        default_note = "" if action == "resolved" else DEFAULT_TICKET_CLOSE_NOTE
        self.note = discord.ui.TextInput(
            label="Note to send the user", style=discord.TextStyle.paragraph,
            default=default_note, max_length=1000, required=False,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction):
        note = self.note.value.strip()
        if self.action == "closed" and not note:
            note = DEFAULT_TICKET_CLOSE_NOTE
        try:
            ticket = await asyncio.to_thread(
                self.moderator.store.set_ticket_status, self.ticket["document_id"], self.action,
                f"Discord ({interaction.user})", note,
            )
        except Exception as exc:
            return await interaction.response.send_message(f"Could not update the ticket: {exc}", ephemeral=True)
        await interaction.response.send_message(
            f"Ticket #{ticket['ticket_id']} marked **{self.action}** and the user was notified.", ephemeral=True,
        )
        try:
            await self.moderator.notify_ticket_status(ticket)
        except Exception as exc:
            self.moderator.log(f"Could not DM ticket owner about #{ticket['ticket_id']}: {exc}")
        await self.moderator.delete_ticket_channel(ticket)
        if self.origin_message:
            try:
                embed = self.origin_message.embeds[0].copy() if self.origin_message.embeds else discord.Embed(title=f"Ticket #{ticket['ticket_id']}")
                embed.colour = discord.Colour.green() if self.action == "resolved" else discord.Colour.red()
                embed.add_field(name="Status", value=self.action.title(), inline=True)
                embed.add_field(name="Note sent to user", value=note or "-", inline=False)
                await self.origin_message.edit(embed=embed, view=None)
            except Exception as exc:
                self.moderator.log(f"Could not update the staff ticket message: {exc}")
        self.moderator.log(f"Ticket #{ticket['ticket_id']} marked {self.action} by {interaction.user} from Discord.")


class TempVoiceNameModal(discord.ui.Modal, title="Rename Temporary Voice Room"):
    def __init__(self, moderator, channel_id, current_name):
        super().__init__(); self.moderator, self.channel_id = moderator, channel_id
        self.name = discord.ui.TextInput(label="Room name", default=current_name, min_length=1, max_length=100)
        self.add_item(self.name)

    async def on_submit(self, interaction):
        try:
            await self.moderator.rename_temp_room(interaction, self.channel_id, self.name.value.strip())
            await interaction.response.send_message("\u2705 Voice room renamed.", ephemeral=True)
        except PermissionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
        except Exception as exc:
            self.moderator.log(f"Temporary room rename failed: {exc}")
            await interaction.response.send_message("Could not rename this voice room.", ephemeral=True)


class TempVoiceLimitModal(discord.ui.Modal, title="Set Temporary Voice Member Limit"):
    def __init__(self, moderator, channel_id, current_limit):
        super().__init__(); self.moderator, self.channel_id = moderator, channel_id
        self.limit = discord.ui.TextInput(label="Member limit", default=str(current_limit or 0), placeholder="0 for unlimited; otherwise 1-99", max_length=2)
        self.add_item(self.limit)

    async def on_submit(self, interaction):
        try:
            number = int(self.limit.value.strip())
            if not 0 <= number <= 99:
                raise ValueError
        except ValueError:
            return await interaction.response.send_message("Enter a whole number from 0 to 99.", ephemeral=True)
        try:
            await self.moderator.set_temp_room_limit(interaction, self.channel_id, number)
            display_limit = "unlimited" if number == 0 else str(number)
            await interaction.response.send_message(f"\U0001f465 Member limit set to **{display_limit}**.", ephemeral=True)
        except PermissionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
        except Exception as exc:
            self.moderator.log(f"Temporary room limit update failed: {exc}")
            await interaction.response.send_message("Could not update the member limit.", ephemeral=True)


class TempVoiceControlView(discord.ui.View):
    """Persistent control dashboard displayed in each temporary voice channel chat."""
    def __init__(self, moderator, room):
        super().__init__(timeout=None); self.moderator, self.room = moderator, dict(room)
        channel_id = self.room["channel_id"]
        controls = (
            ("\U0001f512 Lock", discord.ButtonStyle.danger, "lock", self.lock),
            ("\U0001f513 Unlock", discord.ButtonStyle.success, "unlock", self.unlock),
            ("\u270f\ufe0f Rename", discord.ButtonStyle.primary, "rename", self.rename),
            ("\U0001f465 Max", discord.ButtonStyle.secondary, "max", self.max_members),
        )
        for label, style, action, callback in controls:
            button = discord.ui.Button(label=label, style=style, custom_id=f"tempvoice:{channel_id}:{action}")
            button.callback = callback; self.add_item(button)

    async def _room(self, interaction):
        return await self.moderator.dashboard_room_for_interaction(interaction, self.room["channel_id"])

    async def lock(self, interaction):
        try:
            room = await self._room(interaction)
            await self.moderator.set_temp_room_lock(interaction, room, True)
            await interaction.response.edit_message(content=self.moderator.dashboard_text(room, locked=True), view=TempVoiceControlView(self.moderator, room))
        except PermissionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    async def unlock(self, interaction):
        try:
            room = await self._room(interaction)
            await self.moderator.set_temp_room_lock(interaction, room, False)
            await interaction.response.edit_message(content=self.moderator.dashboard_text(room, locked=False), view=TempVoiceControlView(self.moderator, room))
        except PermissionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    async def rename(self, interaction):
        try:
            room = await self._room(interaction)
            await interaction.response.send_modal(TempVoiceNameModal(self.moderator, room["channel_id"], room.get("name", "")))
        except PermissionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)

    async def max_members(self, interaction):
        try:
            room = await self._room(interaction)
            await interaction.response.send_modal(TempVoiceLimitModal(self.moderator, room["channel_id"], room.get("user_limit", 0)))
        except PermissionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)



    async def invite(self, interaction):
        try:
            room = await self._room(interaction)
            await interaction.response.send_modal(TempVoiceInviteModal(self.moderator, room["channel_id"]))
        except PermissionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)


class DragMemberView(discord.ui.View):
    """Private two-step picker for moving voice members."""
    def __init__(self, moderator, guild, requester_id):
        super().__init__(timeout=300)
        self.moderator, self.guild, self.requester_id = moderator, guild, requester_id
        self.members = [member for channel in guild.voice_channels for member in channel.members if not member.bot]
        self.member_ids, self.selected_ids = {member.id for member in self.members}, set()
        options = [discord.SelectOption(label=member.display_name[:100], value=str(member.id), description=(member.voice.channel.name if member.voice and member.voice.channel else "Voice")[:100]) for member in self.members[:25]]
        self.member_select = discord.ui.Select(placeholder="Select voice members (first 25 shown)", min_values=0, max_values=max(1, len(options)), options=options, disabled=not options)
        self.member_select.callback = self.select_members; self.add_item(self.member_select)
        all_button = discord.ui.Button(label="Select All", style=discord.ButtonStyle.secondary, emoji="\U0001f465"); all_button.callback = self.select_all; self.add_item(all_button)
        continue_button = discord.ui.Button(label="Choose target VC", style=discord.ButtonStyle.primary, emoji="\u27a1\ufe0f"); continue_button.callback = self.choose_target; self.add_item(continue_button)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id and self.moderator.drag_allowed(interaction.user): return True
        await interaction.response.send_message("Only the authorized /drag user can use this picker.", ephemeral=True); return False

    def prompt(self): return f"Select voice members, then choose the target VC. Selected: **{len(self.selected_ids)}**."
    async def select_members(self, interaction):
        self.selected_ids = {int(value) for value in self.member_select.values}
        await interaction.response.edit_message(content=self.prompt(), view=self)
    async def select_all(self, interaction):
        self.selected_ids = set(self.member_ids)
        await interaction.response.edit_message(content=self.prompt(), view=self)
    async def choose_target(self, interaction):
        if not self.selected_ids: return await interaction.response.send_message("Select at least one member or press Select All first.", ephemeral=True)
        await interaction.response.edit_message(content="Choose the voice channel to move the selected members into:", view=DragTargetView(self.moderator, self.guild, self.requester_id, self.selected_ids))

class DragTargetView(discord.ui.View):
    def __init__(self, moderator, guild, requester_id, member_ids):
        super().__init__(timeout=300); self.moderator, self.guild, self.requester_id, self.member_ids = moderator, guild, requester_id, set(member_ids)
        channels = list(guild.voice_channels)[:25]
        options = [discord.SelectOption(label=channel.name[:100], value=str(channel.id), description=f"{len(channel.members)} member(s)") for channel in channels]
        selector = discord.ui.Select(placeholder="Choose target voice channel", min_values=1, max_values=1, options=options, disabled=not options)
        selector.callback = self.move_members; self.add_item(selector)
    async def interaction_check(self, interaction):
        if interaction.user.id == self.requester_id and self.moderator.drag_allowed(interaction.user): return True
        await interaction.response.send_message("Only the authorized /drag user can use this picker.", ephemeral=True); return False
    async def move_members(self, interaction):
        target = self.guild.get_channel(int(interaction.data["values"][0]))
        if not isinstance(target, discord.VoiceChannel): return await interaction.response.send_message("That voice channel is no longer available.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True); moved, skipped = 0, 0
        for member_id in self.member_ids:
            member = self.guild.get_member(member_id)
            if not member or not member.voice or member.voice.channel == target: skipped += 1; continue
            try: await member.move_to(target, reason=f"/drag requested by {interaction.user}"); moved += 1
            except (discord.Forbidden, discord.HTTPException): skipped += 1
        self.moderator.log(f"/drag by {interaction.user} in {self.guild.name}: moved {moved} member(s) to {target.name}; skipped {skipped}.")
        await interaction.followup.send(f"Moved **{moved}** member(s) to {target.mention}. Skipped: **{skipped}**.", ephemeral=True)
class ModeratorBot:
    """The single moderation implementation shared by GUI and Railway."""
    def __init__(self, store, source, log=print, event=None):
        self.store, self.source, self.log = store, source, log
        self.event = event or (lambda *_: None)
        self.settings = store.get_settings()
        self.bot = None
        self.loop = None
        self.last_action, self.actions = {}, []
        self.counting_locks = {}
        self.dashboard_views_registered = set()
        # `on_ready` may be called again after a Discord gateway resume. The
        # GUI inventory should be refreshed each time, but background loops must
        # only exist once or a reconnect would create duplicate workers.
        self.background_tasks_started = False

    async def track_member_voice_activity(self, member, before, after):
        """Record voice joins, moves, leaves, and time spent in this server only."""
        if member.bot or before.channel == after.channel:
            return
        if before.channel:
            session = await asyncio.to_thread(self.store.end_member_voice_session, member.guild.id, member.id)
            if session:
                started = datetime.fromisoformat(session["started_at"])
                duration = max(0, round((datetime.now(timezone.utc) - started).total_seconds()))
                await asyncio.to_thread(self.store.record_member_activity, member.guild.id, member.id, str(member), "voice_left", {"channel": session.get("channel_name", before.channel.name), "duration_seconds": duration})
        if after.channel:
            started = await asyncio.to_thread(self.store.start_member_voice_session, member.guild.id, member.id, str(member), after.channel.name)
            if started:
                await asyncio.to_thread(self.store.record_member_activity, member.guild.id, member.id, str(member), "voice_joined", {"channel": after.channel.name})

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
                    self.log(f"Posted MetaVerse Bangladesh contract notice in #{channel.name} for {guild.name} ({guild.id}).")
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
        suffix = " …" if len(roles) > 5 else ""
        return (
            f"Cannot manage Auto Role(s): {names}{suffix}. Move the bot's highest role above "
            "these roles in Server Settings → Roles, and ensure it has Manage Roles."
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

        Railway's slim Linux image can occasionally be missing the system IANA zone
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

    def blocked_words(self, guild_id):
        """Return the shared per-server word-block list, normalized for matching."""
        return list(dict.fromkeys(
            str(word).strip().casefold()
            for word in self.settings.get("word_blocks", {}).get(str(guild_id), [])
            if str(word).strip()
        ))

    async def handle_word_block(self, message):
        """Delete a blocked message and permanently ban at the fifth shared strike."""
        content = message.content.casefold()
        matched = next((word for word in self.blocked_words(message.guild.id)
                        if re.search(rf"(?<!\w){re.escape(word)}(?!\w)", content)), None)
        if not matched:
            return False
        try:
            violation = await asyncio.to_thread(
                self.store.record_word_block_violation, message.guild.id, message.author.id, matched, message.id,
            )
            if violation.get("duplicate"):
                return True
            strikes = int(violation["count"])
            await asyncio.to_thread(
                self.store.record_member_activity, message.guild.id, message.author.id, str(message.author),
                "word_spam", {"word": matched, "strikes": strikes, "channel": getattr(message.channel, "name", "unknown"), "content": message.content[:1000]},
                f"word_spam_{message.id}",
            )
        except Exception as exc:
            self.log(f"Could not record blocked-word violation in {message.guild.name}: {exc}")
            return False
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass
        if strikes < 5:
            self.log(f"Blocked word '{matched}' from {message.author} in {message.guild.name} ({strikes}/5).")
            return True
        if message.author.id in PROTECTED_USER_IDS:
            self.log(f"Blocked-word threshold reached by protected user {message.author}; ban skipped.")
            return True
        try:
            await message.author.ban(reason=f"Permanent ban: 5 blocked-word violations (latest: {matched})", delete_message_seconds=0)
            await asyncio.to_thread(
                self.store.record_member_activity, message.guild.id, message.author.id, str(message.author),
                "word_spam_banned", {"word": matched, "strikes": strikes}, f"word_spam_banned_{message.id}",
            )
            self.log(f"Permanently banned {message.author} in {message.guild.name} after {strikes} blocked-word violations.")
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.log(f"Could not ban blocked-word spammer {message.author} in {message.guild.name}: {exc}")
        return True
    def default_role_ids(self, guild_id):
        """Return the configured auto-role ids for a server (strings)."""
        return list(self.settings.get("default_roles", {}).get(str(guild_id), []))

    def managed_default_role_ids(self, guild_id):
        active = self.default_role_ids(guild_id)
        return list(dict.fromkeys(map(str, self.settings.get("default_role_managed_ids", {}).get(str(guild_id), active))))

    async def apply_default_roles(self, member):
        """Give a newly-joined member their server's configured default role(s).

        Safe to call even if nothing is configured, a role was deleted, or the
        member already has some of the roles — those are simply skipped.
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
        embed.set_footer(text=f"{guild.name} • {footer}")
        try:
            await channel.send(embed=embed)
            self.log(f"{member} reached level {result['level']} in {guild.name}.")
        except (discord.Forbidden, discord.HTTPException) as exc:
            self.log(f"Could not send level-up message: {exc}")

    async def handle_counting_message(self, message):
        """Validate 1, 2, 3… in the configured channel with a shared Cloudflare R2 state."""
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
            channel_id = str(config.get("channel_id", ""))
            if not channel_id:
                continue
            guild = self.bot.get_guild(int(guild_id_str))
            if not guild:
                continue
            channel = guild.get_channel(int(channel_id))
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
            # Greeting must not disappear just because the anti-duplicate store is temporarily unavailable.
            self.log(f"Could not claim {kind} greeting event; sending once anyway: {exc}")
            claimed = True
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

    async def refresh_settings(self):
        while not self.bot.is_closed():
            await asyncio.sleep(10)
            try:
                fresh = self.store.get_settings()
                if fresh["settings_revision"] != self.settings["settings_revision"]:
                    token_changed = fresh.get("token") != self.settings.get("token")
                    channels_changed = fresh.get("ticket_channels") != self.settings.get("ticket_channels")
                    birthday_channels_changed = fresh.get("birthdays") != self.settings.get("birthdays")
                    self.settings = fresh
                    self.log("Settings synchronized from Cloudflare R2.")
                    if token_changed:
                        self.log("Discord token changed in Cloudflare R2; reconnecting with the new token.")
                        await self.bot.close()
                        return
                    if channels_changed:
                        await self.ensure_ticket_dashboards()
                    if birthday_channels_changed:
                        await self.ensure_birthday_dashboards()
            except Exception as exc:
                self.log(f"Cloudflare R2 settings refresh failed: {exc}")

    def build(self):
        intents = discord.Intents.default(); intents.message_content = True; intents.members = True
        bot = commands.Bot(command_prefix="!", intents=intents)
        self.bot = bot

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
            if not self.background_tasks_started:
                self.background_tasks_started = True
                asyncio.create_task(self.refresh_settings())
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
            # Keep R2 activity logging off the critical temporary-room path.
            asyncio.create_task(self.track_member_voice_activity(member, before, after))
            await self.handle_temp_voice_update(member, before, after)
        @bot.event
        async def on_member_join(member):
            self.log(f"Member join event received: {member} ({member.id}) in {member.guild.name}; bot={member.bot}")
            # Welcome is sent for every joining member, including bot accounts.
            await self.send_member_greeting(member, "welcome")
            if member.bot:
                return
            asyncio.create_task(asyncio.to_thread(self.store.record_member_activity, member.guild.id, member.id, str(member), "member_joined", {}))
            await self.apply_default_roles(member)
        @bot.event
        async def on_member_remove(member):
            await asyncio.to_thread(self.store.record_member_activity, member.guild.id, member.id, str(member), "member_left", {})
            await self.send_member_greeting(member, "goodbye")

        @bot.event
        async def on_message_delete(message):
            # Cached messages retain their text, so manual and bot deletions remain
            # visible to the server's saved monitoring targets.
            if message.author.bot or not message.guild:
                return
            await asyncio.to_thread(
                self.store.record_member_activity, message.guild.id, message.author.id, str(message.author),
                "message_deleted", {"channel": getattr(message.channel, "name", "unknown"), "content": message.content[:1000]},
                f"message_deleted_{message.id}",
            )

        @bot.event
        async def on_message(message):
            if message.author.bot or not message.guild:
                return await bot.process_commands(message)
            asyncio.create_task(asyncio.to_thread(self.store.record_member_activity, message.guild.id, message.author.id, str(message.author), "message", {"channel": getattr(message.channel, "name", "unknown"), "content": message.content[:1000]}, f"message_{message.id}"))
            if await self.handle_word_block(message):
                return
            # Discord invites are always blocked. Discord does not expose an invite's
            # destination without resolving it, so this safely blocks all server invites.
            if DISCORD_INVITE_PATTERN.search(message.content):
                try:
                    await message.delete()
                    await asyncio.to_thread(self.store.record_member_activity, message.guild.id, message.author.id, str(message.author), "discord_invite_blocked", {"channel": getattr(message.channel, "name", "unknown"), "content": message.content[:1000]}, f"invite_{message.id}")
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
            await interaction.response.send_message("Select voice members to move. You are included whenever you are in voice.", ephemeral=True, view=DragMemberView(self, interaction.guild, interaction.user.id))
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
        staff_ids=set(TICKET_CHANNEL_STAFF_IDS)
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
        ticket = await asyncio.to_thread(self.store.update_ticket_fields, ticket["document_id"], {"ticket_channel_id": str(channel.id)})
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
    async def send_ticket_copy_to_staff(self, ticket):
        """DM the ticket to its atomically assigned moderator."""
        staff_id = str(ticket.get("assigned_moderator_id", ""))
        if not staff_id.isdigit():
            self.log(f"Ticket #{ticket['ticket_id']} has no assigned moderator.")
            return
        try:
            staff = self.bot.get_user(int(staff_id)) or await self.bot.fetch_user(int(staff_id))
        except discord.NotFound:
            self.log(f"Assigned ticket moderator {staff_id} was not found.")
            return
        category = ticket["category"] + (f" ? {ticket['other_category']}" if ticket.get("other_category") else "")
        embed = discord.Embed(title="\U0001f3ab New Support Ticket #" + str(ticket.get("ticket_id")), colour=discord.Colour.blurple())
        embed.add_field(name="From", value=f"{ticket['username']} (`{ticket['user_id']}`)", inline=False)
        embed.add_field(name="Category", value=category or "-", inline=False)
        category = ticket["category"] + (" - " + str(ticket["other_category"]) if ticket.get("other_category") else "")
        embed = discord.Embed(title="Ticket #" + str(ticket.get("ticket_id")), colour=discord.Colour.blurple())
        try:
            message = await staff.send(embed=embed, view=TicketStaffActionView(self, ticket))
        except discord.Forbidden:
            self.log(f"Could not DM ticket #{ticket['ticket_id']} to assigned moderator {staff_id} (their DMs are closed).")
            return
        try:
            await asyncio.to_thread(self.store.update_ticket_fields, ticket["document_id"], {"staff_dm_channel_id": str(message.channel.id), "staff_dm_message_id": str(message.id)})
        except Exception as exc:
            self.log(f"Could not save the ticket's staff DM reference: {exc}")
    async def restore_ticket_staff_dashboards(self):
        """Re-register the persistent Resolved/Closed buttons on open tickets after a restart."""
        try:
            tickets = await asyncio.to_thread(self.store.list_tickets, 500)
            restored = 0
            for ticket in tickets:
                if ticket.get("status") != "open" or not ticket.get("staff_dm_message_id"):
                    continue
                self.bot.add_view(TicketStaffActionView(self, ticket), message_id=int(ticket["staff_dm_message_id"]))
                restored += 1
            if restored:
                self.log(f"Restored {restored} ticket staff dashboard button(s).")
        except Exception as exc:
            self.log(f"Ticket staff dashboard restore failed: {exc}")

    async def notify_ticket_status(self, ticket):
        user = self.bot.get_user(int(ticket["user_id"])) or await self.bot.fetch_user(int(ticket["user_id"]))
        note = ticket.get("resolution_note") or "No additional note."
        if ticket["status"] == "resolved":
            await user.send(f"Your support ticket **#{ticket['ticket_id']}** has been resolved. Note: {note}")
        elif ticket["status"] == "closed":
            await user.send(f"Your support ticket **#{ticket['ticket_id']}** has been closed.\n\n{note}")
        else:
            await user.send(f"Your support ticket **#{ticket['ticket_id']}** status changed to **{ticket['status']}**. Note: {note}")

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
        await asyncio.to_thread(self.store.record_member_activity, guild_id, user_id, str(member), f"member_{action}ed", {"reason": reason or "GUI moderation action"})
        self.log(f"{action.title()}ed {member} from {guild.name}.")

class TempVoiceInviteModal(discord.ui.Modal, title="Invite member to temporary VC"):
    def __init__(self, moderator, channel_id):
        super().__init__()
        self.moderator = moderator
        self.channel_id = channel_id
        self.user_id = discord.ui.TextInput(label="Member Discord ID", placeholder="123456789012345678", max_length=20)
        self.add_item(self.user_id)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            room = await self.moderator.dashboard_room_for_interaction(interaction, self.channel_id)
            member = await self.moderator.invite_temp_room_member(interaction, room, self.user_id.value.strip())
            await interaction.followup.send(f"\u2705 {member.mention} can now enter this temporary voice room, including when it is locked.", ephemeral=True)
        except (PermissionError, discord.Forbidden, discord.HTTPException) as exc:
            await interaction.followup.send(f"Could not invite that member: {exc}", ephemeral=True)










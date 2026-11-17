"""Tickets feature module."""

import asyncio

import discord


DEFAULT_TICKET_CLOSE_NOTE = (
    "Sorry for the inconvenience. This ticket has been closed. If you still need help, "
    "please feel free to open a new ticket and our support team will be happy to assist you."
)


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
        try:
            staff = await asyncio.to_thread(self.moderator.store.ticket_moderators, interaction.guild.id)
        except Exception as exc:
            self.moderator.log(f"Could not check ticket staff for {interaction.guild.name}: {exc}")
            return await interaction.response.send_message("Ticket creation is temporarily unavailable. Please try again later.", ephemeral=True)
        if not staff:
            return await interaction.response.send_message("Ticket creation is unavailable because no ticket staff are configured. Ask an administrator to add a moderator in the GUI.", ephemeral=True)
        await interaction.response.send_modal(TicketModal(self.moderator))


class TicketModal(discord.ui.Modal, title="Create Support Ticket"):
    def __init__(self, moderator):
        super().__init__(); self.moderator = moderator
        self.category = discord.ui.TextInput(label="Category", placeholder="Write your own support category", max_length=80)
        self.add_item(self.category)
        self.problem = discord.ui.TextInput(label="Describe your problem", style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(self.problem)

    async def on_submit(self, interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Tickets can only be created in the configured support server.", ephemeral=True)
        category = self.category.value.strip()
        if not category:
            return await interaction.response.send_message("Write your category before submitting.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            staff = await asyncio.to_thread(self.moderator.store.ticket_moderators, interaction.guild.id)
            if not staff:
                return await interaction.followup.send("Ticket creation is unavailable because no ticket staff are configured. Ask an administrator to add a moderator in the GUI.", ephemeral=True)
            permitted = await asyncio.to_thread(self.moderator.store.claim_rate_limit, "ticket", interaction.user.id, 300)
            if not permitted:
                return await interaction.followup.send("You can create one support ticket every 5 minutes.", ephemeral=True)
            ticket = await asyncio.to_thread(self.moderator.store.create_ticket, interaction.guild.id, interaction.user.id, str(interaction.user), category, "", "", self.problem.value.strip())
        except Exception as exc:
            self.moderator.log(f"Ticket save failed: {exc}")
            if "No ticket moderators are configured" in str(exc):
                return await interaction.followup.send("Ticket creation is unavailable because no ticket staff are configured. Ask an administrator to add a moderator in the GUI.", ephemeral=True)
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
        transfer_button = discord.ui.Button(label="Change Staff", style=discord.ButtonStyle.secondary, emoji="\U0001f504",
                                            custom_id=f"ticket_staff:{document_id}:transfer")
        resolved_button.callback = self.mark_resolved
        closed_button.callback = self.mark_closed
        transfer_button.callback = self.change_staff
        self.add_item(resolved_button); self.add_item(closed_button); self.add_item(transfer_button)

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

    async def change_staff(self, interaction):
        ticket=await self._authorized_open_ticket(interaction)
        if not ticket:return
        await interaction.response.defer(ephemeral=True,thinking=True)
        guild=self.moderator.bot.get_guild(int(ticket["guild_id"]))
        if not guild:
            return await interaction.followup.send("The support server is unavailable right now.",ephemeral=True)
        staff_ids=list(dict.fromkeys(map(str,self.moderator.settings.get("ticket_moderators",{}).get(str(guild.id),[]))))
        options=[]
        for staff_id in staff_ids:
            if staff_id==str(ticket.get("assigned_moderator_id")):continue
            if not staff_id.isdigit():continue
            staff_member=guild.get_member(int(staff_id))
            if not staff_member:
                try:staff_member=await guild.fetch_member(int(staff_id))
                except (discord.NotFound,discord.Forbidden,discord.HTTPException):continue
            options.append(discord.SelectOption(label=staff_member.display_name[:100],value=staff_id,description=f"{staff_member} • Available support staff"[:100]))
            if len(options)>=25:break
        if not options:
            return await interaction.followup.send("There are no other available ticket moderators to transfer this ticket to.",ephemeral=True)
        view=TicketTransferView(self.moderator,ticket,interaction.user.id,interaction.message,options)
        await interaction.followup.send("Choose the new moderator for this ticket:",view=view,ephemeral=True)


class TicketTransferView(discord.ui.View):
    """Private staff picker for transferring an open ticket."""
    def __init__(self,moderator,ticket,requester_id,origin_message,options):
        super().__init__(timeout=120)
        self.moderator,self.ticket,self.requester_id,self.origin_message=moderator,dict(ticket),requester_id,origin_message
        self.staff_select=discord.ui.Select(placeholder="Select available support staff",min_values=1,max_values=1,options=options)
        self.staff_select.callback=self.transfer
        self.add_item(self.staff_select)

    async def interaction_check(self,interaction):
        if interaction.user.id==self.requester_id:return True
        await interaction.response.send_message("Only the moderator who opened this transfer picker can use it.",ephemeral=True)
        return False

    async def transfer(self,interaction):
        new_staff_id=self.staff_select.values[0]
        await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            updated=await self.moderator.transfer_ticket(self.ticket,new_staff_id,self.requester_id,self.origin_message)
        except Exception as exc:
            return await interaction.followup.send(f"Ticket transfer failed: {exc}",ephemeral=True)
        self.stop()
        await interaction.followup.send(f"Ticket **#{updated['ticket_id']}** was transferred successfully.",ephemeral=True)


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


class TicketRatingView(discord.ui.View):
    """Persistent private 1–5 star rating prompt sent to a closed ticket's owner."""
    def __init__(self, moderator, ticket):
        super().__init__(timeout=None)
        self.moderator,self.ticket=moderator,dict(ticket)
        for stars in range(1,6):
            button=discord.ui.Button(label=f"{stars} Star" if stars==1 else f"{stars} Stars",style=discord.ButtonStyle.secondary,custom_id=f"ticket_rating:{ticket['document_id']}:{stars}")
            button.callback=self.make_callback(stars)
            self.add_item(button)

    def make_callback(self,stars):
        async def choose(interaction):
            if str(interaction.user.id)!=str(self.ticket.get("user_id")):
                return await interaction.response.send_message("Only the ticket owner can submit this rating.",ephemeral=True)
            try:
                existing=await asyncio.to_thread(self.moderator.store.get_ticket_review,self.ticket["document_id"])
            except Exception as exc:
                return await interaction.response.send_message("Could not check the review status. Please try again later.",ephemeral=True)
            if existing:
                return await interaction.response.send_message("A review has already been submitted for this ticket.",ephemeral=True)
            await interaction.response.send_modal(TicketRatingModal(self.moderator,self.ticket,stars,interaction.message))
        return choose


class TicketRatingModal(discord.ui.Modal,title="Rate Your Support Experience"):
    def __init__(self,moderator,ticket,stars,origin_message):
        super().__init__()
        self.moderator,self.ticket,self.stars,self.origin_message=moderator,ticket,stars,origin_message
        self.note=discord.ui.TextInput(label="Optional feedback for the support team",style=discord.TextStyle.paragraph,max_length=1000,required=False,placeholder="Tell us how the support experience went.")
        self.add_item(self.note)

    async def on_submit(self,interaction):
        try:
            result=await asyncio.to_thread(self.moderator.store.save_ticket_review,self.ticket["document_id"],interaction.user.id,self.stars,self.note.value.strip())
        except Exception as exc:
            self.moderator.log(f"Could not save ticket #{self.ticket['ticket_id']} review: {exc}")
            return await interaction.response.send_message("Your review could not be saved. Please try again later.",ephemeral=True)
        if not result.get("saved"):
            return await interaction.response.send_message("A review has already been submitted for this ticket.",ephemeral=True)
        review=result["review"]
        await interaction.response.send_message("Thank you. Your rating and feedback were sent to the support team.",ephemeral=True)
        try:
            await self.moderator.send_ticket_review_to_staff(self.ticket,review)
        except Exception as exc:
            self.moderator.log(f"Could not deliver ticket #{self.ticket['ticket_id']} review to staff: {exc}")
        if self.origin_message:
            try:await self.origin_message.edit(content=f"Thank you for rating support ticket **#{self.ticket['ticket_id']}** ({self.stars}/5 stars).",view=None)
            except discord.HTTPException:pass

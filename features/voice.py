"""Voice feature module."""

import discord


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

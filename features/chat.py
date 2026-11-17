"""Chat feature module."""

import asyncio

import discord


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

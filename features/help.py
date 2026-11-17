"""Help feature module."""

import discord

from config.app_config import APP_CONFIG


HELP_PAGES = [
    ("QUICK START", "Welcome to the server bot guide. Use the buttons below to browse each feature.", [
        ("Commands", "Run **`/leaderboard`** to see the top 10 members by lifetime XP in a high-resolution image. Server-specific GIF commands appear in Discord's slash-command menu; choose a member when prompted."),
        ("Channel buttons", "Use the **Create Ticket** button to contact support and the **Add Birthday** button to submit your birthday privately. These buttons are posted in channels selected by the server team."),
        ("Server setup", "Most features are configured by server admins in the desktop control panel. If a button or command is missing, ask an admin whether it has been enabled and configured."),
    ]),
    ("LEVELS & LEADERBOARD", "Earn XP through meaningful server activity and climb the shared server ranking.", [
        ("Text XP", "Eligible messages earn XP after the cooldown. Very short messages and repeated rapid messages do not count. Longer meaningful messages can earn a small bonus."),
        ("Voice XP", "Voice XP is earned on its own timer. By default, another non-bot member must be in the channel, and deafened members are skipped to discourage idle farming."),
        ("Leaderboard", "Use **`/leaderboard`** for the top 10 by total lifetime XP. The image shows each member's level and profile picture, plus the server logo. Leveling settings are managed by admins."),
    ]),
    ("SUPPORT, BIRTHDAYS & COMMUNITY", "Use the persistent channel buttons and configured community channels.", [
        ("Support tickets", "Click **Create Ticket**, choose a category, describe the issue, and confirm. Your ticket is sent privately to the support team. Use the cancellation button in your confirmation if you no longer need help."),
        ("Birthdays", "Click **Add Birthday**, type your day and month, and optionally enter your birth year. The confirmation is private. If you already have a birthday saved, the bot asks before changing it. Birthday wishes are posted in the channel chosen by admins."),
        ("Counting & greetings", "In the configured counting channel, post the next number in order. Welcome and goodbye messages are sent automatically when members join or leave."),
        ("Auto roles", "New members receive the roles selected by server admins automatically. Members do not need to run a command."),
    ]),
    ("VOICE & SCHEDULED POSTS", "Voice rooms and announcements follow the schedule and channels selected by admins.", [
        ("Temporary voice rooms", "Join the configured voice lobby to create your own temporary room. Use its room controls to lock/unlock it, rename it, set a member limit, or invite someone. Empty temporary rooms are removed automatically."),
        ("Announcements", "Movie posts and role notifications are sent automatically to their configured channels and schedules. Some schedules repeat by day or weekday; one-time movie announcements fire once."),
    ]),
    ("SAFETY & MODERATION", "The bot applies the server's saved moderation rules and protects configured server spaces.", [
        ("Link and word rules", "Only configured roles may post links where link filtering is enabled. Configured blocked words are removed and logged; repeated blocked-word violations can lead to an automatic permanent ban."),
        ("Moderation controls", "Server admins manage warning thresholds, timeout settings, protected accounts, and manual moderation actions through the desktop control panel."),
    ]),
    ("ADMIN SETUP & SYNC", "The desktop panel configures features; the hosted worker keeps the shared bot online.", [
        ("Configure", "Connect the desktop control panel, choose a server, open the relevant settings tab, and save. Discord permissions and role hierarchy may limit which actions the bot can perform."),
        ("Hosting", "Run one persistent Railway worker. Saved configuration and leveling data are shared through Cloudflare and normally synchronize within about 10 seconds."),
        ("Need help?", "If a feature is unavailable, contact a server admin. They can check the channel configuration, role permissions, bot permissions, and hosted worker status."),
    ]),
]


def make_help_embeds(guild):
    embeds = []
    for index, (title, description, fields) in enumerate(HELP_PAGES):
        embed = discord.Embed(
            title=f"{APP_CONFIG.bot_app_name} | {title}",
            description=description,
            colour=discord.Colour.from_rgb(18, 28, 46),
        )
        if guild and guild.icon:
            embed.set_author(name=f"{APP_CONFIG.bot_app_name} | Bot Guide", icon_url=str(guild.icon.url))
            embed.set_thumbnail(url=str(guild.icon.url))
        else:
            embed.set_author(name=f"{APP_CONFIG.bot_app_name} | Bot Guide")
        for name, value in fields:
            embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text=f"{APP_CONFIG.bot_app_name} | Page {index + 1} of {len(HELP_PAGES)} | Use the buttons below")
        embeds.append(embed)
    return embeds


class HelpPagerView(discord.ui.View):
    """Private, requester-only pagination for the English bot guide."""
    def __init__(self, owner_id, embeds):
        super().__init__(timeout=300)
        self.owner_id,self.embeds,self.page=owner_id,embeds,0
        self.message=None
        self._sync_buttons()

    def _sync_buttons(self):
        self.previous_button.disabled=self.page==0
        self.next_button.disabled=self.page>=len(self.embeds)-1

    async def interaction_check(self,interaction):
        if interaction.user.id==self.owner_id:return True
        await interaction.response.send_message("Only the member who opened this help guide can use these buttons.",ephemeral=True)
        return False

    @discord.ui.button(label="Previous",emoji="â—€ï¸",style=discord.ButtonStyle.secondary)
    async def previous_button(self,interaction,button):
        self.page=max(0,self.page-1); self._sync_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.page],view=self)

    @discord.ui.button(label="Close",emoji="âœ–ï¸",style=discord.ButtonStyle.danger)
    async def close_button(self,interaction,button):
        self.stop()
        await interaction.response.edit_message(content="Help guide closed.",embed=None,view=None)

    @discord.ui.button(label="Next",emoji="â–¶ï¸",style=discord.ButtonStyle.primary)
    async def next_button(self,interaction,button):
        self.page=min(len(self.embeds)-1,self.page+1); self._sync_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.page],view=self)

    async def on_timeout(self):
        for item in self.children:
            if isinstance(item,discord.ui.Button):item.disabled=True
        if self.message:
            try:await self.message.edit(view=self)
            except discord.HTTPException:pass

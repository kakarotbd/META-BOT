"""Move feature module."""

import discord

from config.app_config import APP_CONFIG


class _MovePickerView(discord.ui.View):
    def __init__(self,moderator,guild,requester_id):
        super().__init__(timeout=300)
        self.moderator,self.guild,self.requester_id=moderator,guild,requester_id

    async def interaction_check(self,interaction):
        if interaction.user.id==self.requester_id and self.moderator.drag_allowed(interaction.user):return True
        await interaction.response.send_message("Only the authorized member who opened this /move picker can use it.",ephemeral=True)
        return False

    def add_page_buttons(self,page,page_count,previous,next_page):
        if page_count<=1:return
        previous_button=discord.ui.Button(label="Previous",style=discord.ButtonStyle.secondary,disabled=page<=0,row=1)
        next_button=discord.ui.Button(label="Next",style=discord.ButtonStyle.secondary,disabled=page>=page_count-1,row=1)
        previous_button.callback=previous; next_button.callback=next_page
        self.add_item(previous_button); self.add_item(next_button)


class MoveActiveVCView(_MovePickerView):
    """First step: show only voice channels currently occupied by human members."""
    PAGE_SIZE=25
    def __init__(self,moderator,guild,requester_id,channels):
        super().__init__(moderator,guild,requester_id)
        self.channels=channels; self.page=0; self.build()

    def current_page(self):
        start=self.page*self.PAGE_SIZE
        return self.channels[start:start+self.PAGE_SIZE]

    def make_embed(self):
        page_count=max(1,(len(self.channels)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        embed=discord.Embed(title=f"{APP_CONFIG.bot_app_name} | Move Members: Active Voice Channels",description="Choose an occupied voice channel to select its connected human members. Discord does not expose a reliable per-member live-speaking flag here, so active means a non-bot member is connected.",colour=discord.Colour.blurple())
        embed.set_footer(text=f"{APP_CONFIG.bot_app_name} | Page {self.page+1} of {page_count} | Use the controls below")
        return embed

    def build(self):
        self.clear_items()
        options=[discord.SelectOption(label=channel.name[:100],value=str(channel.id),description=f"{len([m for m in channel.members if not m.bot])} human member(s) connected") for channel in self.current_page()]
        select=discord.ui.Select(placeholder="Choose an active source voice channel",min_values=1,max_values=1,options=options)
        select.callback=self.choose_channel; self.add_item(select)
        pages=max(1,(len(self.channels)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self.add_page_buttons(self.page,pages,self.previous_page,self.next_page)

    async def choose_channel(self,interaction):
        channel=self.guild.get_channel(int(interaction.data["values"][0]))
        if not isinstance(channel,discord.VoiceChannel):return await interaction.response.send_message("That voice channel is no longer available.",ephemeral=True)
        members=[member for member in channel.members if not member.bot]
        if not members:return await interaction.response.send_message("That voice channel is no longer active. Run /move again to refresh the list.",ephemeral=True)
        view=DragMemberView(self.moderator,self.guild,self.requester_id,channel,members)
        await interaction.response.edit_message(embed=view.make_embed(),view=view)

    async def previous_page(self,interaction):
        self.page=max(0,self.page-1); self.build(); await interaction.response.edit_message(embed=self.make_embed(),view=self)

    async def next_page(self,interaction):
        self.page=min((len(self.channels)-1)//self.PAGE_SIZE,self.page+1); self.build(); await interaction.response.edit_message(embed=self.make_embed(),view=self)


class DragMemberView(_MovePickerView):
    """Paginated member picker; selection remains stable while browsing pages."""
    PAGE_SIZE=25
    def __init__(self,moderator,guild,requester_id,source_channel,members):
        super().__init__(moderator,guild,requester_id)
        self.source_channel=source_channel; self.members=list(members); self.selected_ids=set(); self.page=0; self.build()

    def current_page(self):
        start=self.page*self.PAGE_SIZE
        return self.members[start:start+self.PAGE_SIZE]

    def make_embed(self):
        page_count=max(1,(len(self.members)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        embed=discord.Embed(title=f"{APP_CONFIG.bot_app_name} | Move Members: Select Members",description=f"Source: **#{self.source_channel.name}**\nSelect members on each page. Your choices stay selected when you browse. You can also select everyone in this source channel.",colour=discord.Colour.blurple())
        embed.set_footer(text=f"{APP_CONFIG.bot_app_name} | Page {self.page+1} of {page_count} | Use the controls below")
        return embed

    def build(self):
        self.clear_items()
        page_members=self.current_page(); page_ids={member.id for member in page_members}
        options=[discord.SelectOption(label=member.display_name[:100],value=str(member.id),description=f"@{member.name}"[:100],default=member.id in self.selected_ids) for member in page_members]
        select=discord.ui.Select(placeholder="Select members on this page",min_values=0,max_values=len(options),options=options)
        select.callback=self.update_selection; self.add_item(select)
        pages=max(1,(len(self.members)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self.add_page_buttons(self.page,pages,self.previous_page,self.next_page)
        all_button=discord.ui.Button(label="Select All in Source VC",style=discord.ButtonStyle.secondary,emoji="👥",row=2)
        clear_button=discord.ui.Button(label="Clear Selection",style=discord.ButtonStyle.secondary,row=2)
        continue_button=discord.ui.Button(label="Choose Destination",style=discord.ButtonStyle.primary,emoji="➡️",row=3)
        all_button.callback=self.select_all; clear_button.callback=self.clear_selection; continue_button.callback=self.choose_target
        self.add_item(all_button); self.add_item(clear_button); self.add_item(continue_button)

    async def update_selection(self,interaction):
        page_ids={member.id for member in self.current_page()}
        self.selected_ids.difference_update(page_ids)
        self.selected_ids.update(int(value) for value in self.member_select_values(interaction))
        self.build(); await interaction.response.edit_message(embed=self.make_embed(),view=self)

    @staticmethod
    def member_select_values(interaction):
        return interaction.data.get("values",[])

    async def select_all(self,interaction):
        self.selected_ids={member.id for member in self.members}
        self.build(); await interaction.response.edit_message(embed=self.make_embed(),view=self)

    async def clear_selection(self,interaction):
        self.selected_ids.clear(); self.build(); await interaction.response.edit_message(embed=self.make_embed(),view=self)

    async def previous_page(self,interaction):
        self.page=max(0,self.page-1); self.build(); await interaction.response.edit_message(embed=self.make_embed(),view=self)

    async def next_page(self,interaction):
        self.page=min((len(self.members)-1)//self.PAGE_SIZE,self.page+1); self.build(); await interaction.response.edit_message(embed=self.make_embed(),view=self)

    async def choose_target(self,interaction):
        if not self.selected_ids:return await interaction.response.send_message("Select at least one member first.",ephemeral=True)
        view=DragTargetView(self.moderator,self.guild,self.requester_id,self.source_channel.id,self.selected_ids)
        await interaction.response.edit_message(embed=view.make_embed(),view=view)


class DragTargetView(_MovePickerView):
    PAGE_SIZE=25
    def __init__(self,moderator,guild,requester_id,source_channel_id,member_ids):
        super().__init__(moderator,guild,requester_id)
        self.source_channel_id=source_channel_id; self.member_ids=set(member_ids); self.channels=list(guild.voice_channels); self.page=0; self.build()

    def current_page(self):
        start=self.page*self.PAGE_SIZE
        return self.channels[start:start+self.PAGE_SIZE]

    def make_embed(self):
        page_count=max(1,(len(self.channels)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        embed=discord.Embed(title=f"{APP_CONFIG.bot_app_name} | Move Members: Choose Destination",description=f"Move **{len(self.member_ids)}** selected member(s) from **#{self.guild.get_channel(self.source_channel_id).name}**. Choose any voice channel as the destination, including an empty channel.",colour=discord.Colour.blurple())
        embed.set_footer(text=f"{APP_CONFIG.bot_app_name} | Page {self.page+1} of {page_count} | Use the controls below")
        return embed

    def build(self):
        self.clear_items()
        options=[]
        for channel in self.current_page():
            humans=sum(not member.bot for member in channel.members)
            description=f"{humans} human member(s) currently connected" if humans else "Empty destination"
            options.append(discord.SelectOption(label=channel.name[:100],value=str(channel.id),description=description[:100]))
        select=discord.ui.Select(placeholder="Choose destination voice channel",min_values=1,max_values=1,options=options)
        select.callback=self.move_members; self.add_item(select)
        pages=max(1,(len(self.channels)+self.PAGE_SIZE-1)//self.PAGE_SIZE)
        self.add_page_buttons(self.page,pages,self.previous_page,self.next_page)

    async def previous_page(self,interaction):
        self.page=max(0,self.page-1); self.build(); await interaction.response.edit_message(embed=self.make_embed(),view=self)

    async def next_page(self,interaction):
        self.page=min((len(self.channels)-1)//self.PAGE_SIZE,self.page+1); self.build(); await interaction.response.edit_message(embed=self.make_embed(),view=self)

    async def move_members(self,interaction):
        target=self.guild.get_channel(int(interaction.data["values"][0]))
        if not isinstance(target,discord.VoiceChannel):return await interaction.response.send_message("That destination voice channel is no longer available.",ephemeral=True)
        source=self.guild.get_channel(self.source_channel_id)
        if not isinstance(source,discord.VoiceChannel):return await interaction.response.send_message("The source voice channel is no longer available.",ephemeral=True)
        await interaction.response.defer(ephemeral=True,thinking=True); moved,skipped=0,0
        for member_id in self.member_ids:
            member=self.guild.get_member(member_id)
            if not member or not member.voice or member.voice.channel!=source or source==target:
                skipped+=1; continue
            try:await member.move_to(target,reason=f"/move requested by {interaction.user}"); moved+=1
            except (discord.Forbidden,discord.HTTPException):skipped+=1
        self.moderator.log(f"/move by {interaction.user} in {self.guild.name}: moved {moved} member(s) from {source.name} to {target.name}; skipped {skipped}.")
        await interaction.followup.send(f"Moved **{moved}** member(s) to **#{target.name}**. Skipped: **{skipped}**.",ephemeral=True)

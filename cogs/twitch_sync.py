import asyncio

from datetime import datetime, timedelta
from discord.ext import commands
from functions import functions_twitch

class twitch_sync(commands.Cog, name="twitch_sync"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print("Twitch sync ready...")

        self.task = self.bot.loop.create_task(self.twitch_sync())

    async def twitch_sync(self):
        last_checked_date = None

        print("Twitch sync loop started")
        while True:
            timestamp = (datetime.utcnow() + timedelta(hours=2))

            if timestamp.minute % 30 == 0:
                print("Twitch check - messages.")
                await functions_twitch.assign_roles_messages(self)
                print("Twitch check - watchtime.")
                await functions_twitch.assign_roles_watchtime(self)
                print("Twitch check - VIP and MODs check.")
                await functions_twitch.assign_roles_vip_mod(self)
                print("Twitch synchronization completed.")

            # wait some time before another loop. Don't make it more than 60 sec or it will skip
            await asyncio.sleep(35)

    @commands.command(name="sync_twitch")
    async def sync_twitch(self, ctx):
        print("Twitch check - messages.")
        await functions_twitch.assign_roles_messages(self)
        print("Twitch check - watchtime.")
        await functions_twitch.assign_roles_watchtime(self)
        print("Twitch check - VIP and MODs check.")
        await functions_twitch.assign_roles_vip_mod(self)
        await ctx.send("Role z twitcha zsynchronizowane.")

def setup(bot):
    bot.add_cog(twitch_sync(bot))
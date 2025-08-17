# kick_sync.py
import asyncio

from datetime import datetime, timedelta
from discord.ext import commands
from functions import functions_kick


class kick_sync(commands.Cog, name="kick_sync"):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print("Kick sync ready...")
        self.task = self.bot.loop.create_task(self.kick_sync())

    async def kick_sync(self):
        print("Kick sync loop started")
        while True:
            timestamp = (datetime.utcnow() + timedelta(hours=2))

            if timestamp.minute % 30 == 0:
                print("Kick check - messages.")
                await functions_kick.assign_roles_messages(self)
                print("Kick check - VIP and MODs check.")
                await functions_kick.assign_roles_vip_mod(self)
                print("Kick synchronization completed.")

            # Poczekaj chwilę przed kolejną iteracją (nie więcej niż 60s, żeby nie przeskoczyć minuty)
            await asyncio.sleep(35)

    @commands.command(name="sync_kick")
    async def sync_kick(self, ctx):
        print("Kick check - messages.")
        await functions_kick.assign_roles_messages(self)
        print("Kick check - VIP and MODs check.")
        await functions_kick.assign_roles_vip_mod(self)
        await ctx.send("Role z Kicka zsynchronizowane.")

def setup(bot):
    bot.add_cog(kick_sync(bot))

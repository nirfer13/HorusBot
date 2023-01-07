from discord.ext import commands
import discord
import asyncio
from datetime import datetime

# general bag for functions

class functions_general(commands.Cog, name="functions_general"):
    def __init__(self, bot):
        self.bot = bot


def setup(bot):
    bot.add_cog(functions_general(bot))

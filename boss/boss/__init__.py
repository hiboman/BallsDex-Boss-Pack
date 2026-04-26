from discord.ext import commands


async def setup(bot: commands.Bot):
    from .cog import BossCog

    cog = BossCog(bot)
    await bot.add_cog(cog)


async def teardown(bot: commands.Bot):
    await bot.remove_cog("Boss")

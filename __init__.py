from .kickalerts import KickAlerts


async def setup(bot):
    cog = KickAlerts(bot)
    await bot.add_cog(cog)

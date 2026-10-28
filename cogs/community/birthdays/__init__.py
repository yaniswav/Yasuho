"""Purpose: package entry point for birthdays (a package whose __init__ defines
``setup`` is loaded whole by core's extension discovery).

Layout: store.py (config, date maths, every query), panel.py (/birthdays
manager), cog.py (commands, the daily timer, the member-remove cleanup).
"""

from .cog import Birthdays


async def setup(bot):
    await bot.add_cog(Birthdays(bot))

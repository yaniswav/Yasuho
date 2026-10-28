"""Purpose: package entry point for the suggestion box (a package whose __init__
defines ``setup`` is loaded whole by core's extension discovery).

Layout: store.py (config + every query), render.py (status labels, the embed),
views.py (persistent buttons, status picker, modals), panel.py (/suggestions
manager), cog.py (commands, votes, statuses, digest).
"""

from .cog import Suggestions


async def setup(bot):
    await bot.add_cog(Suggestions(bot))

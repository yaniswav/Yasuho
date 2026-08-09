"""Purpose: package entry point - exposes the leveling cogs to core's extension
discovery (a package whose __init__ defines ``setup`` is loaded whole).

THE leveling feature: per-message XP with a fixed sqrt curve, level-up role
rewards, an admin ``/xp`` group, the ``/levelconfig`` panel, opt-in voice XP,
and monthly/weekly seasons with a hall-of-fame podium. Six cogs, one extension
(the same fold as ``cogs/community/profile/``): a hybrid group's subcommands
must live in the same cog as their parent, so ``LevelConfigUI`` stays its own
cog even though most of what it configures belongs to the other five.

Layout:
* engine.py         - the pure XP curve, ``LevelConfig``, the no-xp and
                       multiplier snapshots, the leaderboard pager and the
                       season period-key maths (no discord, no database, no
                       awaits) - re-homed from ``tools/leveling.py``;
* gate.py           - the pure command-prefix filter that keeps the on_message
                       hot path from granting XP to a command invocation -
                       re-homed from ``tools/leveling_gate.py``;
* admin_rules.py    - the pure value maths behind ``/xp`` (bounds checks, the
                       floored give/take/set arithmetic, the resetall
                       name-match gate) - re-homed from ``tools/level_admin.py``;
* reward_rules.py   - the pure decision engine for level-up role rewards
                       (stack vs replace, which roles to add/remove) -
                       re-homed from ``tools/level_rewards.py``;
* rank_card.py      - rank-card validation, normalisation and the
                       one-statement ``rank_cards`` / ``user_rank_cards``
                       storage queries - re-homed from ``tools/rank_card.py``
                       (no collision, kept its name);
* rank_card_user.py - ``RankCardUserMixin``: the per-MEMBER card layer (U1) -
                       the ``/rankcard`` surface, the marker cache and the
                       precedence resolver /rank draws with. A MIXIN of the
                       ``Leveling`` cog, not a seventh cog (cogs/anilist/'s
                       shape): it customises what ``/rank`` draws and needs that
                       cog's guild-side card accessor;
* leveling.py        - the ``Leveling`` cog: the on_message grant path, the
                       leaderboard/rank commands, the rank-card render seam;
* level_admin.py     - the ``LevelAdmin`` cog: the ``/levelconfig xp`` group
                       body (give/take/set/reset/resetall);
* level_config_ui.py - the ``LevelConfigUI`` cog: the ``/levelconfig`` panel
                       and every admin-facing knob (no-xp zones, multipliers,
                       rank-card customisation, cross-cog delegation to the
                       other five cogs' subcommands);
* level_rewards.py   - the ``LevelRewards`` cog: applies reward_rules'
                       decisions against real roles, the admin rewards group;
* voice_xp.py        - the ``VoiceXP`` cog: the in-memory session map, its own
                       voice-state listener, and the periodic batched sweep;
* seasons.py         - the ``Seasons`` cog: the monthly/weekly rollover, the
                       hall-of-fame podium queries, the champion role;
* seasons_views.py   - the two Components V2 views seasons.py hands its
                       podium/panel data to (no cog of its own - imported by
                       seasons.py, one-way, mirrors music.py -> views.py).

Cross-cog coupling: dashboard_sync's invalidators reach this feature by cog
NAME (``bot.get_cog("Leveling")`` / ``"LevelRewards"`` / ``"Seasons"``), never
by module path, so none of this move touches them. The same is true of every
other ``bot.get_cog(...)`` lookup between these six cogs - all lazy, all at
call time, so add_cog order below carries no dependency.

Typography rule: ASCII '-' and '...' only.
"""

from .level_admin import LevelAdmin
from .level_config_ui import LevelConfigUI
from .level_rewards import LevelRewards
from .leveling import Leveling
from .seasons import Seasons
from .voice_xp import VoiceXP

__all__ = (
    "Leveling",
    "LevelAdmin",
    "LevelConfigUI",
    "LevelRewards",
    "Seasons",
    "VoiceXP",
    "setup",
)


async def setup(bot):
    await bot.add_cog(Leveling(bot))
    await bot.add_cog(LevelAdmin(bot))
    await bot.add_cog(LevelConfigUI(bot))
    await bot.add_cog(LevelRewards(bot))
    await bot.add_cog(Seasons(bot))
    await bot.add_cog(VoiceXP(bot))

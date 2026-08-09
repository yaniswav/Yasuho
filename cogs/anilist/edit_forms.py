"""The AniList WRITE path: the update wizard, its pickers, and the quota gate.

Everything a member touches to CHANGE a list entry lives here, as opposed to the
read-only browsing surface in :mod:`cogs.anilist.media_view`:

* the update wizard, in the order it is walked - :class:`TypeView` (anime vs
  manga) -> :class:`SeasonSelectView` (the exact title) -> :class:`EditEntryModal`
  (status dropdown + progress/score fields, pre-filled from the entry);
* the two disambiguation pickers a text-driven edit can land in -
  :class:`EditSelectView` (which media does this edit target?) and
  :class:`OnListSelectView` (pick among the titles you already track);
* :func:`_deny_if_throttled`, the ONE interactive-quota gate every AniList write
  and every interactive read passes through. It lives here because the wizard is
  its heaviest consumer, and ``media_view`` / ``schedule`` import it from here
  rather than restating the refusal.

Bottom of the package's import graph: it depends only on ``helpers``, ``queries``
and ``tools``, never on another AniList surface. The mutation itself is NOT here
- every form hands its values to the cog's ``_apply_edit``, which owns the single
``SaveMediaListEntry`` seam.
"""

import logging

import discord

from .helpers import (
    DEFAULT_SCORE_FORMAT,
    _format_score,
    _media_title,
    _media_unit,
    _progress_max,
    _status_label,
    parse_score,
    render_score,
    score_hint,
)
from .queries import SAVE_ENTRY_QUERY
from tools.i18n import _
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)


async def _deny_if_throttled(cog, interaction):
    """Refuse an interactive AniList action when the per-user/guild quota is spent.

    Guards the button and select callbacks that issue an AniList GraphQL request,
    so one member (or one hyped guild) is throttled BEFORE the expensive fetch and
    the airing / feed / chapter pollers keep their share of the shared per-IP
    budget. Sends a terse ephemeral 'slow down' and returns ``True`` when the
    caller should stop; returns ``False`` (having consumed a slot) when it may
    proceed. Best-effort: a missing throttle (older wiring) never blocks a click.
    """

    throttle = getattr(cog, "_throttle", None)
    if throttle is None:
        return False
    if throttle.allow_interactive(interaction.user.id, interaction.guild_id):
        return False
    try:
        await interaction.response.send_message(
            _(
                "Slow down a little - too many AniList requests right now. "
                "Give it a few seconds and try again."
            ),
            ephemeral=True,
        )
    except Exception:
        pass
    return True


# ----------------------------------------------------------------------
# Interactive components (discord.ui)
# ----------------------------------------------------------------------
class EditSelect(discord.ui.Select):
    """Disambiguation dropdown: choose which media a text edit targets."""

    def __init__(self, cog, candidates, author_id, field, value):
        self.cog = cog
        self.author_id = author_id
        self.field = field
        self.value = value
        self.candidates = {str(m.get("id")): m for m in candidates}

        options = []
        for media in candidates[:25]:
            mtype = media.get("type") or "?"
            romaji = (media.get("title") or {}).get("romaji") or "Unknown"
            year = media.get("seasonYear") or "?"
            label = f"[{mtype}] {romaji} ({year})"
            options.append(
                discord.SelectOption(label=label[:100], value=str(media.get("id")))
            )

        super().__init__(placeholder=_("Pick the right title..."), options=options)

    async def callback(self, interaction):
        try:
            if await _deny_if_throttled(self.cog, interaction):
                return
            media = self.candidates.get(self.values[0])
            if not media:
                return await interaction.response.send_message(
                    _("Could not load that title."), ephemeral=True
                )

            for child in self.view.children:
                child.disabled = True
            await interaction.response.edit_message(
                content=_("Updating **{title}**...").format(
                    title=_media_title(media)
                ),
                view=self.view,
            )
            await self.cog._apply_edit(
                interaction, self.author_id, media, self.field, self.value
            )
        except Exception:
            log.exception("AniList edit select failed")
            try:
                await interaction.followup.send(
                    _("Something went wrong updating that entry."), ephemeral=True
                )
            except Exception:
                pass


class EditSelectView(AuthorView):
    """Author-restricted wrapper around an :class:`EditSelect`."""

    def __init__(self, cog, candidates, author_id, field, value, timeout=120):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.add_item(EditSelect(cog, candidates, author_id, field, value))


class EditEntryModal(LocaleModal):
    """Edit status (a dropdown) + progress/score (fields), pre-filled from the entry."""

    def __init__(self, cog, media, token=None, entry=None, score_format=None):
        super().__init__(
            title=_("Edit: {title}").format(title=_media_title(media))[:45]
        )
        self.cog = cog
        self.media = media
        self.token = token
        self.score_format = score_format or DEFAULT_SCORE_FORMAT
        entry = entry or {}

        unit = _media_unit(media)
        watching = _("Reading") if unit == "chapter" else _("Watching")
        current_status = entry.get("status")
        choices = [
            ("CURRENT", watching),
            ("PLANNING", _("Planning")),
            ("COMPLETED", _("Completed")),
            ("REPEATING", _("Repeating")),
            ("PAUSED", _("Paused")),
            ("DROPPED", _("Dropped")),
        ]
        # Status is a real dropdown (Components V2 select-in-modal), the current
        # value pre-selected; min_values=0 so it can be left unchanged.
        self.status_select = discord.ui.Select(
            placeholder=_("Keep current status"),
            min_values=0,
            max_values=1,
            required=False,
            options=[
                discord.SelectOption(
                    label=label, value=value, default=(value == current_status)
                )
                for value, label in choices
            ],
        )
        self.add_item(
            discord.ui.Label(text=_("Status"), component=self.status_select)
        )

        current_progress = entry.get("progress")
        self.progress_input = discord.ui.TextInput(
            required=False,
            style=discord.TextStyle.short,
            max_length=6,
            default=str(current_progress) if current_progress is not None else None,
        )
        self.add_item(
            discord.ui.Label(
                text=_("Progress ({unit}s)").format(unit=unit),
                component=self.progress_input,
            )
        )

        # Pre-fill with the bare in-format number (editable, round-trips through
        # parse_score); the placeholder shows the valid range for their format.
        score = _format_score(entry.get("score"))
        self.score_input = discord.ui.TextInput(
            required=False,
            max_length=6,
            placeholder=score_hint(self.score_format),
            default=score if score and score != "0" else None,
        )
        self.add_item(discord.ui.Label(text=_("Score"), component=self.score_input))

    async def on_submit(self, interaction):
        variables = {"mediaId": self.media.get("id")}
        status_values = self.status_select.values
        progress_raw = (self.progress_input.value or "").strip()
        score_raw = (self.score_input.value or "").strip()

        if progress_raw:
            try:
                variables["progress"] = int(progress_raw)
            except ValueError:
                return await interaction.response.send_message(
                    _("Progress must be a whole number."), ephemeral=True
                )
        if score_raw:
            parsed = parse_score(score_raw, self.score_format)
            if parsed is None:
                return await interaction.response.send_message(
                    _("Score must be a number in the {hint} range.").format(
                        hint=score_hint(self.score_format)
                    ),
                    ephemeral=True,
                )
            variables["score"] = parsed

        if status_values:
            variables["status"] = status_values[0]

        if (
            "progress" not in variables
            and "score" not in variables
            and "status" not in variables
        ):
            return await interaction.response.send_message(
                _("Nothing to update - pick a status or fill in progress/score."),
                ephemeral=True,
            )

        # The wizard opens this modal without a token; resolve it lazily here
        # (never logged). Direct callers may still pass one in.
        token = self.token
        if token is None:
            token = await self.cog._get_token(interaction.user.id)
        if not token:
            return await interaction.response.send_message(
                _("Link your account first with `/anilist login`."),
                ephemeral=True,
            )

        try:
            data = await self.cog._graphql(
                SAVE_ENTRY_QUERY, variables, token=token
            )
            entry = ((data or {}).get("data") or {}).get("SaveMediaListEntry")
            if not entry:
                return await interaction.response.send_message(
                    _("Could not update that entry."), ephemeral=True
                )

            name = (
                (entry.get("media") or {}).get("title") or {}
            ).get("romaji") or _media_title(self.media)
            unit = _media_unit(self.media)
            score = render_score(
                entry.get("score"), self.score_format
            ) or _format_score(entry.get("score"))
            await interaction.response.send_message(
                _("Updated **{name}** - {unit} {progress}, score {score}.").format(
                    name=name,
                    unit=unit,
                    progress=entry.get("progress"),
                    score=score,
                ),
                ephemeral=True,
            )
        except Exception:
            log.exception("AniList edit modal failed")
            try:
                await interaction.response.send_message(
                    _("Something went wrong updating that entry."), ephemeral=True
                )
            except Exception:
                pass


class TypeView(AuthorView):
    """Update wizard, step 1: pick anime vs manga among mixed candidates.

    Only the buttons for types actually present in ``candidates`` are shown.
    """

    def __init__(self, cog, candidates, author_id, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.cog = cog
        self.candidates = candidates

        types_present = {m.get("type") for m in candidates if m.get("type")}
        if "ANIME" not in types_present:
            self.remove_item(self.anime_button)
        if "MANGA" not in types_present:
            self.remove_item(self.manga_button)

    async def _choose_type(self, interaction, media_type):
        try:
            subset = [
                m for m in self.candidates if m.get("type") == media_type
            ]
            if not subset:
                return await interaction.response.send_message(
                    _("No matching titles of that type."), ephemeral=True
                )

            view = SeasonSelectView(self.cog, subset, self.author_id, media_type)
            await interaction.response.edit_message(
                content=_("Pick the exact title to update:"), view=view
            )
            view.message = interaction.message
            self.stop()
        except Exception:
            log.exception("AniList update type selection failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        _("Something went wrong."), ephemeral=True
                    )
                else:
                    await interaction.response.send_message(
                        _("Something went wrong."), ephemeral=True
                    )
            except Exception:
                pass

    @discord.ui.button(label="📺 Anime", style=discord.ButtonStyle.primary)
    async def anime_button(self, interaction, button):
        await self._choose_type(interaction, "ANIME")

    @discord.ui.button(label="📖 Manga", style=discord.ButtonStyle.success)
    async def manga_button(self, interaction, button):
        await self._choose_type(interaction, "MANGA")


class SeasonSelect(discord.ui.Select):
    """Update wizard, step 2: pick the exact title; opens a pre-filled modal."""

    def __init__(self, cog, candidates, media_type):
        self.cog = cog
        self.media_type = media_type
        self.candidates = {str(m.get("id")): m for m in candidates}

        options = []
        for media in candidates[:25]:
            romaji = (media.get("title") or {}).get("romaji") or "Unknown"
            year = media.get("seasonYear") or "?"
            label = f"{romaji} ({year})"
            options.append(
                discord.SelectOption(label=label[:100], value=str(media.get("id")))
            )

        super().__init__(placeholder=_("Pick the exact title..."), options=options)

    async def callback(self, interaction):
        try:
            if await _deny_if_throttled(self.cog, interaction):
                return
            media = self.candidates.get(self.values[0])
            if not media:
                return await interaction.response.send_message(
                    _("Could not load that title."), ephemeral=True
                )

            # Fetch the viewer's current entry BEFORE send_modal (allowed) so
            # the form opens pre-filled with their existing status/score/progress.
            entry, _logged_in = await self.cog._viewer_entry(
                interaction.user.id, media.get("id")
            )
            score_format = await self.cog._get_score_format(interaction.user.id)
            await interaction.response.send_modal(
                EditEntryModal(self.cog, media, entry=entry, score_format=score_format)
            )
        except Exception:
            log.exception("AniList update season select failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        _("Something went wrong opening the editor."),
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        _("Something went wrong opening the editor."),
                        ephemeral=True,
                    )
            except Exception:
                pass


class SeasonSelectView(AuthorView):
    """Author-restricted wrapper around a :class:`SeasonSelect`."""

    def __init__(self, cog, candidates, author_id, media_type, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.add_item(SeasonSelect(cog, candidates, media_type))


class OnListSelect(discord.ui.Select):
    """Update wizard: pick among the titles the user *already tracks*.

    Each option is labelled with its type/title and described with the user's
    current status and progress, so the choice is unambiguous.
    """

    def __init__(self, cog, candidates):
        self.cog = cog
        self.candidates = {str(m.get("id")): m for m in candidates}

        options = []
        for media in candidates[:25]:
            mtype = media.get("type") or "?"
            romaji = (media.get("title") or {}).get("romaji") or "Unknown"
            year = media.get("seasonYear") or "?"
            label = f"[{mtype}] {romaji} ({year})"

            entry = media.get("mediaListEntry") or {}
            parts = []
            status = entry.get("status")
            if status:
                parts.append(_status_label(status, media))
            progress = entry.get("progress")
            if progress is not None:
                total = _progress_max(media)
                unit = _media_unit(media, plural=True)
                parts.append(f"{progress}/{total if total else '?'} {unit}")
            description = ", ".join(parts) if parts else None

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    description=description[:100] if description else None,
                    value=str(media.get("id")),
                )
            )

        super().__init__(
            placeholder=_("Pick which one to update..."), options=options
        )

    async def callback(self, interaction):
        try:
            if await _deny_if_throttled(self.cog, interaction):
                return
            media = self.candidates.get(self.values[0])
            if not media:
                return await interaction.response.send_message(
                    _("Could not load that title."), ephemeral=True
                )

            # Fetch the canonical entry BEFORE send_modal (allowed) to pre-fill.
            entry, _logged_in = await self.cog._viewer_entry(
                interaction.user.id, media.get("id")
            )
            score_format = await self.cog._get_score_format(interaction.user.id)
            await interaction.response.send_modal(
                EditEntryModal(self.cog, media, entry=entry, score_format=score_format)
            )
        except Exception:
            log.exception("AniList update on-list select failed")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        _("Something went wrong opening the editor."),
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        _("Something went wrong opening the editor."),
                        ephemeral=True,
                    )
            except Exception:
                pass


class OnListSelectView(AuthorView):
    """Author-restricted wrapper around an :class:`OnListSelect`."""

    def __init__(self, cog, candidates, author_id, timeout=180):
        super().__init__(
            author_id, timeout=timeout, deny_message="This menu isn't for you."
        )
        self.add_item(OnListSelect(cog, candidates))

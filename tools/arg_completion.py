"""Interactive completion for commands used without their required arguments.

When a prefix command is invoked with a missing required argument, instead of
only printing a usage line we send a small guided form:

- member / role / channel arguments become select menus,
- a yes/no argument becomes a two-choice menu,
- every other argument (free text, numbers, ids, durations, a user to ban by
  id, ...) is gathered through a modal.

Once everything is filled the original command is rebuilt as a normal command
line and re-invoked, so every existing converter and permission check runs
exactly as it would have. The flow is best-effort: :func:`start` returns False
whenever it cannot help (hidden owner commands, unknown parameter shapes, a DM
with no guild for the entity menus, ...) and the caller falls back to the
classic usage message. It can never make a command less usable than before.
"""

from __future__ import annotations

import logging
import typing

import discord

from tools.formats import random_colour
from tools.i18n import _
from tools.views import AuthorView, LocaleModal

log = logging.getLogger(__name__)

# Parameter kinds rendered as select menus; everything else becomes a text box.
_SELECT_KINDS = frozenset({"member", "role", "channel", "bool"})
# Select kinds that need a guild to populate, so they are skipped in DMs.
_GUILD_SELECT_KINDS = frozenset({"member", "role", "channel"})

# Message ids whose completion is already running, so a rebuilt command that
# somehow still misses an argument cannot loop back into a brand new form.
_inflight: typing.Set[int] = set()


def _remember(message_id: int) -> None:
    """Track a message id, clearing the set if it grows unreasonably large."""
    if len(_inflight) > 500:
        _inflight.clear()
    _inflight.add(message_id)


class _Field:
    """One command parameter we may need to collect interactively."""

    __slots__ = (
        "name", "kind", "required", "consume_rest", "channel_types",
        "optional_annotation",
    )

    def __init__(
        self, name, kind, required, consume_rest, channel_types,
        optional_annotation=False,
    ):
        self.name = name
        self.kind = kind
        self.required = required
        self.consume_rest = consume_rest
        self.channel_types = channel_types
        self.optional_annotation = optional_annotation


# --- parameter inspection -------------------------------------------------


def _unwrap_optional(annotation):
    """Return X for Optional[X]/Union[X, None]; otherwise the annotation as-is."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _is_optional_annotation(annotation) -> bool:
    """True for a typing.Optional[X] / Union[X, None] annotation.

    discord.py backtracks (view.undo) on a failed conversion for these, so an
    Optional positional argument is safe to omit from a rebuilt command line; a
    bare ``= None`` default is not.
    """
    return (
        typing.get_origin(annotation) is typing.Union
        and type(None) in typing.get_args(annotation)
    )


def _categorize(param) -> str:
    """Map a command parameter to one of our field kinds."""
    ann = _unwrap_optional(param.annotation)

    # Cogs do not use 'from __future__ import annotations', but stay safe if a
    # string annotation ever shows up by matching on the trailing type name.
    if isinstance(ann, str):
        head = ann.split("[", 1)[0].strip()
        tail = head.rsplit(".", 1)[-1]
        if tail == "Member":
            return "member"
        if tail == "Role":
            return "role"
        if tail in (
            "TextChannel", "VoiceChannel", "CategoryChannel", "StageChannel",
            "ForumChannel", "GuildChannel", "Thread",
        ):
            return "channel"
        if tail == "bool":
            return "bool"
        return "text"

    if ann is discord.Member:
        return "member"
    if ann is discord.Role:
        return "role"
    try:
        if isinstance(ann, type) and issubclass(ann, (discord.abc.GuildChannel, discord.Thread)):
            return "channel"
    except TypeError:
        pass
    if ann is bool:
        return "bool"
    # discord.User, str, int, float, custom converters (durations, Range,
    # clean_content, Message, ...) all round-trip through a text box.
    return "text"


def _channel_types(param):
    """Channel types to filter a ChannelSelect, or None for "any channel"."""
    ann = _unwrap_optional(param.annotation)
    mapping = {
        discord.TextChannel: [discord.ChannelType.text, discord.ChannelType.news],
        discord.VoiceChannel: [discord.ChannelType.voice],
        discord.StageChannel: [discord.ChannelType.stage_voice],
        discord.CategoryChannel: [discord.ChannelType.category],
        discord.ForumChannel: [discord.ChannelType.forum],
        discord.Thread: [
            discord.ChannelType.public_thread,
            discord.ChannelType.private_thread,
        ],
    }
    if isinstance(ann, type):
        return mapping.get(ann)
    return None


def _build_fields(command):
    """Build the ordered list of fields for a command's parameters."""
    fields = []
    for name, param in command.clean_params.items():
        kind = _categorize(param)
        ctypes = _channel_types(param) if kind == "channel" else None
        fields.append(
            _Field(
                name=name,
                kind=kind,
                required=param.required,
                consume_rest=param.kind == param.KEYWORD_ONLY,
                channel_types=ctypes,
                optional_annotation=_is_optional_annotation(param.annotation),
            )
        )
    # A positional field with a bare (non-Optional) default that is followed by
    # another field cannot be safely skipped: discord.py will not backtrack on a
    # failed conversion, so its slot would swallow a later token and corrupt the
    # rebuilt line. Force it to be filled in the form. True typing.Optional
    # positionals DO backtrack, so they stay skippable.
    for index, field in enumerate(fields):
        if (
            not field.required
            and not field.consume_rest
            and not field.optional_annotation
            and index < len(fields) - 1
        ):
            field.required = True
    return fields


def _clip(text: str, limit: int) -> str:
    """Plain ASCII clip to a length limit (used for labels and previews)."""
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


# --- the interactive view -------------------------------------------------


class _CompletionView(AuthorView):
    """Guided form that collects a command's arguments then re-invokes it."""

    def __init__(self, ctx, fields, select_fields, text_fields, timeout=180.0):
        super().__init__(
            ctx.author.id, timeout=timeout, deny_message="This prompt isn't for you."
        )
        self.ctx = ctx
        self.command = ctx.command
        self.prefix = ctx.prefix
        self.fields = fields
        self.select_fields = select_fields
        self.text_fields = text_fields
        self.collected = {}
        self.provided = set()
        self.finished = False

        button_row = len(select_fields)
        for index, field in enumerate(select_fields):
            self.add_item(_make_select(self, field, index))
        if not select_fields and text_fields:
            self.add_item(_ActionButton(self, _("Fill in"), discord.ButtonStyle.primary, "modal", button_row))
        else:
            if text_fields:
                self.add_item(_ActionButton(self, _("Enter details"), discord.ButtonStyle.secondary, "modal", button_row))
            self.add_item(_ActionButton(self, _("Run"), discord.ButtonStyle.success, "run", button_row))
        self.add_item(_ActionButton(self, _("Cancel"), discord.ButtonStyle.danger, "cancel", button_row))

    # -- value handling ---------------------------------------------------

    def set_value(self, field, value):
        self.collected[field.name] = value
        self.provided.add(field.name)

    def _render_token(self, field, value) -> str:
        if field.kind in ("member", "role", "channel"):
            return str(value.id)
        if field.kind == "bool":
            return "true" if value else "false"
        # A prefilled discord object (e.g. a User parsed before the missing arg)
        # in a text field re-converts reliably by id, not by its str() name.
        if not isinstance(value, str) and hasattr(value, "id"):
            text = str(value.id)
        else:
            text = str(value)
        if field.consume_rest:
            return text
        text = text.replace("\n", " ").strip()
        if not text:
            return ""
        if any(ch.isspace() for ch in text) or '"' in text:
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return text

    def _reconstruct(self) -> str:
        parts = [f"{self.prefix}{self.command.qualified_name}"]
        for field in self.fields:
            if field.name not in self.provided:
                continue
            token = self._render_token(field, self.collected[field.name])
            if token == "" and not field.required:
                continue
            parts.append(token)
        return " ".join(parts)

    # -- embed ------------------------------------------------------------

    def _instructions(self) -> str:
        if self.select_fields and self.text_fields:
            return _("Pick from the menus, use **Enter details** for the rest, then press **Run**.")
        if self.select_fields:
            return _("Pick from the menus, then press **Run**.")
        return _("Press **Fill in** to type the details.")

    def _preview(self, field) -> str:
        value = self.collected.get(field.name)
        if field.kind in ("member", "role", "channel"):
            return getattr(value, "mention", str(value))
        if field.kind == "bool":
            return _("Yes") if value else _("No")
        return _clip(str(value), 60)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=_("Let's finish that command"),
            description=_(
                "`{command}` needs a little more info.\n{instructions}"
            ).format(
                command=f"{self.prefix}{self.command.qualified_name}",
                instructions=self._instructions(),
            ),
            colour=random_colour(),
        )
        for field in self.fields:
            if field.name in self.provided:
                value = _("Set: {preview}").format(preview=self._preview(field))
            elif field.required:
                value = _("Required")
            else:
                value = _("Optional - you can skip this")
            embed.add_field(name=field.name, value=value, inline=False)
        embed.set_footer(text=_("Only you can use this - it times out in 3 minutes."))
        return embed

    # -- interaction plumbing --------------------------------------------

    async def interaction_check(self, interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if self.finished:
            await interaction.response.send_message(
                _("This prompt is no longer active. Please run the command again."),
                ephemeral=True,
            )
            return False
        return True

    async def _refresh(self, interaction):
        """Re-render the form in place, tolerating either interaction type."""
        try:
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            return
        except discord.HTTPException:
            pass
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except discord.HTTPException:
            pass
        if self.message is not None:
            try:
                await self.message.edit(embed=self.build_embed(), view=self)
            except discord.HTTPException:
                log.debug("arg completion: refresh edit failed", exc_info=True)

    async def _report(self, interaction):
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    _("Something went wrong with that prompt."), ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    _("Something went wrong with that prompt."), ephemeral=True
                )
        except discord.HTTPException:
            log.debug("arg completion: report failed", exc_info=True)

    async def on_select(self, interaction, field, value):
        try:
            self.set_value(field, value)
            await self._refresh(interaction)
        except Exception:
            log.exception("arg completion: select handling failed")
            await self._report(interaction)

    async def maybe_finish(self, interaction):
        # A modal can be submitted after the view timed out; bail out cleanly.
        if self.finished:
            try:
                await interaction.response.send_message(
                    _("This prompt is no longer active. Please run the command again."),
                    ephemeral=True,
                )
            except discord.HTTPException:
                log.debug("arg completion: stale finish notice failed", exc_info=True)
            return
        missing = [f for f in self.fields if f.required and f.name not in self.provided]
        if missing:
            await self._refresh(interaction)
        else:
            await self.finish(interaction)

    async def finish(self, interaction):
        # Idempotency guard: two Run clicks (or Run racing a modal submit) can be
        # dispatched concurrently. There is no await before this set, so the
        # check-and-set is atomic on the event loop and the loser returns here
        # before re-invoking, preventing a destructive command from running twice.
        if self.finished:
            return
        self.finished = True
        for child in self.children:
            child.disabled = True
        content = self._reconstruct()
        running = _("Running `{command}`...").format(
            command=f"{self.prefix}{self.command.qualified_name}"
        )
        try:
            await interaction.response.edit_message(content=running, embed=None, view=None)
        except discord.HTTPException:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except discord.HTTPException:
                pass
            if self.message is not None:
                try:
                    await self.message.edit(content=running, embed=None, view=None)
                except discord.HTTPException:
                    pass
        self.stop()
        await self._reinvoke(content)

    async def _reinvoke(self, content):
        _remember(self.ctx.message.id)
        message = self.ctx.message
        original = message.content
        try:
            message.content = content
            new_ctx = await self.ctx.bot.get_context(message)
            # The failed (missing-argument) attempt already charged any cooldown,
            # so clear it before the rebuilt run to avoid a false CommandOnCooldown.
            if new_ctx.command is not None:
                new_ctx.command.reset_cooldown(new_ctx)
            await self.ctx.bot.invoke(new_ctx)
        except Exception:
            log.exception("arg completion: re-invoke failed for %r", content)
        finally:
            # Leave the shared cached Message object exactly as we found it.
            message.content = original

    async def cancel(self, interaction):
        self.finished = True
        self.stop()
        try:
            await interaction.response.edit_message(
                content=_("Cancelled."), embed=None, view=None
            )
        except discord.HTTPException:
            log.debug("arg completion: cancel edit failed", exc_info=True)

    async def on_timeout(self):
        if self.finished:
            return
        # Record the timed-out state so a late modal submit short-circuits.
        self.finished = True
        if self.message is None:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(content=_("This prompt timed out."), view=self)
        except discord.HTTPException:
            log.debug("arg completion: timeout edit failed", exc_info=True)


# --- selects --------------------------------------------------------------


class _MemberSelect(discord.ui.UserSelect):
    def __init__(self, parent, field, row):
        super().__init__(
            placeholder=_clip(_("Choose {name}").format(name=field.name), 100),
            min_values=1,
            max_values=1,
            row=row,
        )
        self._owner = parent
        self.field = field

    async def callback(self, interaction):
        await self._owner.on_select(interaction, self.field, self.values[0])


class _RoleSelect(discord.ui.RoleSelect):
    def __init__(self, parent, field, row):
        super().__init__(
            placeholder=_clip(_("Choose {name}").format(name=field.name), 100),
            min_values=1,
            max_values=1,
            row=row,
        )
        self._owner = parent
        self.field = field

    async def callback(self, interaction):
        await self._owner.on_select(interaction, self.field, self.values[0])


class _ChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent, field, row):
        kwargs = {}
        if field.channel_types:
            kwargs["channel_types"] = field.channel_types
        super().__init__(
            placeholder=_clip(_("Choose {name}").format(name=field.name), 100),
            min_values=1,
            max_values=1,
            row=row,
            **kwargs,
        )
        self._owner = parent
        self.field = field

    async def callback(self, interaction):
        await self._owner.on_select(interaction, self.field, self.values[0])


class _BoolSelect(discord.ui.Select):
    def __init__(self, parent, field, row):
        super().__init__(
            placeholder=_clip(_("Choose {name}").format(name=field.name), 100),
            min_values=1,
            max_values=1,
            row=row,
            options=[
                discord.SelectOption(label=_("Yes"), value="true"),
                discord.SelectOption(label=_("No"), value="false"),
            ],
        )
        self._owner = parent
        self.field = field

    async def callback(self, interaction):
        await self._owner.on_select(interaction, self.field, self.values[0] == "true")


def _make_select(parent, field, row):
    if field.kind == "member":
        return _MemberSelect(parent, field, row)
    if field.kind == "role":
        return _RoleSelect(parent, field, row)
    if field.kind == "channel":
        return _ChannelSelect(parent, field, row)
    return _BoolSelect(parent, field, row)


# --- buttons + modal ------------------------------------------------------


class _ActionButton(discord.ui.Button):
    def __init__(self, parent, label, style, action, row):
        super().__init__(label=label, style=style, row=row)
        self._owner = parent
        self.action = action

    async def callback(self, interaction):
        try:
            if self.action == "modal":
                await interaction.response.send_modal(_CompletionModal(self._owner))
            elif self.action == "run":
                await self._owner.maybe_finish(interaction)
            else:
                await self._owner.cancel(interaction)
        except Exception:
            log.exception("arg completion: button %r failed", self.action)
            await self._owner._report(interaction)


def _prefill_default(value):
    """Render a prefilled value as a modal text-box default.

    A discord object parsed before the missing argument (e.g. a User) prefills as
    its id so it re-converts; plain strings pass through; None clears the box.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "id"):
        return str(value.id)
    return str(value)


class _CompletionModal(LocaleModal):
    def __init__(self, parent):
        super().__init__(
            title=_clip(
                _("Complete {command}").format(
                    command=f"{parent.prefix}{parent.command.qualified_name}"
                ),
                45,
            )
        )
        self._owner = parent
        self.inputs = {}
        for field in parent.text_fields:
            existing = parent.collected.get(field.name)
            text_input = discord.ui.TextInput(
                label=_clip(field.name, 45),
                required=field.required,
                style=(
                    discord.TextStyle.paragraph
                    if field.consume_rest
                    else discord.TextStyle.short
                ),
                max_length=2000 if field.consume_rest else 400,
                default=_prefill_default(existing),
            )
            self.add_item(text_input)
            self.inputs[field.name] = text_input

    async def on_submit(self, interaction):
        try:
            for field in self._owner.text_fields:
                raw = str(self.inputs[field.name].value or "")
                if raw.strip() == "":
                    continue
                self._owner.set_value(field, raw)
            await self._owner.maybe_finish(interaction)
        except Exception:
            log.exception("arg completion: modal submit failed")
            await self._owner._report(interaction)


# --- entry point ----------------------------------------------------------


def _prefill(view, ctx) -> None:
    """Seed the form with the arguments discord.py already parsed before the gap.

    At MissingRequiredArgument time ctx.args/ctx.kwargs already hold the
    successfully converted leading arguments, so the user is not asked again for
    what they already typed. Best-effort: any problem just leaves the form empty.
    """
    try:
        # ctx.args == [cog, ctx, *positionals] for a cog command, else [ctx, ...].
        offset = 2 if ctx.command.cog is not None else 1
        positional = list(ctx.args[offset:])
        for field, value in zip(view.fields, positional):
            if value is not None:
                view.set_value(field, value)
        for name, value in (ctx.kwargs or {}).items():
            if value is None:
                continue
            for field in view.fields:
                if field.name == name:
                    view.set_value(field, value)
                    break
    except Exception:
        log.debug("arg completion: prefill failed", exc_info=True)


async def start(ctx, error) -> bool:
    """Try to launch an interactive completion. Returns True if a prompt was
    sent (the caller should not show the usage message), False otherwise."""
    command = ctx.command
    if command is None or command.hidden:
        return False
    if ctx.message.id in _inflight:
        return False

    try:
        fields = _build_fields(command)
    except Exception:
        log.exception("arg completion: failed to inspect %s", command)
        return False
    if not fields:
        return False

    select_fields = [f for f in fields if f.kind in _SELECT_KINDS]
    text_fields = [f for f in fields if f.kind not in _SELECT_KINDS]

    if ctx.guild is None and any(f.kind in _GUILD_SELECT_KINDS for f in fields):
        return False
    if len(select_fields) > 4 or len(text_fields) > 5:
        return False

    view = _CompletionView(ctx, fields, select_fields, text_fields)
    _prefill(view, ctx)
    try:
        view.message = await ctx.send(embed=view.build_embed(), view=view)
    except discord.HTTPException:
        log.exception("arg completion: failed to send prompt")
        return False
    return True

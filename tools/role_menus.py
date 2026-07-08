"""Pure helpers for self-assignable role menus (selection maths + config).

The cog owns the DB, the persistent views and the builder UI; this module only
computes which roles to add/remove for a user's selection and cleans a menu's
option list, so the rules are unit-testable without a bot.
"""

from __future__ import annotations

MAX_OPTIONS = 25  # Discord caps a select at 25 options
MAX_LABEL = 80
MAX_DESCRIPTION = 100


def resolve_selection(selected_ids, held_ids, menu_ids, *, exclusive):
    """Return (to_add, to_remove) sets for a user's menu selection.

    Only roles the menu manages (``menu_ids``) are ever touched. ``selected_ids``
    is what the user just picked, ``held_ids`` what they already have. An
    exclusive menu keeps at most one of its roles. Roles held from outside the
    menu are left completely alone.
    """
    menu = set(menu_ids)
    selected = set(selected_ids) & menu
    held = set(held_ids) & menu
    if exclusive and len(selected) > 1:
        selected = set(sorted(selected)[:1])
    return selected - held, held - selected


def normalize_options(blob):
    """Return a clean option list from stored/raw config.

    Each option is ``{"role_id": int, "label": str, "emoji": str|None,
    "description": str|None}``. Entries without a valid int role_id are dropped,
    duplicates by role_id collapse (first wins), and the list is capped at
    MAX_OPTIONS.
    """
    if not isinstance(blob, list):
        return []
    out = []
    seen = set()
    for entry in blob:
        if not isinstance(entry, dict):
            continue
        rid = entry.get("role_id")
        if not isinstance(rid, int) or isinstance(rid, bool) or rid in seen:
            continue
        seen.add(rid)
        label = str(entry.get("label") or rid)[:MAX_LABEL]
        emoji = entry.get("emoji") or None
        desc = entry.get("description")
        desc = str(desc)[:MAX_DESCRIPTION] if desc else None
        out.append(
            {"role_id": rid, "label": label, "emoji": emoji, "description": desc}
        )
        if len(out) >= MAX_OPTIONS:
            break
    return out

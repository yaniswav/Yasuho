"""Assemble locales/<code>/LC_MESSAGES/yasuho.po from the .pot + _work/<code>.json.

Safety net: a translation that introduces a {placeholder} not present in the
source msgid is REJECTED (left untranslated -> English fallback), so a bad
translation can never cause a runtime KeyError in str.format().

Plural entries (msgid_plural) cannot be expressed in the flat _work JSON, so
their translations are carried over from the locale's EXISTING .po: without
that, every rebuild silently wiped them back to English.
"""
import glob
import json
import os
import re

from babel.messages.pofile import read_po, write_po

POT = "locales/yasuho.pot"
WORK = "locales/_work"
DOMAIN = "yasuho"
NAME_RE = re.compile(r"\{([a-zA-Z0-9_]+)")


def names(s):
    return set(NAME_RE.findall(s or ""))



def _sized(forms, num_plurals):
    """Fit a carried-over plural translation to what THIS locale actually has.

    Japanese has no grammatical plural: its catalogue declares one form, and
    gettext hands back index 0 whatever the count is. A translation stored with
    two forms therefore rendered its SINGULAR for every n - "every week" for a
    reminder repeating every three weeks - and `pybabel compile` said only
    "msg has more translations than num_plurals", which is easy to read as noise.
    So when the locale wants fewer forms, keep the LAST one: where two forms
    differ it is the one carrying {count}, which stays true for every n.
    """
    forms = tuple(forms)
    if len(forms) == num_plurals:
        return forms
    if len(forms) > num_plurals:
        kept = [f for f in forms if f]
        chosen = kept[-1] if len(set(kept)) > 1 else (kept[0] if kept else "")
        return (chosen,) * num_plurals
    return forms + ("",) * (num_plurals - len(forms))


summary = []
for path in sorted(glob.glob(os.path.join(WORK, "*.json"))):
    code = os.path.splitext(os.path.basename(path))[0]
    # Skip scratch files (e.g. _new_fr.json): only real locale codes get built.
    if code.startswith("_"):
        continue
    try:
        trans = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        summary.append((code, "BAD JSON: %s" % e))
        continue

    with open(POT, "rb") as f:
        cat = read_po(f)
    try:
        cat.locale = code
        # Babel caches num_plurals on first read, and read_po() reads it off the
        # .pot (two forms, the English default) BEFORE the locale is known. Drop
        # the cache so the locale's own rule wins: without this every catalogue
        # is built as if it had two plural forms, including Japanese, which has
        # one - and gettext then serves index 0 for every count.
        cat._num_plurals = None
        cat._plural_expr = None
    except Exception:
        pass

    # Carry over existing plural translations (tuple msgids) from the current
    # .po - the flat JSON cannot hold them and rebuilding from the .pot alone
    # would reset them to empty.
    plurals = {}
    existing_po = os.path.join("locales", code, "LC_MESSAGES", DOMAIN + ".po")
    if os.path.exists(existing_po):
        with open(existing_po, "rb") as f:
            for msg in read_po(f):
                if isinstance(msg.id, tuple) and msg.string and any(msg.string):
                    plurals[msg.id] = msg.string

    total = applied = rejected = 0
    for msg in cat:
        if not msg.id:
            continue
        total += 1
        if isinstance(msg.id, tuple):
            if msg.id in plurals:
                msg.string = _sized(plurals[msg.id], cat.num_plurals)
                applied += 1
            elif msg.string:
                # Untranslated entries arrive from the .pot sized for TWO forms;
                # a one-form locale must not keep the spare.
                msg.string = _sized(msg.string, cat.num_plurals)
            continue
        t = trans.get(msg.id)
        if not t or not isinstance(t, str):
            continue
        # Reject any translation that adds an unknown placeholder (KeyError risk).
        if names(t) - names(msg.id):
            rejected += 1
            continue
        msg.string = t
        applied += 1

    d = os.path.join("locales", code, "LC_MESSAGES")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, DOMAIN + ".po"), "wb") as f:
        write_po(f, cat, width=0, omit_header=False)
    summary.append((code, "%d/%d applied, %d rejected" % (applied, total, rejected)))

for code, info in summary:
    print(code, "->", info)

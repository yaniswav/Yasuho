"""Assemble locales/<code>/LC_MESSAGES/yasuho.po from the .pot + _work/<code>.json.

Safety net: a translation that introduces a {placeholder} not present in the
source msgid is REJECTED (left untranslated -> English fallback), so a bad
translation can never cause a runtime KeyError in str.format().
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
    except Exception:
        pass

    total = applied = rejected = 0
    for msg in cat:
        if not msg.id:
            continue
        total += 1
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

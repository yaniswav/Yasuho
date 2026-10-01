"""?bigmoji must build the real Twemoji asset basename.

Twemoji's own parser only strips the variation selector U+FE0F, and only
when the emoji has no ZWJ (U+200D) anywhere in it; a ZWJ sequence keeps
every codepoint, FE0F included. Before the fix, ``fun.py`` concatenated raw
codepoints with no separator and never dropped FE0F, which breaks flags
(regional indicator pairs), skin-tone modifiers, ZWJ sequences and keycaps
alike against the real asset names jsdelivr/jdecked/twemoji serves.
"""

from cogs.fun.fun import twemoji_codepoints


def test_single_emoji_no_variation_selector():
    # Grinning face: U+1F600, nothing to strip.
    assert twemoji_codepoints("\U0001f600") == "1f600"


def test_flag_is_two_regional_indicators_joined_with_dash():
    # Flag: France = U+1F1EB U+1F1F7, no FE0F involved at all.
    assert twemoji_codepoints("\U0001f1eb\U0001f1f7") == "1f1eb-1f1f7"


def test_skin_tone_modifier_joined_with_dash():
    # Thumbs up + medium skin tone: U+1F44D U+1F3FD.
    assert twemoji_codepoints("\U0001f44d\U0001f3fd") == "1f44d-1f3fd"


def test_zwj_sequence_keeps_every_codepoint_fe0f_included():
    # Family: man, woman, girl, boy joined by ZWJ (no FE0F in this one, but
    # the ZWJ branch must pass codepoints through unchanged either way).
    family = (
        "\U0001f468‍\U0001f469‍\U0001f467‍\U0001f466"
    )
    assert twemoji_codepoints(family) == "1f468-200d-1f469-200d-1f467-200d-1f466"


def test_zwj_sequence_with_fe0f_is_not_stripped():
    # Eye in speech bubble: U+1F441 FE0F 200D 1F5E8 FE0F - the real Twemoji
    # asset for this one is 1f441-fe0f-200d-1f5e8-fe0f.png: FE0F survives
    # because a ZWJ is present.
    eye = "\U0001f441️‍\U0001f5e8️"
    assert twemoji_codepoints(eye) == "1f441-fe0f-200d-1f5e8-fe0f"


def test_keycap_drops_fe0f_same_as_any_non_zwj_sequence():
    # Keycap "#": U+0023 FE0F 20E3, no ZWJ anywhere - the real Twemoji asset
    # is 23-20e3.png (FE0F dropped), NOT 23-fe0f-20e3.png.
    keycap = "#️⃣"
    assert twemoji_codepoints(keycap) == "23-20e3"

"""Purpose: a pure Pillow renderer that turns a day-by-day activity series into
a PNG chart - the real-chart replacement for the text sparklines
(views.py's ``render_bar`` / ``_sparkline`` / ``_level_sparkline``) on the
``/serverstats`` card and the weekly digest.

PURE ON PURPOSE, same discipline as tools/welcome_card.py: this module knows
nothing about discord, the database or the event loop. It takes plain values
in (:class:`ChartPoint` tuples) and returns PNG bytes out. The caller
(views.py's ``build_chart_points`` for the card, digest.py's ``shape_digest``
for the weekly report) is the one place responsible for turning a rollups
``has_data=False`` point into a ``None`` here - this module just draws
whatever it is handed.

THE HONEST-STATS LAW, restated for a chart instead of a text bar (see
views.py's module docstring, rules 1-2): a day the collector was not
watching is a HOLE, never an invented zero. A hole never becomes a line
point, never becomes a bar, and never gets silently skipped as if it were
not there either - it is drawn as a gap in the line (the polyline breaks
rather than jumping straight from yesterday to tomorrow) plus a light
diagonal hatch across that day's column, so the eye reads "we were not
looking" rather than mistaking a blank column for "nothing happened". The
hatch covers exactly the region that is unknown (see ``_hatch_bounds``):
the message-line area for a ``messages`` hole, the joins/leaves strip for a
``net`` hole, the whole column when both are - because those two are two
different reads and either can be a hole on its own. A day that WAS watched
and genuinely saw zero messages (or a genuinely flat net) draws as a real,
visible zero - flat at the baseline - and must never be confused with a hole.

The GHOST (``previous_points``) carries the same law one level up: it is
only ever meaningful when the WHOLE previous period was observed, so the
caller passes it only when its own honesty gate says so (digest.py mirrors
DigestReport.delta_pct's "both weeks fully observed" test) - this module
does not second-guess that decision, it just draws whatever series it is
given, lighter AND dashed (the chart carries no legend, so the dash is what
names the ghost - see the palette block).

Typography rule: ASCII '-' and '...' only (there is no user-facing text in
this module - day labels are plain digits - but the house rule still governs
comments and identifiers).
"""

from __future__ import annotations

import io
import math
from collections import namedtuple

from PIL import Image, ImageDraw, ImageFont

# One row per day. ``messages`` is the line value; ``net`` is that day's
# joins-minus-leaves, drawn as a small bar. Either is ``None`` on a day the
# collector was NOT watching - a hole, never a zero (see the module
# docstring). Plain values only: this namedtuple carries no rollups/discord
# type anywhere.
ChartPoint = namedtuple("ChartPoint", "day messages net")

# ---------------------------------------------------------------------------
# Canvas + palette.
#
# ONE dark palette, hardcoded, ON PURPOSE - and here is what that means on a
# LIGHT client, because Discord renders the same attachment on both: the PNG
# carries its own opaque background, so a light-theme reader sees a dark tile
# sitting in a light message. That is a deliberate trade, not an oversight. A
# static image cannot adapt to a theme the renderer never learns, and the
# alternative (rendering and uploading two variants for one message that can
# only ever display one of them) doubles the cost of every card and digest
# for a viewer we cannot identify. Every contrast below is therefore measured
# against THIS image's own background, which is the only surface that matters.
#
# MEASURED (WCAG ratio vs BG_COLOR, and OKLab CVD separation, checked with the
# dataviz palette validator - re-run it before touching any swatch):
#   messages line 3.93:1, joins 8.7:1, leaves 3.29:1, axis text 4.5:1
#   joins vs leaves: OKLab dE 20.9 under deuteranopia, 21.9 under tritanopia
#
# TWO deliberate deviations, both load-bearing:
#
# 1. The joins green is LIGHTER than a categorical palette's dark-mode
#    lightness band wants (OKLCH L 0.855). That gap is exactly what makes the
#    green/red pair survive colour blindness: pulled into band (say
#    Discord's #3BA55D) the pair collapses to OKLab dE 4.0 under deuteranopia
#    - indistinguishable. Do not "fix" the green without re-measuring the
#    pair. The bars also encode their sign geometrically (up from the zero
#    baseline for a gain, down for a loss), so the reading never rests on
#    colour alone in the first place.
# 2. The ghost line is the ONLY other series, and there is no legend to name
#    it (a PNG cannot carry translatable text - see the module docstring), so
#    it is DASHED as well as faded. Dash-vs-solid is what says "previous
#    period" without a word and without relying on a lightness difference a
#    colour-blind or low-contrast reader may not resolve.
# ---------------------------------------------------------------------------
CHART_WIDTH = 880
CHART_HEIGHT = 300

# Hard ceiling on how many days one render will draw, so the cost of a call is
# bounded by this module rather than by whatever a caller hands it. Both real
# callers are already far below it (7 days for the digest, 30 for the card, and
# rollups.MAX_WINDOW_DAYS caps any window at 90) - this exists so a future
# caller bug, or a window widened without thinking about pixels, degrades into
# "the most recent MAX_POINTS days" instead of a multi-second Pillow job on the
# shared image semaphore. MEASURED on the dev box: the card's 30 days render in
# ~13 ms and this ceiling in ~17 ms, where 20000 uncapped points cost ~860 ms -
# which is why the ceiling sits here and not higher. The tail is kept rather
# than the head: a chart reads left to right in time and the RECENT end is the
# half anyone looks at.
MAX_POINTS = 400

BG_COLOR = (49, 51, 56)            # #313338 - Discord dark theme's base background
GRID_COLOR = (61, 64, 69)          # a hair lighter than BG - barely-there gridlines
AXIS_TEXT_COLOR = (148, 155, 164)  # #949BA4 - Discord's muted/secondary text grey
# #7A85F7 - Discord "blurple", LIGHTENED from the brand #5865F2, which reads at
# only 2.74:1 on this background: the messages line is the chart's primary mark
# and it was the least visible thing on it. This step keeps the hue and clears
# the 3:1 floor for non-text marks.
MESSAGE_LINE_COLOR = (122, 133, 247)
JOIN_BAR_COLOR = (87, 242, 135)    # #57F287 - Discord green, a net-positive day
LEAVE_BAR_COLOR = (237, 66, 69)    # #ED4245 - Discord red, a net-negative day
GAP_HATCH_COLOR = (71, 74, 80)     # #474A50 - the "not watching" hatch: a texture
                                   # that MEANS something, so it sits above the
                                   # purely decorative gridlines, not below them
GHOST_LINE_ALPHA = 0.50            # how strongly the previous-period ghost blends
                                   # into BG - 2.03:1, readable but plainly
                                   # subordinate to the 3.93:1 current line
_GHOST_DASH = (9, 6)               # on/off pixels of the ghost's dash pattern

# Reuse the TTF already bundled for card rendering (see
# cogs/community/leveling/leveling.py's _load_font and
# tools/welcome_card.py's _load_fonts - same path, same truetype-then-default
# fallback) rather than shipping a font for one more surface.
_FONT_PATH = "ressources/fonts/impact.ttf"
_LABEL_FONT_SIZE = 15

# Geometry, named rather than inlined so the test suite computes the SAME
# pixel positions the renderer uses instead of guessing at them.
_PLOT_LEFT = 24
_PLOT_RIGHT = 24
_PLOT_TOP = 18
_LABEL_AREA_HEIGHT = 22   # room for the sparse day labels along the bottom
_BAR_AREA_HEIGHT = 42     # the joins/leaves strip, under the message line
_BAR_GAP = 8              # breathing room between the line plot and the bar strip
_WEEKLY_TICK = 7          # a day label every 7th point - "sparse", per the brief
_HATCH_STEP = 10          # pixel spacing between diagonal hatch strokes
_LABEL_MIN_GAP = 6        # pixels two day labels must keep apart, or one is dropped


def _load_label_font():
    """Load the bundled TTF at the axis-label size, PIL default on failure."""
    try:
        return ImageFont.truetype(_FONT_PATH, size=_LABEL_FONT_SIZE)
    except Exception:
        return ImageFont.load_default()


def _blend(fg, bg, alpha):
    """Alpha-blend ``fg`` over ``bg`` (both ``(r, g, b)``) into a solid RGB.

    Pillow's :class:`~PIL.ImageDraw.ImageDraw` does not blend against
    existing pixels on an RGB image - a fill colour simply overwrites
    whatever was already there - so the ghost line's "lighter" look has to
    be baked into the colour itself, computed once, rather than drawn with a
    real alpha channel.
    """
    return tuple(
        round(fg_c * alpha + bg_c * (1 - alpha)) for fg_c, bg_c in zip(fg, bg)
    )


def _plot_geometry(width, height):
    """The pixel rectangle(s) every drawing call below shares - a plain dict
    of ints so drawing code and tests read the exact same numbers."""
    plot_bottom = height - _LABEL_AREA_HEIGHT - _BAR_AREA_HEIGHT - _BAR_GAP
    bar_top = plot_bottom + _BAR_GAP
    bar_bottom = bar_top + _BAR_AREA_HEIGHT
    return {
        "left": _PLOT_LEFT,
        "right": width - _PLOT_RIGHT,
        "top": _PLOT_TOP,
        "bottom": plot_bottom,
        "bar_top": bar_top,
        "bar_bottom": bar_bottom,
        "bar_mid": (bar_top + bar_bottom) / 2.0,
        "label_y": bar_bottom + 4,
    }


def _point_x(index, count, left, right):
    """The x pixel of the ``index``-th of ``count`` points, evenly spread
    across ``[left, right]``. A single point centres itself."""
    if count <= 1:
        return (left + right) / 2.0
    return left + (right - left) * index / (count - 1)


def _column_span(index, count, left, right):
    """The ``[x0, x1]`` pixel span one day "owns" - half the distance to
    each neighbour, clamped to the plot. Sizes the gap hatch so a hole reads
    as one whole day wide, not a point-sized sliver."""
    x = _point_x(index, count, left, right)
    if count <= 1:
        return left, right
    step = (right - left) / (count - 1)
    return max(left, x - step / 2.0), min(right, x + step / 2.0)


def _known_extent(*series):
    """The largest ABSOLUTE known value across every series given, or
    ``None`` when nothing anywhere is known.

    Callers use this as ONE shared scale across a whole call - the same
    discipline views.render_bar applies to its text bars - so a ghost line
    and the real line (or every bar in the joins/leaves strip) stay
    comparable to each other rather than each being scaled against itself.
    """
    values = [
        abs(value)
        for points in series
        if points is not None
        for value in points
        if value is not None
    ]
    return max(values) if values else None


def _hatch_bounds(geometry, message_hole, net_hole):
    """The vertical band a day's hatch covers, PER REGION.

    A day can be a hole in one series and known in the other (the message
    line and the joins/leaves strip are two different reads), so the hatch
    marks exactly the region that is unknown: the line area for a
    ``messages`` hole, the bar strip for a ``net`` hole, the whole column
    when both are unknown. Marking only one of them was the bug this
    replaced - a known ``messages`` day whose ``net`` was unknown drew no
    bar AND no hatch, which is pixel for pixel what an observed "net 0" day
    looks like. Under the honest-stats law those two must never render the
    same.
    """
    top = geometry["top"] if message_hole else geometry["bar_top"]
    bottom = geometry["bar_bottom"] if net_hole else geometry["bottom"]
    return top, bottom


def _draw_hatch(draw, x0, x1, y0, y1):
    """A few short diagonal strokes across one gap column.

    Visibly not the plain background, and visibly not a drawn value: the
    honest-stats law forbids a hole from looking like a real zero, so it may
    not be a bar or a line point either - just a texture that reads as "we
    were not watching that day".
    """
    if x1 - x0 < 1 or y1 - y0 < 1:
        return
    span = y1 - y0
    y = y0 - span
    while y < y1:
        # Every stroke runs (x0, y) -> (x1, y + span) and is CLIPPED to the
        # band before it is drawn. Without the clip the diagonals spill a
        # whole band-height above and below - over the day labels, and (now
        # that a hole can be marked in one region only) over the OTHER
        # series' area, which would texture a perfectly known day as
        # unwatched.
        low = max(0.0, (y0 - y) / span)
        high = min(1.0, (y1 - y) / span)
        if high > low:
            draw.line(
                [
                    (x0 + (x1 - x0) * low, y + span * low),
                    (x0 + (x1 - x0) * high, y + span * high),
                ],
                fill=GAP_HATCH_COLOR,
                width=1,
            )
        y += _HATCH_STEP


def _draw_grid(draw, geometry):
    """Subtle horizontal gridlines across the message-line area, plus the
    joins/leaves strip's zero baseline (the one line that means something
    specific - net == 0 - so it is drawn even though the lines above it are
    purely decorative structure)."""
    left, right, top, bottom = (
        geometry["left"], geometry["right"], geometry["top"], geometry["bottom"]
    )
    for fraction in (0.0, 1 / 3, 2 / 3, 1.0):
        y = round(top + (bottom - top) * fraction)
        draw.line([(left, y), (right, y)], fill=GRID_COLOR, width=1)
    mid = round(geometry["bar_mid"])
    draw.line([(left, mid), (right, mid)], fill=GRID_COLOR, width=1)


def _line_y(value, peak, top, bottom):
    """Map a known message value to a pixel y - the largest value at TOP,
    zero at BOTTOM. ``peak`` <= 0 (nothing known, or a flat all-zero window)
    maps every value to the baseline."""
    if peak <= 0:
        return bottom
    fraction = min(1.0, value / peak)
    return bottom - (bottom - top) * fraction


def _dash_runs(segment, on, off):
    """Cut one polyline into the DRAWN runs of an ``on``/``off`` dash pattern.

    Length is measured ALONG the line and carried across vertices, so the
    dashes stay evenly spaced through the bends instead of restarting (and
    visibly bunching) at every data point.
    """
    if on <= 0 or off <= 0:
        return [segment]
    runs = []
    run = [segment[0]]
    drawing = True
    left = on
    for start, end in zip(segment, segment[1:]):
        length = math.hypot(end[0] - start[0], end[1] - start[1])
        travelled = 0.0
        while length - travelled > left:
            travelled += left
            fraction = travelled / length
            cut = (
                start[0] + (end[0] - start[0]) * fraction,
                start[1] + (end[1] - start[1]) * fraction,
            )
            if drawing:
                run.append(cut)
                runs.append(run)
            run = [cut]
            drawing = not drawing
            left = on if drawing else off
        left -= length - travelled
        if drawing:
            run.append(end)
    if drawing and len(run) >= 2:
        runs.append(run)
    return runs


def _draw_segment(draw, segment, color, dash):
    """Draw one unbroken run of known points - solid, or dashed when ``dash``
    is given. A run of ONE point has no line to draw and becomes a dot."""
    if len(segment) == 1:
        x, y = segment[0]
        draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=color)
        return
    if len(segment) < 2:
        return
    for run in _dash_runs(segment, *dash) if dash else [segment]:
        draw.line(run, fill=color, width=2, joint="curve")


def _draw_series(draw, points, geometry, peak, color, dash=None):
    """One polyline, BROKEN at every hole - never interpolated across a gap,
    never a phantom zero (the honest-stats law). A run of consecutive known
    points is joined; a ``None`` simply ends the current segment, and the
    next known point starts a fresh one. A known point stranded between two
    holes (nothing to draw a LINE to) still gets a small dot: "this one day
    is real data" must survive even in total isolation.

    ``dash`` is the ``(on, off)`` pattern the previous-period ghost is drawn
    with - the non-colour half of telling the two series apart on a chart
    that has no room for a legend (see the palette block).
    """
    left, right, top, bottom = (
        geometry["left"], geometry["right"], geometry["top"], geometry["bottom"]
    )
    count = len(points)
    segment = []
    for index, point in enumerate(points):
        if point.messages is None:
            _draw_segment(draw, segment, color, dash)
            segment = []
            continue
        x = _point_x(index, count, left, right)
        y = _line_y(point.messages, peak, top, bottom)
        segment.append((x, y))
    _draw_segment(draw, segment, color, dash)


def _draw_bars(draw, points, geometry, peak):
    """The joins/leaves strip: one thin bar per KNOWN net value, growing up
    (green) for a net gain and down (red) for a net loss from the strip's
    zero baseline. A ``None`` (hole) draws no bar at all - the gap hatch
    already marks that whole column, so an absent bar there is not read as
    "net zero", it is read as "no data", exactly like the message line's own
    break. A real net of zero draws as a bar of height zero: present,
    honest, and visually just the baseline itself - never confused with a
    hole because a hole has NO bar geometry drawn at all, hatch aside.
    """
    left, right = geometry["left"], geometry["right"]
    mid = geometry["bar_mid"]
    half_height = _BAR_AREA_HEIGHT / 2.0
    count = len(points)
    for index, point in enumerate(points):
        if point.net is None:
            continue
        x0, x1 = _column_span(index, count, left, right)
        bar_width = max(2.0, (x1 - x0) * 0.5)
        x = _point_x(index, count, left, right)
        fraction = min(1.0, abs(point.net) / peak) if peak and peak > 0 else 0.0
        extent = half_height * fraction
        color = JOIN_BAR_COLOR if point.net >= 0 else LEAVE_BAR_COLOR
        if point.net >= 0:
            draw.rectangle(
                (x - bar_width / 2, mid - extent, x + bar_width / 2, mid), fill=color
            )
        else:
            draw.rectangle(
                (x - bar_width / 2, mid, x + bar_width / 2, mid + extent), fill=color
            )


def _label_anchor(x, label_width, left, right):
    """Where a label of ``label_width`` starts to sit centred on ``x`` without
    spilling out of the plot on either side."""
    return min(max(x - label_width / 2.0, left), max(left, right - label_width))


def _draw_labels(draw, points, geometry, font):
    """Day labels at WEEKLY ticks only (``_WEEKLY_TICK``), plus always the
    last point - a 30-day chart with one label per day would be an
    unreadable smear, and the last point is the one a reader looks for
    first ("where does this end").

    Placed from the LAST label BACKWARDS, dropping any earlier label that
    would come within ``_LABEL_MIN_GAP`` of the one already placed to its
    right. That order is the whole point: the card's window is exactly 30
    days, whose weekly ticks land on 0/7/14/21/28 with the last point at 29,
    so the tick and the end label were being drawn a few pixels apart and
    overprinted into an unreadable smear ("08-0708"). The end of the window
    always wins; a weekly tick is what gets dropped.

    Labels are ``%m-%d`` digits - deliberately NOT a localized date: this is
    a static PNG one guild-wide render has to serve every viewer of the
    message, so it carries no translatable text at all (see the module
    docstring's typography rule).
    """
    left, right = geometry["left"], geometry["right"]
    count = len(points)
    y = geometry["label_y"]

    candidates = [index for index in range(count) if index % _WEEKLY_TICK == 0]
    if count and candidates[-1] != count - 1:
        candidates.append(count - 1)

    placed = []
    next_start = None
    for index in reversed(candidates):
        label = points[index].day.strftime("%m-%d")
        label_width = draw.textlength(label, font=font)
        anchor_x = _label_anchor(
            _point_x(index, count, left, right), label_width, left, right
        )
        if next_start is not None and anchor_x + label_width + _LABEL_MIN_GAP > next_start:
            continue
        placed.append((anchor_x, label))
        next_start = anchor_x
    for anchor_x, label in placed:
        draw.text((anchor_x, y), label, font=font, fill=AXIS_TEXT_COLOR)


def _draw_peak(draw, geometry, peak, font):
    """The chart's y-axis scale, reduced to the ONE number that defines it:
    the peak of the shared message scale (current line and ghost together,
    the same ``_known_extent`` every line point is mapped against), written
    in the top-left. Digits and thousands separators only - like the day
    labels, deliberately not localized text (see the module docstring's
    typography rule).

    It sits ABOVE the top gridline, in the margin the plot never draws
    into: the peak value itself maps exactly to that gridline, so text
    placed inside the plot area could sit right on the line when the first
    day is the peak. Only if the font is taller than the margin does the
    text drop to the top of the line area, on a 2px background-colour
    backing rect so the line still cannot run through it.
    """
    label = format(peak, ",")
    left = geometry["left"]
    bbox = draw.textbbox((0, 0), label, font=font)
    y = geometry["top"] - bbox[3] - 2
    if y < 0:
        y = 0
        draw.rectangle(
            (
                left + bbox[0] - 2,
                y + bbox[1] - 2,
                left + bbox[2] + 2,
                y + bbox[3] + 2,
            ),
            fill=BG_COLOR,
        )
    draw.text((left, y), label, font=font, fill=AXIS_TEXT_COLOR)


def render_activity_chart(
    points, *, previous_points=None, width=CHART_WIDTH, height=CHART_HEIGHT
):
    """Render a day-by-day activity chart to PNG bytes. Pure and total.

    ``points`` is a sequence of :class:`ChartPoint`, oldest first, covering
    the whole window (rollups.DEFAULT_SERIES_DAYS for the card, 7 for the
    digest's reported week).
    ``messages`` draws the main line; ``net`` (joins minus leaves) draws the
    small bars in the strip underneath. Either is ``None`` on a day the
    collector was not watching, rendered as a broken line / no bar plus a
    light diagonal hatch across that day's column (see the module
    docstring's honest-stats law) - never as an invented zero.

    ``previous_points`` is an OPTIONAL ghosted comparison series, the exact
    same shape and length as ``points``, drawn lighter AND DASHED behind the
    main line - the digest's week-over-week overlay. Pass it only when the whole
    period it covers was fully observed (mirror DigestReport.delta_pct's own
    gate): a comparison partly made of holes is not an honest ghost, it is a
    guess with a line drawn through it. Raises :class:`ValueError` if its
    length does not match ``points`` - a caller bug, not a data condition to
    paper over silently.

    The peak of the shared message scale is written in the top-left of the
    chart (digits and thousands separators only, muted axis grey) - the one
    number that turns the unlabelled y axis into a readable scale. A window
    with no known message value anywhere writes nothing: an unknown scale
    has no peak to state.

    Never raises for empty ``points`` (returns a bare grid) or an all-hole
    window (grid + hatch, no line, no bars, no crash on a zero-valued
    scale). More than ``MAX_POINTS`` days keeps the most recent
    ``MAX_POINTS`` rather than drawing (and paying for) all of them.
    Returns PNG bytes.
    """
    points = tuple(points or ())
    if previous_points is not None:
        previous_points = tuple(previous_points)
        if len(previous_points) != len(points):
            raise ValueError("previous_points must be the same length as points")
    # The ceiling is applied AFTER the length check, and to both series with
    # the same slice, so the ghost stays day-aligned with the main line.
    if len(points) > MAX_POINTS:
        points = points[-MAX_POINTS:]
        if previous_points is not None:
            previous_points = previous_points[-MAX_POINTS:]

    image = Image.new("RGB", (width, height), BG_COLOR)
    draw = ImageDraw.Draw(image)
    geometry = _plot_geometry(width, height)
    _draw_grid(draw, geometry)

    count = len(points)
    for index, point in enumerate(points):
        message_hole = point.messages is None
        net_hole = point.net is None
        if not (message_hole or net_hole):
            continue
        x0, x1 = _column_span(index, count, geometry["left"], geometry["right"])
        _draw_hatch(draw, x0, x1, *_hatch_bounds(geometry, message_hole, net_hole))

    known_peak = _known_extent(
        [p.messages for p in points],
        [p.messages for p in previous_points] if previous_points is not None else None,
    )
    peak = known_peak or 0

    if previous_points is not None:
        ghost_color = _blend(MESSAGE_LINE_COLOR, BG_COLOR, GHOST_LINE_ALPHA)
        _draw_series(
            draw, previous_points, geometry, peak, ghost_color, dash=_GHOST_DASH
        )
    _draw_series(draw, points, geometry, peak, MESSAGE_LINE_COLOR)

    bar_peak = _known_extent([p.net for p in points]) or 0
    _draw_bars(draw, points, geometry, bar_peak)

    if points:
        font = _load_label_font()
        # The scale indicator only exists when there IS a scale: an all-hole
        # window has no known peak, and writing an invented "0" over it would
        # be exactly the phantom zero the honest-stats law forbids.
        if known_peak is not None:
            _draw_peak(draw, geometry, known_peak, font)
        _draw_labels(draw, points, geometry, font)

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()

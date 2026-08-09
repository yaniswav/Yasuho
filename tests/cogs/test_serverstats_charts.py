"""U3: the PNG activity chart (cogs/community/serverstats/charts.py).

Pure-function tests only - no discord, no DB, no event loop, matching the
module's own contract. What is pinned here:

1. Total and side-effect free: empty input, an all-hole window, dimensions
   and mode never vary by input.
2. THE HONEST-STATS LAW: a hole (``messages=None`` / ``net=None``) never
   draws as a line point or a bar - a gap column carries only the
   background colour and the hatch texture, NEVER the line colour or either
   bar colour. A KNOWN zero (a real, observed silence) draws differently
   from a hole.
3. The shared-scale discipline (``_known_extent``) and the ghost line's
   validation (same length as the main series, or raise).
"""

import datetime
import io
import pathlib

import pytest
from PIL import Image, ImageDraw

from cogs.community.serverstats import charts

DAY = datetime.date(2026, 7, 28)
ONE_DAY = datetime.timedelta(days=1)


def _points(*values):
    """``values`` is a list of ``(messages, net)`` pairs (either may be
    ``None``), oldest first, one per day starting at DAY - ONE_DAY * (n-1)."""
    start = DAY - ONE_DAY * (len(values) - 1)
    return [
        charts.ChartPoint(day=start + ONE_DAY * index, messages=messages, net=net)
        for index, (messages, net) in enumerate(values)
    ]


def _decode(data):
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def _column_pixels(image, geometry, x0, x1):
    x = int((x0 + x1) / 2)
    return {
        image.getpixel((x, y))
        for y in range(geometry["top"], geometry["bar_bottom"])
    }


# ---------------------------------------------------------------------------
# Total and side-effect free
# ---------------------------------------------------------------------------
def test_the_renderer_stays_executor_safe_and_translation_free():
    """This module runs inside tools.rendering.run_image_job, i.e. in a
    worker THREAD off the event loop. It may therefore never await, never
    touch discord, and never carry a translatable string (a PNG is rendered
    once and shown to every viewer of the message, whatever their locale)."""
    source = pathlib.Path(charts.__file__).read_text(encoding="utf-8")
    assert "import discord" not in source
    assert "async def" not in source
    assert "await " not in source
    assert "_(" not in source.replace("charts._(", "")
def test_empty_points_renders_a_bare_grid_without_raising():
    data = charts.render_activity_chart(())
    image = _decode(data)
    assert image.size == (charts.CHART_WIDTH, charts.CHART_HEIGHT)


def test_default_dimensions_and_mode():
    data = charts.render_activity_chart(_points((10, 1), (20, -1)))
    image = _decode(data)
    assert image.size == (charts.CHART_WIDTH, charts.CHART_HEIGHT)
    assert image.mode == "RGB"


def test_custom_dimensions_are_honoured():
    data = charts.render_activity_chart(_points((10, 1)), width=400, height=150)
    image = _decode(data)
    assert image.size == (400, 150)


def test_an_all_hole_window_draws_no_line_and_no_bar_anywhere():
    """Nothing known at all: the honest answer is a plain grid plus hatch,
    never an invented flat line at zero."""
    points = _points((None, None), (None, None), (None, None))
    data = charts.render_activity_chart(points)
    image = _decode(data)
    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    colors = {
        image.getpixel((x, y))
        for x in range(geometry["left"], geometry["right"])
        for y in range(geometry["top"], geometry["bar_bottom"])
    }
    assert charts.MESSAGE_LINE_COLOR not in colors
    assert charts.JOIN_BAR_COLOR not in colors
    assert charts.LEAVE_BAR_COLOR not in colors


def test_a_single_known_point_does_not_crash_and_draws_a_dot():
    """A lone known day between two holes has nothing to draw a LINE to, but
    it must still leave a visible mark - not vanish as if it were a hole
    too."""
    points = _points((None, None), (500, 2), (None, None))
    data = charts.render_activity_chart(points)
    image = _decode(data)
    colors = {image.getpixel((x, y)) for x in range(image.width) for y in range(image.height)}
    assert charts.MESSAGE_LINE_COLOR in colors


# ---------------------------------------------------------------------------
# The honest-stats law: a hole is a gap, never a zero
# ---------------------------------------------------------------------------
def test_a_hole_column_never_carries_the_line_colour():
    points = _points((100, 5), (None, None), (50, -3))
    data = charts.render_activity_chart(points)
    image = _decode(data)
    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    x0, x1 = charts._column_span(1, 3, geometry["left"], geometry["right"])
    colors = _column_pixels(image, geometry, x0, x1)
    assert charts.MESSAGE_LINE_COLOR not in colors


def test_a_hole_column_never_carries_a_bar_colour():
    points = _points((100, 5), (None, None), (50, -3))
    data = charts.render_activity_chart(points)
    image = _decode(data)
    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    x0, x1 = charts._column_span(1, 3, geometry["left"], geometry["right"])
    colors = _column_pixels(image, geometry, x0, x1)
    assert charts.JOIN_BAR_COLOR not in colors
    assert charts.LEAVE_BAR_COLOR not in colors


def test_a_mid_week_hole_breaks_the_line_across_its_WHOLE_column():
    """The load-bearing one: not the middle pixel of the gap, EVERY pixel
    column the missing day owns. A polyline that merely dipped, or that was
    drawn straight from the day before to the day after, would put line
    colour somewhere in this band - the line has to physically stop."""
    points = _points((100, 2), (200, -1), (300, 3), (None, None), (250, 1), (400, -2), (350, 0))
    image = _decode(charts.render_activity_chart(points))
    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    x0, x1 = charts._column_span(3, 7, geometry["left"], geometry["right"])

    colors = set()
    for x in range(int(x0) + 1, int(x1)):
        for y in range(geometry["top"], geometry["bar_bottom"]):
            colors.add(image.getpixel((x, y)))

    assert charts.MESSAGE_LINE_COLOR not in colors
    assert charts.JOIN_BAR_COLOR not in colors
    assert charts.LEAVE_BAR_COLOR not in colors
    assert charts.GAP_HATCH_COLOR in colors


def test_a_net_hole_hatches_the_bar_strip_even_when_messages_are_known():
    """The two series are two different reads, so a day can be a hole in one
    and known in the other. An unknown net used to draw NOTHING in the strip
    - pixel for pixel what an observed 'net 0' day looks like - which is the
    invented zero the honest-stats law forbids."""
    points = _points((100, 2), (200, None), (300, 3))
    image = _decode(charts.render_activity_chart(points))
    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    x0, x1 = charts._column_span(1, 3, geometry["left"], geometry["right"])

    def _band(top, bottom):
        return {
            image.getpixel((x, y))
            for x in range(int(x0) + 1, int(x1))
            for y in range(int(top), int(bottom))
        }

    strip = _band(geometry["bar_top"], geometry["bar_bottom"])
    assert charts.GAP_HATCH_COLOR in strip
    assert charts.JOIN_BAR_COLOR not in strip
    assert charts.LEAVE_BAR_COLOR not in strip
    # ... and the message line, which IS known that day, keeps its column
    # clean: the hatch marks the unknown region only.
    assert charts.GAP_HATCH_COLOR not in _band(geometry["top"], geometry["bottom"])


def test_the_hatch_stays_inside_its_own_band():
    """A gap column's texture is clipped to the region it marks - it must
    not bleed over the day labels underneath, nor into a neighbouring
    region whose data IS known."""
    points = _points((100, 2), (None, None), (300, 3))
    image = _decode(charts.render_activity_chart(points))
    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    x0, x1 = charts._column_span(1, 3, geometry["left"], geometry["right"])

    below = {
        image.getpixel((x, y))
        for x in range(int(x0) + 1, int(x1))
        for y in range(int(geometry["bar_bottom"]) + 1, charts.CHART_HEIGHT)
    }
    above = {
        image.getpixel((x, y))
        for x in range(int(x0) + 1, int(x1))
        for y in range(0, geometry["top"])
    }
    assert charts.GAP_HATCH_COLOR not in below
    assert charts.GAP_HATCH_COLOR not in above


def test_a_net_hole_and_an_observed_net_zero_do_not_render_alike():
    hole = charts.render_activity_chart(_points((100, 2), (200, None), (300, 3)))
    zero = charts.render_activity_chart(_points((100, 2), (200, 0), (300, 3)))
    assert hole != zero


def test_a_message_hole_hatches_the_line_area_only_when_the_net_is_known():
    """The mirror case: nothing known about messages, but the day WAS
    watched for joins/leaves. The bar is real and must not be hatched over."""
    points = _points((100, 2), (None, 4), (300, 3))
    image = _decode(charts.render_activity_chart(points))
    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    x0, x1 = charts._column_span(1, 3, geometry["left"], geometry["right"])
    strip = {
        image.getpixel((x, y))
        for x in range(int(x0) + 1, int(x1))
        for y in range(int(geometry["bar_top"]), int(geometry["bar_bottom"]))
    }
    assert charts.GAP_HATCH_COLOR not in strip
    assert charts.JOIN_BAR_COLOR in strip


def test_a_hole_column_carries_the_hatch_texture():
    points = _points((100, 5), (None, None), (50, -3))
    data = charts.render_activity_chart(points)
    image = _decode(data)
    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    x0, x1 = charts._column_span(1, 3, geometry["left"], geometry["right"])
    colors = _column_pixels(image, geometry, x0, x1)
    assert charts.GAP_HATCH_COLOR in colors


def test_a_known_zero_day_draws_differently_from_a_hole():
    """A REAL, observed zero (net=0, messages=0) must not look like a hole:
    no hatch on its column, and its bar geometry is drawn (even if that
    geometry is a zero-height rectangle, present rather than absent)."""
    points = _points((100, 5), (0, 0), (50, -3))
    data = charts.render_activity_chart(points)
    image = _decode(data)
    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    x0, x1 = charts._column_span(1, 3, geometry["left"], geometry["right"])
    colors = _column_pixels(image, geometry, x0, x1)
    assert charts.GAP_HATCH_COLOR not in colors


def test_known_zero_and_hole_are_visually_distinguishable():
    known_zero = charts.render_activity_chart(_points((10, 1), (0, 0), (10, 1)))
    hole = charts.render_activity_chart(_points((10, 1), (None, None), (10, 1)))
    assert known_zero != hole


# ---------------------------------------------------------------------------
# The ghost (previous_points)
# ---------------------------------------------------------------------------
def test_ghost_series_must_match_the_main_series_length():
    points = _points((10, 1), (20, 2))
    with pytest.raises(ValueError):
        charts.render_activity_chart(points, previous_points=_points((5, 0)))


def test_a_ghost_series_changes_the_render():
    points = _points((10, 1), (20, 2), (30, 3))
    plain = charts.render_activity_chart(points)
    ghosted = charts.render_activity_chart(points, previous_points=_points((5, 0), (8, 0), (12, 0)))
    assert plain != ghosted


def test_dash_runs_cut_a_line_into_evenly_spaced_pieces():
    """Pure geometry: the pattern is measured ALONG the line and carried
    across vertices, so the dashes do not restart (and bunch) at every data
    point."""
    runs = charts._dash_runs([(0.0, 0.0), (100.0, 0.0)], 10, 10)

    assert len(runs) == 5
    assert runs[0] == [(0.0, 0.0), (10.0, 0.0)]
    assert runs[1][0][0] == 20.0
    # Across a bend, the second leg picks the pattern up where the first left
    # it rather than starting a fresh dash.
    bent = charts._dash_runs([(0.0, 0.0), (15.0, 0.0), (15.0, 15.0)], 10, 10)
    assert any(run[0] != (15.0, 0.0) for run in bent)


def test_dash_runs_degrade_to_a_solid_line_on_a_useless_pattern():
    segment = [(0.0, 0.0), (10.0, 0.0)]
    assert charts._dash_runs(segment, 0, 5) == [segment]


def test_the_ghost_is_dashed_and_the_current_line_is_not():
    """The two series are told apart WITHOUT a legend (a PNG carries no
    translatable text) and without relying on colour alone: solid is now,
    dashed is the period before."""
    points = _points((10, 0), (90, 0), (10, 0), (90, 0), (10, 0))
    ghost = _points((50, 0), (50, 0), (50, 0), (50, 0), (50, 0))
    image = _decode(charts.render_activity_chart(points, previous_points=ghost))
    ghost_color = charts._blend(
        charts.MESSAGE_LINE_COLOR, charts.BG_COLOR, charts.GHOST_LINE_ALPHA
    )

    def _columns(color):
        return {
            x
            for x in range(image.width)
            for y in range(image.height)
            if image.getpixel((x, y)) == color
        }

    ghost_columns = _columns(ghost_color)
    line_columns = _columns(charts.MESSAGE_LINE_COLOR)
    assert ghost_columns and line_columns
    # The ghost is a FLAT line across the plot, so a solid one would paint
    # every column between its ends; the dashes leave holes.
    span = range(min(ghost_columns), max(ghost_columns) + 1)
    assert len(ghost_columns) < len(span) * 0.9
    # The current line, over the same span, is continuous.
    line_span = range(min(line_columns), max(line_columns) + 1)
    assert set(line_span) <= line_columns


def test_ghost_and_main_share_one_peak_scale():
    """The ghost must not rescale the main line against its OWN peak - a
    100-point ghost next to a 10-point real series should visibly shrink the
    real line rather than both filling the same height (views.render_bar's
    shared-scale discipline, restated for pixels)."""
    points = _points((10, 0), (10, 0))
    unshared_peak_render = charts.render_activity_chart(points)
    shared_peak_render = charts.render_activity_chart(
        points, previous_points=_points((1000, 0), (1000, 0))
    )
    assert unshared_peak_render != shared_peak_render


# ---------------------------------------------------------------------------
# Labels and the bounded cost
# ---------------------------------------------------------------------------
def _placed_labels(points):
    """Every label _draw_labels would actually draw, as (x, text)."""
    drawn = []

    class _Recorder:
        def textlength(self, text, font=None):
            return ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(text, font=font)

        def text(self, position, text, font=None, fill=None):
            drawn.append((position[0], text))

    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    charts._draw_labels(_Recorder(), points, geometry, charts._load_label_font())
    return sorted(drawn)


def test_thirty_day_labels_never_overlap():
    """The card's window is exactly 30 days: weekly ticks at 0/7/14/21/28
    with the last point at 29, which drew two labels a few pixels apart and
    overprinted them into an unreadable smear. The end of the window wins;
    the colliding tick is dropped."""
    points = _points(*[(10 + index, 1) for index in range(30)])
    placed = _placed_labels(points)

    assert placed, "a 30-day chart must carry day labels"
    assert placed[-1][1] == points[-1].day.strftime("%m-%d")
    font = charts._load_label_font()
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for (x_left, left_label), (x_right, _right) in zip(placed, placed[1:]):
        width = measure.textlength(left_label, font=font)
        assert x_left + width <= x_right, f"{left_label} overlaps its neighbour"


def test_a_seven_day_window_labels_both_ends():
    points = _points(*[(10, 0)] * 7)
    assert len(_placed_labels(points)) == 2


def test_more_points_than_the_ceiling_keeps_the_most_recent_ones():
    """A caller that hands over an absurd window degrades into the most
    RECENT MAX_POINTS days instead of a multi-second Pillow job holding a
    slot of the bot-wide image semaphore."""
    huge = _points(*[(index, 0) for index in range(charts.MAX_POINTS + 50)])
    tail = huge[-charts.MAX_POINTS:]

    assert charts.render_activity_chart(huge) == charts.render_activity_chart(tail)


def test_the_ceiling_slices_the_ghost_the_same_way():
    """Both series are cut with the same slice, so the ghost stays
    day-aligned with the line it is compared against."""
    size = charts.MAX_POINTS + 10
    points = _points(*[(index, 0) for index in range(size)])
    ghost = _points(*[(size - index, 0) for index in range(size)])

    assert charts.render_activity_chart(
        points, previous_points=ghost
    ) == charts.render_activity_chart(
        points[-charts.MAX_POINTS:], previous_points=ghost[-charts.MAX_POINTS:]
    )


def test_the_ceiling_is_applied_after_the_ghost_length_check():
    """A mismatched ghost is a CALLER BUG and stays loud, even when both
    series are long enough to be trimmed to the same size."""
    points = _points(*[(1, 0)] * (charts.MAX_POINTS + 5))
    with pytest.raises(ValueError):
        charts.render_activity_chart(
            points, previous_points=_points(*[(1, 0)] * (charts.MAX_POINTS + 4))
        )


# ---------------------------------------------------------------------------
# The peak indicator: the y axis reduced to its one defining number
# ---------------------------------------------------------------------------
def _top_margin_colors(image, geometry, x_stop=None):
    """Every colour in the strip ABOVE the top gridline (y < top), where the
    peak text - and nothing else - is allowed to live."""
    return {
        image.getpixel((x, y))
        for x in range(0, x_stop if x_stop is not None else image.width)
        for y in range(0, geometry["top"])
    }


def test_a_known_series_writes_its_peak_above_the_top_gridline():
    points = _points((1200, 1), (3400, -1), (2600, 0))
    image = _decode(charts.render_activity_chart(points))
    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    assert charts.AXIS_TEXT_COLOR in _top_margin_colors(image, geometry)


def test_an_all_hole_chart_writes_no_peak_text():
    """No known point anywhere means no known scale: the honest answer is
    silence, not an invented '0'. The whole top margin stays untouched
    background."""
    points = _points((None, None), (None, None), (None, None))
    image = _decode(charts.render_activity_chart(points))
    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    assert _top_margin_colors(image, geometry) == {charts.BG_COLOR}


def test_the_peak_label_uses_thousands_separators():
    """The text is format(peak, ',') - digits and ',' only, never a
    translatable word (a PNG is one render for every viewer's locale)."""
    drawn = []

    class _Recorder:
        def textbbox(self, position, text, font=None):
            return (0, 3, 40, 14)

        def rectangle(self, box, fill=None):
            raise AssertionError("no backing rect needed when the text fits")

        def text(self, position, text, font=None, fill=None):
            drawn.append((position, text, fill))

    geometry = charts._plot_geometry(charts.CHART_WIDTH, charts.CHART_HEIGHT)
    charts._draw_peak(_Recorder(), geometry, 12345, charts._load_label_font())

    assert len(drawn) == 1
    (x, y), text, fill = drawn[0]
    assert text == "12,345"
    assert fill == charts.AXIS_TEXT_COLOR
    assert x == geometry["left"]
    # Sits fully above the top gridline: the peak value maps exactly onto
    # that gridline, so this is what keeps the text off the line at x=0.
    assert y + 14 < geometry["top"]


# ---------------------------------------------------------------------------
# Geometry helpers: pure, total
# ---------------------------------------------------------------------------
def test_point_x_centres_a_single_point():
    assert charts._point_x(0, 1, 0, 100) == 50.0


def test_point_x_spans_the_full_width_for_two_points():
    assert charts._point_x(0, 2, 0, 100) == 0.0
    assert charts._point_x(1, 2, 0, 100) == 100.0


def test_column_span_covers_the_whole_plot_for_a_single_point():
    assert charts._column_span(0, 1, 0, 100) == (0, 100)


def test_known_extent_ignores_holes_and_returns_none_when_nothing_is_known():
    assert charts._known_extent([10, None, 5]) == 10
    assert charts._known_extent([None, None]) is None
    assert charts._known_extent([None], None) is None
    assert charts._known_extent([1, 2], [None, 30]) == 30

"""Execute a draw-list (render.py) with cairo. pycairo is imported on first use, so the pure modules
and their tests never need it."""

from __future__ import annotations

import math

from .render import Arc, DrawList, Line, Paint, Rect, Text


def surface_for(pixels: object, width: int, height: int):
    """A cairo ARGB32 (premultiplied, = wl_shm ARGB8888) surface over writable `pixels`."""
    import cairo

    return cairo.ImageSurface.create_for_data(pixels, cairo.FORMAT_ARGB32, width, height,
                                              width * 4)


def paint(draw_list: DrawList, surface) -> None:
    import cairo

    context = cairo.Context(surface)
    for op in draw_list:
        if isinstance(op, Paint):
            context.set_operator(cairo.OPERATOR_SOURCE if op.source else cairo.OPERATOR_OVER)
            context.set_source_rgba(*op.rgba)
            context.paint()
            context.set_operator(cairo.OPERATOR_OVER)
        elif isinstance(op, Rect):
            context.set_source_rgba(*op.rgba)
            context.rectangle(op.x, op.y, op.width, op.height)
            context.fill()
        elif isinstance(op, Line):
            context.set_source_rgba(*op.rgba)
            context.set_line_width(op.width)
            first, *rest = op.points
            context.move_to(*first)
            for point in rest:
                context.line_to(*point)
            if op.closed:
                context.close_path()
            context.stroke()
        elif isinstance(op, Arc):
            context.set_source_rgba(*op.rgba)
            context.set_line_width(op.width)
            context.new_path()
            context.arc(op.x, op.y, op.radius, 0.0, 2 * math.pi)
            context.stroke()
        elif isinstance(op, Text):
            context.set_source_rgba(*op.rgba)
            if op.face is not None:
                context.select_font_face(op.face, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            context.set_font_size(op.size)
            context.new_path()
            context.move_to(op.x, op.y)
            context.show_text(op.text)
        else:
            raise TypeError(f"draw_op {type(op).__name__}")
    surface.flush()

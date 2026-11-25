"""What the everyday actions read of each Frame (decision 0036, slice 3), over PostgreSQL in the
caller's transaction: central/everyday/ports.py `Showing`, composed from each owner's own read
(Coordination's newest offers, the Runtime's Intents, shown history, the skips and the Never show
list). It judges nothing: Frame status adds its judgements (central/frame_status/policy.py
`everyday_facts`) and central/everyday/policy.py decides.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from central.everyday.model import AskedAction, CoverCause
from central.everyday.policy import asked_until
from central.everyday.ports import AskFact, LayerOrigin, Showing
from central.infra.never_show_store import PgNeverShow
from central.infra.pause_store import PgPauses
from central.infra.shown_history_store import PgShownHistory
from central.infra.skip_store import PgSkips
from central.planner import assignment_id
from central.runtime import Runtime, Scene
from contracts.models import Layer

_ASKED = {CoverCause.NEXT: AskedAction.SKIP, CoverCause.PREVIOUS: AskedAction.PREVIOUS,
          CoverCause.NEVER_SHOW: AskedAction.NEVER_SHOW}


def layer_origins(runtime: Runtime, layers: Iterable[Layer], now: float) -> dict[str, LayerOrigin]:
    """What made each offered layer: the Runtime Intent whose assignment id it is, over the
    timeline from `now` to the last layer's drawn end (a layer no Intent makes now has none, an
    action layer among them, so an hour's hold never stretches the timeline read)."""
    layers = [layer for layer in layers if not layer.action]   # a cover or a hold is no Intent's
    wanted = {layer.assignment_id for layer in layers}
    ends = [layer.drawn_end for layer in layers]
    if not wanted or max(ends) <= now:
        return {}
    scenes = runtime.export_state()["scenes"]
    found: dict[str, LayerOrigin] = {}
    for view in runtime.timeline(now, max(ends) + 1).views:
        for intent in view.contributions:
            identity = assignment_id(intent)
            if identity in wanted and identity not in found:
                stored = scenes.get(intent.scene_id)
                transition = 0.0 if stored is None else Scene.model_validate(stored).transition.seconds
                found[identity] = LayerOrigin(scene_id=intent.scene_id, hand_placed=bool(intent.asset_refs),
                                              ending=intent.phase == "outro", transition=transition)
    return found


def showing_in(conn: Any, now: float, frame_ids: Iterable[str], *, runtime: Runtime,
               layers: Mapping[str, tuple[Layer, ...]], shown: PgShownHistory | None = None,
               skips: PgSkips | None = None) -> dict[str, Showing]:
    """Each Frame's `Showing` at Central's `now`: its offered layers (with each cover built whole
    that is decided and not offered yet) and what made them, its covers newest first with when
    each was reported presented, its shown history, the Never show list, and its newest asked
    action that moved it with when its cover was reported presented."""
    shown, skips = shown or PgShownHistory(), skips or PgSkips()
    frame_ids = sorted(set(frame_ids))
    origins = layer_origins(runtime, [layer for frame in frame_ids for layer in layers.get(frame, ())], now)
    history = shown.frame_history_in(conn, frame_ids)
    recent = skips.recent_in(conn, frame_ids, now)
    listed = PgNeverShow.listed_in(conn)
    newest = {frame: next(((record, cover) for record, cover in rows if cover is not None), None)
              for frame, rows in recent.items()}
    pauses = {pause.frame_id: pause for pause in PgPauses.live_in(conn, now, frame_ids)}
    covers = {frame: tuple(cover for _, cover in rows if cover is not None) for frame, rows in recent.items()}
    presented = shown.presented_in(conn, [cover.cover_id for found in covers.values() for cover in found]
                                   + [pause.held.assignment_id for pause in pauses.values()])
    answer = {}
    for frame in frame_ids:
        offered = layers.get(frame, ())
        known = {layer.assignment_id for layer in offered}
        # A cover decided and not offered yet (the next planning pass offers it) that is built
        # whole is part of what the Frame will show, so a second press steps from it.
        on_frame = offered + tuple(cover.layer for cover in covers.get(frame, ())
                                   if cover.layer is not None and cover.cover_id not in known
                                   and cover.end > now)
        asked = None
        if (found := newest.get(frame)) is not None:
            record, cover = found
            asked = AskFact(action=_ASKED[cover.cause], request_id=record.request_id, at=cover.start,
                            until=asked_until(cover.start, cover.end),
                            presented_at=presented.get(cover.cover_id))
        if (pause := pauses.get(frame)) is not None and (asked is None or pause.started_at >= asked.at):
            # A pause is acknowledged as a cover is: once its hold was reported presented.
            asked = AskFact(action=AskedAction.PAUSE, request_id=pause.pause_id, at=pause.started_at,
                            until=asked_until(pause.started_at, pause.held.drawn_end),
                            presented_at=presented.get(pause.held.assignment_id))
        answer[frame] = Showing(
            layers=on_frame, origins={layer.assignment_id: origins[layer.assignment_id]
                                      for layer in on_frame if layer.assignment_id in origins},
            recent=covers.get(frame, ()), history=history.get(frame, ()), listed=listed, asked=asked,
            presented={cover.cover_id: presented[cover.cover_id] for cover in covers.get(frame, ())
                       if cover.cover_id in presented})
    return answer


__all__ = ["layer_origins", "showing_in"]

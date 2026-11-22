"""A Frame's status (decision 0036): the six status words, composed in Central from facts their
owners already judge. Pure; it composes and never judges.

- `model.py`: the vocabulary (`StatusWord`, `FrameStatus`, `Problem`, `Fix`, `Because`,
  `HouseStatus`, the closed codes).
- `policy.py`: the decision table, the cause table, the house word.
- `ports.py`: what each input's owner answers, one port per input, and `FrameStatusLog`.
- `views.py`: `HomeView` and the Frame page's shapes.

I/O: central/infra/frame_status_store.py; routes: central/home_routes.py.
"""

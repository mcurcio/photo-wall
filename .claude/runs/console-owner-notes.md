# Console: horizontal fleet IA — owner notes

## 2026-10-08 (verbatim)
"I have another agent session working on the node runtime and hardware tests. I would rather that you focus on UI improvements. I still don't like the way that the node stack is represented in the Central UI. it's too "vertical" where every part of the pi stack is all on one screen, where I want the UI to present a more "horizontal" structure where the UI is organized by the conceptual layers/domains and each includes all of the relevant hardware."

Note: reverses pass-1 "Q1 = A, one home per box" (docs/operator-console-ddd.md:3, :198, §9, §48, §50, §52; fleetRoutes.jsx:10). Owner's word outranks the doc; the doc gets rewritten when the design lands.

## 2026-10-09 — answers on https://claude.ai/artifact/5FY46D9hBWLyV9UbQKxHyC (verbatim)
- Q1 (five layers?): no choice. "I think software and apps go together. Hardware and connection go together. Screens is related to show runs. And there probably needs to be other screens for show creation."
- Q2 (picking one Pi): focus filter. "There are inevitably some per-pi specific detail pages as you drill down"
- Q3 (new Pis): inside Connection.

## 2026-10-09 (chat, verbatim)
"Consider the feature domains. Frames are a calibration concern in one flow AND a runtime surface in another perspective"

## 2026-10-09 — answers on https://claude.ai/artifact/Dx6gHA8T31WDmAGccyg1TY
- Q1 start page: Screens. Q2 show creation pages: defer to own design pass. Q3 binding: only from the Frame on the Wall. No notes.

## 2026-10-09 (chat, verbatim)
"I answered the questions you posed, but I also want to note that this design is just a starting point. As new capabilities are added to the hardware and to Central, we will need to constantly re-evaluate how pieces are laid out to maximize the user experience.

To that end, I would encourage focusing on a design language and building a catalog of react primitives rather than hard coding specific pages. It might be good to find a strong design library (tailwind? Bootstrap?) and build out the design system."

## 2026-10-09 — answers on https://claude.ai/artifact/B9GbkgD3d66wsh5iiwzwjh
- Q1 kit: Tailwind + shadcn/ui. Q2 look: keep. Note (verbatim): "I want the look-and-feel to feel like it's related to immich"

"""The render redesign's tracer bench on the test Pi (run ledger .claude/runs/render-planes/
s1-page.md, bead B6): one Frame on the 4K screen dissolving between photos, dipping to black,
dissolving from a photo into a 1080p30 HEVC video with the look, then the screen turned 90
degrees, and a soak; it stands in for Central and for Display Host's controller, drives the
Player's new path (player/plane_renderer.py) and records what the kernel and the display
controller did. Never shipped (scripts/release_plan.py); run by an agent on the Pi as root.

Modules: `timeline` (pure: the tracer's plan and what each moment must show), `controller` (the
shell's control socket: the grant and the turn), `kms_state` (pure: the kernel's plane state),
`registers` (the pixel valve's sticky glitch bits), `evidence` (pure: the verdict), `__main__`.
"""

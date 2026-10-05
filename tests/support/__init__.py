"""Shared test infrastructure for tests that drive real processes and real wire formats.

`github_release`: the GitHub Releases wire (release list, `manifest.json`, the real base tarball)
and a fake origin HTTP server that counts downloads. `release_build`: one release build's outputs
(the base bundle, both `.deb`s, the image digests) as the packager and the seal receive them.
`workers`: the `media.worker` process environment, live-worker counting and `wait_for`.
`packet_pair`: the one AF_UNIX packet pair standing in for the node's SOCK_SEQPACKET sockets
(SEQPACKET on Linux, DGRAM on macOS: boundaries everywhere, EOF on Linux only). Imported
as `support.<module>` (the `fakes` convention: `tests/` is on `sys.path`).
"""

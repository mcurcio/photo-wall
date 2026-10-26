"""Shared test infrastructure for tests that drive real processes and real wire formats.

`github_release`: the GitHub Releases wire (release list, the real base tarball, an environment)
and a fake origin HTTP server that counts downloads. `release_build`: one release build's outputs
(the base bundle, both `.deb`s, the image digests) as the packager and the seal receive them.
`workers`: the `media.worker` process environment, live-worker counting and `wait_for`.
`subject_model`: the pinned subject model (required: unset PHOTO_WALL_SUBJECT_MODEL fails the
test) and the stand-in maps.
`packet_pair`: the one AF_UNIX packet pair standing in for the node's SOCK_SEQPACKET sockets
(SEQPACKET on Linux, DGRAM on macOS: boundaries everywhere, EOF on Linux only). Imported
as `support.<module>` (the `fakes` convention: `tests/` is on `sys.path`).
"""

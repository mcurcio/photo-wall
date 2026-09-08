# OS baseline publication failure and shared dependency checks

## Observed failure

The initial retained-base run at application commit `44c6790a5c95229c5b6d5e80b00da6a697278159`
([run 34278343284](https://github.com/mcurcio/photo-wall/actions/runs/34278343284))
successfully prepared and published its builder container. Native Ubuntu package
installation completed, but root archive validation raised
`archive_private_material` before the OS base could be published.

Its public diagnostic artifact, `photo-wall-arm64-build-log-c2d1057f4f2708463dd8481220a9051c2c589caa`,
has ZIP SHA-256 `ef697bb218d6d9575f7c0f976d297a82008406b937856ad56c7eccdd527708b7`.
The failure is after APT acquisition, unlike the earlier snapshot HTTP 503 errors.

The follow-up at commit `20b084187e6b87a74800e3a4996cbe92fccbb8c7`
([run 34281103160](https://github.com/mcurcio/photo-wall/actions/runs/34281103160))
reused the builder, then rejected the missing OS definition
`9caae97f9343bf0aa06af3e117b8fd9e108be13135f2047315f1f9b5f26ce39c`.
The definition was unchanged, so this run correctly did not start another APT
preparation. Repeating that follow-up alone cannot repair the missing artifact.

## Reproduction and correction

A read-only inspection of the retained local Ubuntu `base-root` found one file
matching the PEM rejection condition:
`usr/lib/python3/dist-packages/twisted/test/key.pem.no_trailing_newline`.
It matches the installed `python3-twisted` 24.3.0-1ubuntu0.1 package checksum;
its SHA-256 is `380626aa2b5e4a799153f2c8faf6f38f080765fe0b2ab59c7865bf6167f26604`.
This is packaged public test data. No key contents were printed or added here.
The original hosted error did not identify a path; the local root establishes
the reproducible fixture, rather than claiming a path from the hosted log.

Baseline sanitation removes only that exact path with those exact bytes.
Changed contents and unsafe links fail. The sanitized fixture path and hash
are retained in `os-sanitization.json`; archive admission continues to reject
other private material. Rejections now identify the escaped relative path
without including file contents. This changes the OS definition and therefore
permits a new baseline preparation while reusing the published builder.

The correction passed 43 Linux archive/sanitization tests with networking
disabled (including GNU tar metadata checks). A second diagnostic copied the
historical root, removed its old `etc/photo-wall`, `var/lib/photo-wall`,
`opt/photo-wall`, and `usr/share/photo-wall` directories from that disposable
copy, then applied the exact-fixture sanitation and full strict archive scan.
The 1,956,618,240-byte archive passed; SHA-256
`9e1a993d42c065e4847a2be81fd55fb4ff40041ea1cfe69a79874ef41cc394e6`.
This adjusted historical-root check does not publish or qualify a new OS base.
The retained original volume was mounted read-only.

## Shared dependency validation

Both [MVP checks](https://github.com/mcurcio/photo-wall/actions/runs/34281103165)
and [software E2E](https://github.com/mcurcio/photo-wall/actions/runs/34281103301)
passed at `20b0841`. These runs published the AMD64 and ARM64 media OS definitions
and qualified their application consumers, including hosted native conversion,
operator browser checks using the published Playwright image, and the full
software scenario. They do not qualify the Pi appliance.

Before the narrow fixture correction, the local PostgreSQL suite passed
1,475 tests with 24 explicit platform or opt-in skips. The local retained-media
application build skipped the native APT stage; 54 conversion tests passed and
one five-second process-start deadline failed, including on a focused retry.
The corresponding hosted Linux-media job passed. That local timing failure is
retained as a limitation rather than suppressed or reported as a pass.

Updated OS archive checks and hosted baseline publication are separate gates;
no physical Pi, PXE, HDMI, or final-appliance success is established by this note.

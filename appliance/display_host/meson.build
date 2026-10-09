project('photo-wall-display', 'c', version: '0.1.0',
  default_options: ['c_std=c11', 'warning_level=3', 'werror=true'])
# libweston has no stable cross-major ABI. The deployed package lock must pin
# the exact Debian build in addition to this enforced ABI major.
# Built by debhelper's Meson build system (debian/rules, decision 0019) into two binary packages,
# each owning /usr/lib/photo-wall/<package without photo-wall->/: photo-wall-node-display (the
# shell, the diagnostic client and its overlay) and photo-wall-frame-client (the client library).
# Under prefix/lib, never the multiarch libdir dh passes: these are private directories.
private_dir = get_option('prefix') / 'lib' / 'photo-wall'
display_dir = private_dir / 'node-display'
weston = dependency('libweston-14', version: '>=14.0.0', include_type: 'system')
# Weston 14 folds the desktop shell API into libweston-14.
wayland = dependency('wayland-server', version: '>=1.22')
jansson = dependency('jansson')
scanner = find_program('wayland-scanner')
xml = files('native/photo-wall-frame-v1.xml')
server_header = custom_target('frame-server-header', input: xml,
  output: 'photo-wall-frame-server.h',
  command: [scanner, 'server-header', '@INPUT@', '@OUTPUT@'])
protocol = custom_target('frame-code', input: xml,
  output: 'photo-wall-frame-protocol.c',
  command: [scanner, 'private-code', '@INPUT@', '@OUTPUT@'])
shared_module('photo-wall-shell', 'native/shell.c', server_header, protocol,
  dependencies: [weston, wayland, jansson], name_prefix: '',
  install: true, install_dir: display_dir)
client_header = custom_target('frame-client-header', input: xml,
  output: 'photo-wall-frame-client.h',
  command: [scanner, 'client-header', '@INPUT@', '@OUTPUT@'])
wayland_client = dependency('wayland-client', version: '>=1.22')
wayland_protocols = dependency('wayland-protocols')
presentation_xml = wayland_protocols.get_variable(pkgconfig: 'pkgdatadir') / 'stable/presentation-time/presentation-time.xml'
presentation_header = custom_target('presentation-client-header', input: presentation_xml,
  output: 'presentation-time-client.h',
  command: [scanner, 'client-header', '@INPUT@', '@OUTPUT@'])
presentation_code = custom_target('presentation-code', input: presentation_xml,
  output: 'presentation-time-protocol.c',
  command: [scanner, 'private-code', '@INPUT@', '@OUTPUT@'])
# The private overlay client is Python (overlay/), spawned by the shell at
# display_dir/diagnostic-client. Debian's interpreter by absolute path: a builder image's PATH
# python3 cannot see Debian's pywayland. Its bindings are generated here.
python = find_program('/usr/bin/python3')
core_xml = dependency('wayland-scanner', native: true).get_variable(pkgconfig: 'pkgdatadir') / 'wayland.xml'
custom_target('overlay-protocol', input: [core_xml, presentation_xml, xml], output: 'protocol',
  command: [python, '-m', 'pywayland.scanner', '-o', '@OUTPUT@', '-i', '@INPUT@'],
  install: true, install_dir: display_dir / 'overlay')
install_data('overlay/__init__.py', 'overlay/instruction.py', 'overlay/render.py',
  'overlay/paint.py', 'overlay/health.py', 'overlay/client.py', install_dir: display_dir / 'overlay')
install_data('overlay/diagnostic-client', install_dir: display_dir, install_mode: 'rwxr-xr-x')
# The client library the Player loads in the app root (photo-wall-frame-client).
shared_library('photo-wall-frame-client', 'native/frame-client.c', client_header,
  protocol, presentation_header, presentation_code, dependencies: [wayland_client],
  install: true, install_dir: private_dir / 'frame-client')

project('photo-wall-display', 'c', version: '0.1.0',
  default_options: ['c_std=c11', 'warning_level=3', 'werror=true'])
# libweston has no stable cross-major ABI. The deployed package lock must pin
# the exact Debian build in addition to this enforced ABI major.
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
  install: true, install_dir: get_option('libdir') / 'photo-wall-display')
client_header = custom_target('frame-client-header', input: xml,
  output: 'photo-wall-frame-client.h',
  command: [scanner, 'client-header', '@INPUT@', '@OUTPUT@'])
wayland_client = dependency('wayland-client', version: '>=1.22')
cairo = dependency('cairo')
wayland_protocols = dependency('wayland-protocols')
presentation_xml = wayland_protocols.get_variable(pkgconfig: 'pkgdatadir') / 'stable/presentation-time/presentation-time.xml'
presentation_header = custom_target('presentation-client-header', input: presentation_xml,
  output: 'presentation-time-client.h',
  command: [scanner, 'client-header', '@INPUT@', '@OUTPUT@'])
presentation_code = custom_target('presentation-code', input: presentation_xml,
  output: 'presentation-time-protocol.c',
  command: [scanner, 'private-code', '@INPUT@', '@OUTPUT@'])
executable('diagnostic-client', 'native/diagnostic-client.c', client_header, protocol,
  presentation_header, presentation_code, dependencies: [wayland_client, cairo, jansson],
  install: true, install_dir: get_option('libdir') / 'photo-wall-display')
# This client artifact belongs in the sealed Player .deb, never in its host
# import fallback path. The native build emits it for the app package builder.
shared_library('photo-wall-frame-client', 'native/frame-client.c', client_header,
  protocol, presentation_header, presentation_code, dependencies: [wayland_client],
  install: true, install_dir: get_option('libdir') / 'photo-wall-client')

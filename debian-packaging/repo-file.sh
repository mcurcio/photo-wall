#!/bin/sh
# The .deb a local repo holds for one package (decision 0019): the path of REPO's Packages index
# `Filename:` for PACKAGE, joined to REPO. The one shell home of that lookup; the release writer
# reads the same index in Python (scripts/node_release_writer.py).
#
#   debian-packaging/repo-file.sh REPO PACKAGE
#
# REPO is a debian-packaging/build-repo.sh output (or a copy of one), PACKAGE a binary package it
# holds once. Fails, naming both, when the index holds no such package or holds it more than once.
set -eu

[ $# -eq 2 ] || { echo "usage: $0 REPO PACKAGE" >&2; exit 2; }
files=$(sed -n "/^Package: $2\$/,/^\$/s/^Filename: //p" "$1/Packages")
case "$files" in
	'') echo "repo-file: $1 holds no $2" >&2; exit 1 ;;
	*'
'*) echo "repo-file: $1 holds $2 more than once" >&2; exit 1 ;;
esac
echo "$1/$files"

#!/bin/bash

# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT

# Writes ed25519-gpg.asc: a passphrase-protected ed25519 signing key, as GnuPG 2.3 or later
# exports it, whose secret Sequoia refuses as a malformed MPI (its leading zero octets kept). Keys
# are generated until one is refused, about one in two. Run it from this directory, with gpg and
# the workspace's environment (uv run).
set -euo pipefail

home=$(mktemp -d)
trap 'gpgconf --homedir "$home" --kill gpg-agent; rm -rf "$home"' EXIT
gpg_() { gpg --homedir "$home" --batch --quiet --pinentry-mode loopback --passphrase test-passphrase "$@"; }

for n in $(seq 1 64)
do
	uid="ed25519 test $n <ed25519@example.invalid>"
	gpg_ --quick-gen-key "$uid" ed25519 sign never
	gpg_ --armor --export-secret-keys "=$uid" > candidate.asc
	if ! python3 -c '
import sys, pysequoia
key = pysequoia.Tsk.from_bytes(open("candidate.asc", "rb").read())
try:
    key.signer("test-passphrase")
except Exception:
    sys.exit(1)
'
	then
		mv candidate.asc ed25519-gpg.asc
		exit 0
	fi
done
rm -f candidate.asc
echo "no refused key in 64 attempts" >&2
exit 1

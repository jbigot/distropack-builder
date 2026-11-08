# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT
# shellcheck shell=bash

# Sourced by the actions' steps.

# distribution_options PATTERNS
# Sets the array options to one `-D <pattern>` per pattern of PATTERNS, which separates them by
# spaces, tabs or line breaks; a pattern is never expanded as a file name.
distribution_options() {
    local words=()
    read -r -d '' -a words <<< "$1" || true
    options=()
    local word
    for word in "${words[@]}"; do
        options+=(-D "$word")
    done
}

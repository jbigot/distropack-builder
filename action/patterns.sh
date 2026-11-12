# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT
# shellcheck shell=bash

# Sourced by the actions' steps: the command lines their inputs make.

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

# snapshot_options SNAPSHOTS
# Appends to the array options one `--snapshot <package>=<commit>` per word of SNAPSHOTS, which
# separates them by spaces, tabs or line breaks.
snapshot_options() {
    local words=()
    read -r -d '' -a words <<< "$1" || true
    local word
    for word in "${words[@]}"; do
        options+=(--snapshot "$word")
    done
}

# mode_command MODE
# Sets the array command to the command line of the action's MODE input; fails on another value.
# shellcheck disable=SC2034  # command is read by the step sourcing this file
mode_command() {
    case "$1" in
        run) command=(run) ;;
        no-publish) command=(run --no-publish) ;;
        publish-only) command=(run --publish-only) ;;
        fetch) command=(fetch) ;;
        *)
            echo "::error::the input mode is run, no-publish, publish-only or fetch, not $1"
            return 1
            ;;
    esac
}

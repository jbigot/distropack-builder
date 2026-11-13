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

# extra_options SPEC BASE
# Reads SPEC, `<path>=<tag>` per line (blanks around them ignored), the trees a site copies as they
# are: sets the array extra_tags to each tag once, in the order first named, and the array options
# to one `--extra <path>=BASE/<tag>/<path>` per line, where BASE/<tag> is where the tag is pulled;
# fails on a line without `=`, or whose tag is not one (letters, digits, `_`, `.`, `-`).
# shellcheck disable=SC2034  # extra_tags is read by the step sourcing this file
extra_options() {
    extra_tags=()
    options=()
    local line path tag known existing
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line#"${line%%[![:space:]]*}"}
        line=${line%"${line##*[![:space:]]}"}
        [ -n "$line" ] || continue
        path=${line%%=*}
        tag=${line#*=}
        if [ "$path" = "$line" ] || [ -z "$path" ]; then
            echo "::error::site-extra: expected <path>=<tag>, not $line"
            return 1
        fi
        if ! [[ $tag =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]*$ ]]; then
            echo "::error::site-extra: $tag is not a tag, in $line"
            return 1
        fi
        known=false
        for existing in "${extra_tags[@]}"; do
            if [ "$existing" = "$tag" ]; then known=true; fi
        done
        if [ "$known" = false ]; then extra_tags+=("$tag"); fi
        options+=(--extra "$path=$2/$tag/$path")
    done <<< "$1"
}

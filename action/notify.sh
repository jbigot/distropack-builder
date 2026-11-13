# SPDX-FileCopyrightText: 2026 Julien Bigot <julien@julien-bigot.fr>
#
# SPDX-License-Identifier: MIT
# shellcheck shell=bash

# Sourced by the notify action, which sends the request, and by the reusable workflow, which reads
# it: the `repository_dispatch` of type distropack-build, {channel, ref, commit, packages}.

# dispatch_body CHANNEL REF COMMIT PACKAGES
# Prints the JSON body of the request; PACKAGES separates the package names by spaces, tabs or
# line breaks, and none means every package of the channel.
dispatch_body() {
    local words=()
    read -r -d '' -a words <<< "$4" || true
    jq -cn --arg channel "$1" --arg ref "$2" --arg commit "$3" '{
        event_type: "distropack-build",
        client_payload: {channel: $channel, ref: $ref, commit: $commit, packages: $ARGS.positional}
    }' --args "${words[@]}"
}

# payload_snapshots PAYLOAD CHANNELS CHANNEL
# Prints `<package>=<commit>` per package the client_payload PAYLOAD names, or per package of
# CHANNEL when it names none, CHANNELS being `distropack-build channels list --json`; fails when
# PAYLOAD is for another channel, its commit is not 40 lowercase hexadecimal digits, or it names a
# package CHANNEL does not build.
payload_snapshots() {
    local channel commit
    if ! channel=$(jq -er 'if type == "object" then .channel else error("not an object") end' <<< "$1" 2> /dev/null); then
        echo "::error::dispatch-payload: not a payload of the notify action: $1"
        return 1
    fi
    if [ "$channel" != "$3" ]; then
        echo "::error::dispatch-payload: for the channel $channel, not $3"
        return 1
    fi
    commit=$(jq -r '.commit // ""' <<< "$1")
    if ! [[ $commit =~ ^[0-9a-f]{40}$ ]]; then
        echo "::error::dispatch-payload: the commit is not a full commit hash: $commit"
        return 1
    fi
    jq -r --argjson channels "$2" --arg channel "$3" --arg commit "$commit" '
        ($channels[] | select(.name == $channel) | .packages) as $known
        | ((.packages // []) | if length == 0 then $known else . end) as $wanted
        | ($wanted - $known) as $unknown
        | if ($unknown | length) > 0
          then error("dispatch-payload: \($channel) builds no \($unknown | join(", "))")
          else $wanted[] | "\(.)=\($commit)" end
    ' <<< "$1"
}

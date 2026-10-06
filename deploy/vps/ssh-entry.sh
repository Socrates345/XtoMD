#!/usr/bin/env bash
# All the phone's SSH key may do on the VPS. In ~xmd/.ssh/authorized_keys, on one line:
#
#   command="bash /home/xmd/XtoMD/deploy/vps/ssh-entry.sh",restrict ssh-ed25519 AAAA... pixel
#
# sshd then runs this script whatever the phone asked for, and hands it the request in SSH_ORIGINAL_COMMAND:
#
#   ssh xmd-vps run 24h       make a brief now (brief.sh's arguments: 24h|since [MINUTES])
#   ssh xmd-vps pull          the briefs written in the last day, as a tar stream on stdout
#   ssh xmd-vps pull 7        ... in the last 7 days
#
# Anything else is refused, so a lost phone can start a brief and read briefs, and that is all. The request is
# never handed to a shell: it is split into words and each word is checked.
#
# A brief asked for here is finished even if the phone goes away in the middle: see `run` below.
set -euo pipefail

refuse() {
    echo "allowed: run 24h|since [MINUTES] | pull [DAYS]" >&2
    exit 2
}

. "$(dirname "${BASH_SOURCE[0]}")/../common.sh"
request="${SSH_ORIGINAL_COMMAND:-}"
plain='^[a-z0-9 ]+$'  # no quote, no separator, no second line: nothing a shell would read anything into
[[ "$request" =~ $plain ]] || refuse
read -r -a words <<< "$request"

case "${words[0]:-}" in
    run)
        [[ ${#words[@]} -ge 2 && ${#words[@]} -le 3 ]] || refuse
        [[ "${words[1]}" == 24h || "${words[1]}" == since ]] || refuse
        [[ ${#words[@]} -eq 2 || "${words[2]}" =~ ^[0-9]{1,3}$ ]] || refuse
        # The brief must outlive this connection: in the minutes it takes, a phone changes network or goes to
        # sleep. So it runs in a session of its own and writes to a file, and this side only shows that file as
        # it grows. A phone that goes away ends the showing, not the brief: `pull` finds it once it is made.
        digests="$(digests_dir)"
        cd "$repo"
        mkdir -p "$digests"
        find "$digests" -maxdepth 1 -type f -name '.brief-log.*' -mmin +180 -delete  # left by lost connections
        log="$(mktemp "$digests/.brief-log.XXXXXX")"
        setsid bash "$repo/deploy/brief.sh" "${words[@]:1}" > "$log" 2>&1 < /dev/null &
        job=$!
        tail --pid="$job" -n +1 -f "$log" 2> /dev/null || true  # ends with the brief, or with the connection
        code=0
        wait "$job" || code=$?
        rm -f "$log"
        exit "$code"
        ;;
    pull)
        [[ ${#words[@]} -le 2 ]] || refuse
        days="${words[1]:-1}"
        [[ "$days" =~ ^[0-9]{1,2}$ ]] || refuse
        digests="$(digests_dir)"
        cd "$repo"
        cd "$digests"
        find . -maxdepth 1 -type f -name '*-brief*.md' -mtime -"$days" -printf '%f\0' | tar -cf - --null -T -
        ;;
    *)
        refuse
        ;;
esac

#!/data/data/com.termux/files/usr/bin/bash
# On the phone (Termux): the gate between the briefs downloaded into the vault's 1_unverified and its 2_verified.
# The same checks as promote.ps1 on the laptop (isolated-workspace, "Obsidian vault bind-mounting"), for briefs
# the laptop never sees: on the phone, 1_unverified is excluded from Obsidian Sync, 2_verified is synced.
#
#   bash deploy/termux/verify.sh        scan 1_unverified, report, and on Enter move what was scanned
#
# Every file in 1_unverified is scanned: a .md file of at most 5 MB without any of the patterns below is clean,
# anything else is rejected. The report lists both, with the line and the pattern of every match. After Enter,
# clean files move to 2_verified and rejected ones to the quarantine folder, under the same relative path; a file
# whose name is taken there, or that changed since the report, stays in 1_unverified. Nothing is ever overwritten.
# A symbolic link anywhere in 1_unverified stops it before anything is scanned or moved.
#
# The three folders come from .env at the repo root (deploy/termux/env.example): XMD_BRIEF_DEST (1_unverified,
# where vps.sh pull puts the briefs), XMD_BRIEF_VERIFIED (2_verified) and XMD_BRIEF_REJECTED (the quarantine).
# 2_verified is never created here: it is part of the vault, and its absence means a setting is wrong.
#
# Exit codes: 0 done (or nothing to do, or no Enter: nothing moved), 1 a setting or folder is wrong, or a link
# was found.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ -f "$repo/.env" ]]; then
    set -a
    . "$repo/.env"
    set +a
fi

MAX_BYTES=$((5 * 1024 * 1024))
# case-insensitive LITERAL substrings, as in promote.ps1: several hold regex characters ("$="), so they are only
# ever given to grep -F
PATTERNS=('<%' 'dataviewjs' '$=' '<script' '<iframe' '<object' '<embed' 'javascript:' 'obsidian://' 'file:///')

for setting in XMD_BRIEF_DEST XMD_BRIEF_VERIFIED XMD_BRIEF_REJECTED; do
    if [[ -z "${!setting:-}" ]]; then
        echo "ERROR: $setting is not set: name the folder in $repo/.env (deploy/termux/env.example)"
        exit 1
    fi
done
unverified="${XMD_BRIEF_DEST%/}"
verified="${XMD_BRIEF_VERIFIED%/}"
rejected="${XMD_BRIEF_REJECTED%/}"

if [[ -L "$unverified" ]]; then
    echo "ABORT: 1_unverified itself is a symbolic link: $unverified"
    exit 1
fi
if [[ ! -d "$unverified" ]]; then
    echo "ERROR: 1_unverified does not exist: $unverified"
    exit 1
fi
if [[ ! -d "$verified" ]]; then
    echo "ERROR: 2_verified does not exist: $verified"
    echo "This script will not create vault folders: check XMD_BRIEF_VERIFIED in .env, or let Obsidian Sync bring it."
    exit 1
fi
odd="$(find "$unverified" -mindepth 1 ! -type f ! -type d -print -quit)"  # a link, a pipe, a device...
if [[ -n "$odd" ]]; then
    echo "ABORT: not a file or a folder (a symbolic link?): $odd"
    echo "Refusing to scan or move anything until this is investigated and removed."
    exit 1
fi

# --- scan: cheapest checks first, the content only of a .md file small enough
files=() status=() reasons=() sums=() details=()
while IFS= read -r -d '' file; do
    rel="${file#"$unverified"/}"
    name="${file##*/}"
    reason="" detail=""
    if [[ "${name,,}" != *.md ]]; then
        reason="not a .md file"
    elif (( $(stat -c %s -- "$file") > MAX_BYTES )); then
        reason="size $(stat -c %s -- "$file") bytes exceeds the 5 MB limit"
    elif [[ ! -r "$file" ]]; then
        reason="cannot be read"
    else
        count=0
        for pattern in "${PATTERNS[@]}"; do
            while IFS= read -r match; do
                [[ -n "$match" ]] || continue
                count=$((count + 1))
                detail+="         line ${match%%:*}: pattern '$pattern' -> ${match#*:}"$'\n'
            done < <(grep -a -F -i -n -e "$pattern" -- "$file" || true)
        done
        if (( count > 0 )); then
            reason="$count forbidden-pattern match(es)"
        fi
    fi
    files+=("$rel")
    status+=("$([[ -z "$reason" ]] && echo clean || echo rejected)")
    reasons+=("$reason")
    details+=("$detail")
    sums+=("$(sha256sum -- "$file" | cut -d' ' -f1)")
done < <(find "$unverified" -type f -print0 | sort -z)

echo
echo "=== Scan report: ${#files[@]} file(s) in 1_unverified ==="
if (( ${#files[@]} == 0 )); then
    echo "nothing to verify"
    exit 0
fi
clean=0
for i in "${!files[@]}"; do [[ "${status[$i]}" == clean ]] && clean=$((clean + 1)); done
echo
echo "-- Clean ($clean) --"
for i in "${!files[@]}"; do
    [[ "${status[$i]}" == clean ]] && echo "  OK   ${files[$i]}"
done
echo
echo "-- Rejected ($(( ${#files[@]} - clean ))) --"
for i in "${!files[@]}"; do
    if [[ "${status[$i]}" == rejected ]]; then
        echo "  FAIL ${files[$i]} - ${reasons[$i]}"
        printf '%s' "${details[$i]}"
    fi
done
echo

# --- move, once asked: a deliberate step, as promote.ps1's -Fix
if ! read -r -p "Press Enter to move the clean files to 2_verified and the rejected ones to quarantine (Ctrl+C: nothing moves) "; then
    echo
    echo "nothing moved"
    exit 0
fi

moved_clean=0 moved_rejected=0 attention=()
for i in "${!files[@]}"; do
    rel="${files[$i]}"
    source="$unverified/$rel"
    if [[ "${status[$i]}" == clean ]]; then root="$verified"; else root="$rejected"; fi
    target="$root/$rel"
    if [[ ! -f "$source" || -L "$source" ]] || [[ "$(sha256sum -- "$source" | cut -d' ' -f1)" != "${sums[$i]}" ]]; then
        attention+=("$rel  [changed or gone since the report]")
    elif [[ -e "$target" || -L "$target" ]]; then
        attention+=("$rel  [name already exists at destination]")
    elif mkdir -p -- "$(dirname -- "$target")" && mv -n -- "$source" "$target" && [[ ! -e "$source" ]]; then
        if [[ "${status[$i]}" == clean ]]; then moved_clean=$((moved_clean + 1)); else moved_rejected=$((moved_rejected + 1)); fi
    else
        attention+=("$rel  [move failed]")
    fi
done

echo
echo "=== Summary ==="
echo "  Moved to 2_verified:   $moved_clean"
echo "  Moved to quarantine:   $moved_rejected"
echo "  Left in 1_unverified:  ${#attention[@]}"
if (( ${#attention[@]} > 0 )); then
    echo
    echo "  NEEDS MANUAL ATTENTION (left behind in 1_unverified):"
    for line in "${attention[@]}"; do echo "    - $line"; done
fi
exit 0

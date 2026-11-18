#!/usr/bin/env bash
# Build, sign, notarize and staple HeyMa.app on the Mac, from big-chungus.
#
#   release.sh [--no-notarize] [--mac HOST] [--keep]
#
# Writes dist/HeyMa-<VERSION>-macos-universal.zip (stapled) and prints its size
# and sha256. Idempotent: every run uses a fresh remote work directory and
# replaces the local zip. It does not publish; see skills/heyma/references/shell.md
# "Mac app" for the GitHub release and the cask bump.
#
# Secrets: read with `op read` on this host and piped over ssh stdin to
# mac-sign-notarize.sh. They are never on an argv here, in a file, or in output.
# The signing identity must be in the Mac's login keychain; the keychain is
# unlocked in the same ssh session that signs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MAC="mac"
NOTARIZE=1
KEEP=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-notarize) NOTARIZE=0; shift ;;
        --mac) MAC="${2:?--mac needs a host}"; shift 2 ;;
        --keep) KEEP=1; shift ;;
        *) echo "usage: $0 [--no-notarize] [--mac HOST] [--keep]" >&2; exit 2 ;;
    esac
done
VERSION="$(tr -d '[:space:]' < "$HERE/VERSION")"
KEYCHAIN_PW_REF="op://DeLoSecrets/ghbrrlh7q3m2vdbltyxkbrd77y/password"
NOTARY_ITEM="op://DeLoSecrets/7edxjfddpg2e5vy5qjnjxuvoq4"
REMOTE="/tmp/heyma-mac-release-$VERSION"
DIST="$HERE/dist"

command -v op >/dev/null || { echo "op (1Password CLI) is required" >&2; exit 1; }
ssh -o BatchMode=yes "$MAC" true || { echo "cannot ssh to $MAC" >&2; exit 1; }

echo "==> ship source to $MAC:$REMOTE"
ssh "$MAC" "rm -rf '$REMOTE' && mkdir -p '$REMOTE/src'"
rsync -a --delete --exclude dist --exclude tests --exclude '*.zip' --exclude __pycache__ \
    "$HERE/" "$MAC:$REMOTE/src/"
cleanup() { [ "$KEEP" = 1 ] || ssh "$MAC" "rm -rf '$REMOTE'" >/dev/null 2>&1 || true; }
trap cleanup EXIT

if [ "$NOTARIZE" = 1 ]; then echo "==> sign and notarize on $MAC"; else echo "==> sign on $MAC (no notarization)"; fi
FLAG=""
[ "$NOTARIZE" = 1 ] || FLAG="--no-notarize"
{
    op read "$KEYCHAIN_PW_REF"
    if [ "$NOTARIZE" = 1 ]; then
        op read "$NOTARY_ITEM/key_id"
        op read "$NOTARY_ITEM/issuer_id"
        op read "$NOTARY_ITEM/AuthKey_5MRHBR4678.p8"
    fi
} | ssh "$MAC" "bash '$REMOTE/src/mac-sign-notarize.sh' '$REMOTE/out' $FLAG"

echo "==> fetch the zip"
mkdir -p "$DIST"
ZIP="HeyMa-$VERSION-macos-universal.zip"
scp -q "$MAC:$REMOTE/out/$ZIP" "$DIST/$ZIP"
echo "$DIST/$ZIP"
ls -l "$DIST/$ZIP" | awk '{print "bytes:", $5}'
sha256sum "$DIST/$ZIP"

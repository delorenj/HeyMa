#!/usr/bin/env bash
# Runs ON the Mac, started by release.sh over ssh. Builds, signs, notarizes,
# staples and validates HeyMa.app, then leaves the stapled zip in $1.
#
# stdin (read once, in this order, then closed): the login keychain password
# (one line), the App Store Connect API key id (one line), the issuer UUID (one
# line), and the .p8 key (the rest). None of it is echoed, logged or written
# anywhere but a 0600 temp file for the key that a trap removes. The keychain is
# unlocked in THIS process tree because headless codesign only works when the
# unlock and the signing share one ssh session.
#
# usage: mac-sign-notarize.sh OUT_DIR [--no-notarize]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${1:?usage: mac-sign-notarize.sh OUT_DIR [--no-notarize]}"
NOTARIZE=1
[ "${2:-}" = "--no-notarize" ] && NOTARIZE=0
VERSION="$(tr -d '[:space:]' < "$HERE/VERSION")"
IDENTITY="${HEYMA_SIGN_IDENTITY:-Developer ID Application: Jarad DeLorenzo (YJ7W7Z36FP)}"
TEAM="YJ7W7Z36FP"
KEYCHAIN="${HEYMA_KEYCHAIN:-$HOME/Library/Keychains/login.keychain-db}"

KEYDIR=""
cleanup() { [ -z "$KEYDIR" ] || rm -rf "$KEYDIR"; }
trap cleanup EXIT

IFS= read -r KC_PW || { echo "stdin ended before the keychain password" >&2; exit 2; }
if [ "$NOTARIZE" = 1 ]; then
    IFS= read -r KEY_ID || { echo "stdin ended before the API key id" >&2; exit 2; }
    IFS= read -r ISSUER || { echo "stdin ended before the API issuer" >&2; exit 2; }
    KEYDIR="$(umask 077 && mktemp -d "${TMPDIR:-/tmp}/heyma-notary.XXXXXX")"
    P8="$KEYDIR/AuthKey.p8"
    (umask 077 && cat > "$P8")
    chmod 600 "$P8"
    grep -q -- '-----BEGIN PRIVATE KEY-----' "$P8" || { echo "stdin held no .p8 private key" >&2; exit 2; }
fi
exec < /dev/null

echo "==> unlock keychain (same session as codesign)"
security unlock-keychain -p "$KC_PW" "$KEYCHAIN"
unset KC_PW
security find-identity -v -p codesigning "$KEYCHAIN" | grep -F "$IDENTITY" >/dev/null \
    || { echo "identity not found in the login keychain: $IDENTITY" >&2; exit 1; }

echo "==> build and sign"
BUILD="$(mktemp -d "${TMPDIR:-/tmp}/heyma-build.XXXXXX")"
trap 'cleanup; rm -rf "$BUILD"' EXIT
HEYMA_SIGN_IDENTITY="$IDENTITY" HEYMA_OUT="$BUILD" bash "$HERE/build-app.sh"
APP="$BUILD/HeyMa.app"

echo "==> verify signature"
codesign --verify --deep --strict --verbose=2 "$APP"
DETAILS="$(codesign -dvv "$APP" 2>&1)"
printf '%s\n' "$DETAILS" | grep -E '^(Authority|TeamIdentifier|Identifier|Timestamp|Runtime|CodeDirectory)' || true
printf '%s\n' "$DETAILS" | grep -q "TeamIdentifier=$TEAM" || { echo "wrong team identifier" >&2; exit 1; }
printf '%s\n' "$DETAILS" | grep -q 'flags=0x[0-9a-f]*(.*runtime' || { echo "hardened runtime flag missing" >&2; exit 1; }

mkdir -p "$OUT"
ZIP="$OUT/HeyMa-$VERSION-macos-universal.zip"
if [ "$NOTARIZE" = 0 ]; then
    ditto -c -k --keepParent "$APP" "$ZIP"
    echo "signed only (not notarized): $ZIP"
    exit 0
fi

echo "==> notarize"
SUBMIT_ZIP="$BUILD/submit.zip"
ditto -c -k --keepParent "$APP" "$SUBMIT_ZIP"
RESULT="$BUILD/notary.json"
set +e
xcrun notarytool submit "$SUBMIT_ZIP" --key "$P8" --key-id "$KEY_ID" --issuer "$ISSUER" \
    --wait --output-format json > "$RESULT"
RC=$?
set -e
SUB_ID="$(plutil -extract id raw -o - "$RESULT" 2>/dev/null || true)"
STATUS="$(plutil -extract status raw -o - "$RESULT" 2>/dev/null || true)"
echo "notarization submission id: ${SUB_ID:-unknown}"
echo "notarization status: ${STATUS:-unknown}"
if [ "$STATUS" != "Accepted" ] || [ "$RC" -ne 0 ]; then
    echo "notarization was not accepted; notarytool log follows (no retry)" >&2
    [ -z "$SUB_ID" ] || xcrun notarytool log "$SUB_ID" --key "$P8" --key-id "$KEY_ID" --issuer "$ISSUER" >&2 || true
    exit 1
fi

echo "==> staple and validate"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
spctl -a -vvv -t exec "$APP"

# The release asset is the STAPLED bundle (the submitted zip held no ticket).
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"
echo "stapled zip: $ZIP"

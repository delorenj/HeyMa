#!/usr/bin/env bash
# Build the native HeyMa.app (universal arm64+x86_64) and sign it. Runs ON a Mac
# with Xcode or the Command Line Tools. The app contains no Python runtime: it
# drives the installed Vinyl CLI. release.sh runs this on the Mac for you.
#
#   HEYMA_SIGN_IDENTITY  codesign identity (default: the Developer ID below).
#                        "-" signs ad hoc (local development only; Gatekeeper
#                        and notarization refuse it).
#   HEYMA_OUT            output directory (default: ./dist next to this script).
#
# Signing needs the login keychain unlocked in THIS shell session: over ssh that
# means `security unlock-keychain` and this script in the same session
# (release.sh arranges it; never put the keychain password in a file).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$HERE"
VERSION="$(tr -d '[:space:]' < "$HERE/VERSION")"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "VERSION must be x.y.z, got '$VERSION'" >&2; exit 1; }
IDENTITY="${HEYMA_SIGN_IDENTITY:-Developer ID Application: Jarad DeLorenzo (YJ7W7Z36FP)}"
[ "$(uname -s)" = Darwin ] || { echo "build-app.sh requires macOS and Xcode Command Line Tools" >&2; exit 1; }
for f in main.swift Info.plist heyma-logo.png; do
    [ -f "$SOURCE/$f" ] || { echo "missing native app source: $f" >&2; exit 1; }
done
command -v xcrun >/dev/null || { echo "xcrun is required (install Xcode Command Line Tools)" >&2; exit 1; }

OUT="${HEYMA_OUT:-$HERE/dist}"
APP="$OUT/HeyMa.app"
CONTENTS="$APP/Contents"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources" "$TMP/HeyMa.iconset"
for ARCH in arm64 x86_64; do
    xcrun swiftc -O -swift-version 5 -target "$ARCH-apple-macosx13.0" -sdk "$SDK" \
        -framework AppKit -framework Carbon -o "$TMP/HeyMa-$ARCH" "$SOURCE/main.swift"
done
xcrun lipo -create "$TMP/HeyMa-arm64" "$TMP/HeyMa-x86_64" -output "$CONTENTS/MacOS/HeyMa"
chmod 755 "$CONTENTS/MacOS/HeyMa"
cp "$SOURCE/Info.plist" "$CONTENTS/Info.plist"
cp "$SOURCE/heyma-logo.png" "$CONTENTS/Resources/heyma-logo.png"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$CONTENTS/Info.plist"
plutil -replace CFBundleVersion -string "$VERSION" "$CONTENTS/Info.plist"
plutil -replace CFBundleIconFile -string HeyMa "$CONTENTS/Info.plist"
plutil -lint "$CONTENTS/Info.plist"

# Render the supplied transparent HeyMa wordmark aspect-fit on a warm, dark
# rounded tile. Draw directly to a bitmap (no TIFF conversion or window focus).
cat > "$TMP/render-icon.swift" <<'SWIFT'
import AppKit

func render(_ size: Int, logo: NSImage, to path: String) throws {
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size,
                                        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                        isPlanar: false, colorSpaceName: .deviceRGB,
                                        bytesPerRow: 0, bitsPerPixel: 0),
          let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
        throw NSError(domain: "HeyMaIcon", code: 1)
    }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    defer { NSGraphicsContext.restoreGraphicsState() }
    let side = CGFloat(size)
    context.cgContext.clear(CGRect(x: 0, y: 0, width: side, height: side))
    let tile = NSRect(x: side * 0.045, y: side * 0.045, width: side * 0.91, height: side * 0.91)
    let shape = NSBezierPath(roundedRect: tile, xRadius: side * 0.23, yRadius: side * 0.23)
    NSColor(srgbRed: 0.17, green: 0.105, blue: 0.095, alpha: 1).setFill()
    shape.fill()

    // Leave a quiet margin so both the wordmark and its waveform fit at every
    // icon size without clipping or changing the supplied image's aspect ratio.
    let availableWidth = side * 0.74
    let availableHeight = side * 0.74
    let scale = min(availableWidth / logo.size.width, availableHeight / logo.size.height)
    let width = logo.size.width * scale
    let height = logo.size.height * scale
    logo.draw(in: NSRect(x: (side - width) / 2, y: (side - height) / 2,
                         width: width, height: height),
              from: NSRect(origin: .zero, size: logo.size), operation: .sourceOver, fraction: 1)
    context.flushGraphics()
    guard let png = bitmap.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "HeyMaIcon", code: 2)
    }
    try png.write(to: URL(fileURLWithPath: path))
}

guard CommandLine.arguments.count == 3,
      let logo = NSImage(contentsOfFile: CommandLine.arguments[2]),
      logo.size.width > 0, logo.size.height > 0 else {
    throw NSError(domain: "HeyMaIcon", code: 3)
}
let iconset = CommandLine.arguments[1]
for size in [16, 32, 128, 256, 512] {
    try render(size, logo: logo, to: "\(iconset)/icon_\(size)x\(size).png")
    try render(size * 2, logo: logo, to: "\(iconset)/icon_\(size)x\(size)@2x.png")
}
SWIFT
xcrun swiftc -swift-version 5 -target "$(uname -m)-apple-macosx13.0" -sdk "$SDK" \
    -framework AppKit -o "$TMP/render-icon" "$TMP/render-icon.swift"
"$TMP/render-icon" "$TMP/HeyMa.iconset" "$SOURCE/heyma-logo.png"
xcrun iconutil -c icns "$TMP/HeyMa.iconset" -o "$CONTENTS/Resources/HeyMa.icns"
# Hardened runtime plus a secure timestamp: both are required for notarization.
# No entitlements file: the app does not capture audio, load unsigned code or
# use JIT; it execs the Vinyl CLI and writes LaunchAgents, which hardened runtime
# does not restrict. Add one only if a future feature needs it.
if [ "$IDENTITY" = "-" ]; then
    codesign --force --sign - "$APP"
    echo "signed AD HOC (not distributable)"
else
    codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP"
fi
codesign --verify --deep --strict --verbose=2 "$APP"
echo "built $APP (version $VERSION, universal arm64+x86_64, macOS 13+)"

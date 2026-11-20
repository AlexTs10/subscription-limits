#!/bin/zsh
set -euo pipefail
project_dir="${0:A:h}"
bundle="$project_dir/build/Subscription Limits.app"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
xcrun swiftc -O -parse-as-library -target arm64-apple-macosx14.0 \
  -framework AppKit \
  "$project_dir/Sources/SubscriptionLimits.swift" \
  -o "$bundle/Contents/MacOS/SubscriptionLimits"
cp "$project_dir/Sources/limits_reader.py" "$bundle/Contents/Resources/limits_reader.py"
cat > "$bundle/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>SubscriptionLimits</string>
<key>CFBundleIdentifier</key><string>com.alextoska.subscriptionlimits</string>
<key>CFBundleName</key><string>Subscription Limits</string>
<key>CFBundleDisplayName</key><string>Subscription Limits</string>
<key>CFBundleVersion</key><string>4</string>
<key>CFBundleShortVersionString</key><string>1.1.2</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
codesign --force --sign - "$bundle"
print -r -- "$bundle"

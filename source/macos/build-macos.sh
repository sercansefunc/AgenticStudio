#!/bin/bash
# Build "Agentic Studio.app": SwiftUI shell + PyInstaller-packaged agenticstudio.desk bridge.
# Usage: macos/build-macos.sh [open|install]
set -euo pipefail
MAC="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$MAC/.." && pwd)"
BUILD="$MAC/build"
APP="$BUILD/Agentic Studio.app"
VENV="$MAC/.build-venv"
CORE_OUT="$BUILD/core-dist/agentic-core"
CORE_BIN="$CORE_OUT/agentic-core"
VERSION="$(tr -d '\n\r' < "$MAC/VERSION")"
SHORT_VERSION="${VERSION%%-*}"
BUILD_NUMBER="${VERSION##*.}"
[[ "$BUILD_NUMBER" =~ ^[0-9]+$ ]] || BUILD_NUMBER="1"
CORE_HASH_FILE="$BUILD/core-source.sha256"

command -v xcrun >/dev/null 2>&1 || { echo "[!] Xcode Command Line Tools gerekli: xcode-select --install"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "[!] python3 (>=3.12) gerekli."; exit 1; }

mkdir -p "$BUILD"
if [[ ! -d "$VENV" ]]; then
  echo "[*] Build venv oluşturuluyor..."
  python3 -m venv "$VENV"
fi
PY="$VENV/bin/python"
"$PY" -m pip install --disable-pip-version-check -q pyinstaller "$REPO"

CORE_HASH="$({
  find "$REPO/src" -name '*.py' -print0 | sort -z | xargs -0 cat
  cat "$REPO/pyproject.toml" "$MAC/core/desk_entry.py" "$MAC/VERSION"
  "$PY" --version 2>&1
} | shasum -a 256 | awk '{print $1}')"
if [[ ! -x "$CORE_BIN" || "$CORE_HASH" != "$(cat "$CORE_HASH_FILE" 2>/dev/null || true)" ]]; then
  echo "[*] Python çekirdeği paketleniyor..."
  rm -rf "$BUILD/core-dist" "$BUILD/pyi-work" "$BUILD/agentic-core.spec"
  # Alembic loads migration scripts from disk, so they ship as files next to the package.
  "$PY" -m PyInstaller --noconfirm --onedir --name agentic-core \
    --distpath "$BUILD/core-dist" --workpath "$BUILD/pyi-work" --specpath "$BUILD" \
    --collect-submodules agenticstudio --collect-submodules google.genai \
    --add-data "$REPO/src/agenticstudio/persistence/migrations:agenticstudio/persistence/migrations" \
    "$MAC/core/desk_entry.py"
  printf '%s\n' "$CORE_HASH" > "$CORE_HASH_FILE"
else
  echo "[+] Çekirdek değişmedi; mevcut paket kullanılıyor."
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/AgenticCore"
cp "$MAC/Resources/Info.plist" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $SHORT_VERSION" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $BUILD_NUMBER" "$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :AgenticStudioReleaseLabel $VERSION" "$APP/Contents/Info.plist"
cp -R "$CORE_OUT/"* "$APP/Contents/Resources/AgenticCore/"

ICON_SRC="$MAC/Resources/AppIcon.png"
if [[ -f "$ICON_SRC" ]]; then
  ICONSET="$BUILD/AppIcon.iconset"
  rm -rf "$ICONSET"; mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    /usr/bin/sips -z "$size" "$size" "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    /usr/bin/sips -z $((size * 2)) $((size * 2)) "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  /usr/bin/iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
fi

case "$(uname -m)" in
  arm64) TARGET="arm64-apple-macos13.0" ;;
  x86_64) TARGET="x86_64-apple-macos13.0" ;;
  *) echo "[!] Desteklenmeyen mimari: $(uname -m)"; exit 1 ;;
esac

echo "[*] Agentic Studio $VERSION — SwiftUI derleniyor ($TARGET)..."
xcrun swiftc -O -parse-as-library -target "$TARGET" \
  -framework SwiftUI -framework AppKit -framework Security -framework Charts -framework UserNotifications \
  "$MAC"/Sources/*.swift \
  -o "$APP/Contents/MacOS/AgenticStudio"

chmod +x "$APP/Contents/MacOS/AgenticStudio" "$APP/Contents/Resources/AgenticCore/agentic-core"
# Stable identity (macos/setup-signing.sh) keeps keychain access across rebuilds; ad-hoc otherwise.
SIGN_ID="-"
security find-certificate -c "Agentic Studio Local" >/dev/null 2>&1 && SIGN_ID="Agentic Studio Local"
/usr/bin/codesign --force --deep --sign "$SIGN_ID" "$APP" >/dev/null 2>&1 \
  || { echo "[!] '$SIGN_ID' ile imzalanamadı, ad-hoc imza."; /usr/bin/codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true; }

echo "[+] Hazır: $APP"
if [[ "${1:-}" == "install" ]]; then
  DEST="$HOME/Applications/Agentic Studio.app"
  mkdir -p "$HOME/Applications"
  rm -rf "$DEST"
  /usr/bin/ditto "$APP" "$DEST"
  echo "[+] Kuruldu: $DEST"
  open "$DEST"
elif [[ "${1:-}" == "open" ]]; then
  open "$APP"
fi

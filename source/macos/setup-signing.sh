#!/bin/bash
# One-time: create a stable self-signed code signing identity in the login keychain.
# Ad-hoc signatures change on every build, so macOS asks for the keychain password each time;
# signing with a fixed certificate keeps the app's identity stable across builds.
set -euo pipefail
NAME="Agentic Studio Local"
if security find-certificate -c "$NAME" >/dev/null 2>&1; then
  echo "[+] '$NAME' zaten var."; exit 0
fi
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=$NAME" \
  -addext "keyUsage=critical,digitalSignature" -addext "extendedKeyUsage=critical,codeSigning" \
  -keyout "$TMP/key.pem" -out "$TMP/cert.pem" 2>/dev/null
openssl pkcs12 -export -legacy -inkey "$TMP/key.pem" -in "$TMP/cert.pem" \
  -out "$TMP/id.p12" -passout pass:agentic
security import "$TMP/id.p12" -k "$HOME/Library/Keychains/login.keychain-db" \
  -P agentic -T /usr/bin/codesign >/dev/null
echo "[+] '$NAME' oluşturuldu. Uygulamayı yeniden build et; ilk açılışta keychain için bir kez 'Always Allow' de."

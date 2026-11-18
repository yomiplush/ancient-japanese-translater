#!/usr/bin/env bash
# Install / uninstall the launcher entries for the current user.
#
# Writes to the XDG user data dir rather than /usr/share so nothing needs root
# and the entries follow the repo. Idempotent: re-running just overwrites.
set -euo pipefail

SELF="$(readlink -f "${BASH_SOURCE[0]}")"
SRC_DIR="$(cd "$(dirname "$SELF")" && pwd)"
APP_DIR="$(cd "$SRC_DIR/../.." && pwd)"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
LAUNCHER="$BIN_DIR/ajt-gui"

usage() {
    cat <<'EOF'
使い方: install-desktop.sh [--uninstall]

  (引数なし)  スタートメニュー（アプリランチャー）に登録
  --uninstall 登録を削除
EOF
}

write_icon() {
    local svg="$SRC_DIR/ajt.svg"
    if command -v rsvg-convert >/dev/null 2>&1; then
        for size in 48 128 256 512; do
            local dir="$ICON_ROOT/${size}x${size}/apps"
            mkdir -p "$dir"
            rsvg-convert -w "$size" -h "$size" "$svg" -o "$dir/ajt.png" 2>/dev/null || true
        done
    fi
    if command -v magick >/dev/null 2>&1; then
        local dir="$ICON_ROOT/256x256/apps"
        mkdir -p "$dir"
        [ -f "$dir/ajt.png" ] || magick -background none "$svg" -resize 256x256 "$dir/ajt.png" 2>/dev/null || true
    fi
    local scalable="$ICON_ROOT/scalable/apps"
    mkdir -p "$scalable"
    cp "$svg" "$scalable/ajt.svg"
    command -v gtk-update-icon-cache >/dev/null 2>&1 &&
        gtk-update-icon-cache -f -t "$ICON_ROOT" >/dev/null 2>&1 || true
}

entry_launch() {
    cat <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=古文 → 現代口語
Name[en]=Classical Japanese Translator
GenericName=Kobun OCR Translator
GenericName[ja]=古文ライブ翻訳
Comment=iPhoneのIPカメラで古文を撮り、現代口語にライブ翻訳します
Comment[en]=Live OCR and modern-Japanese translation of classical texts from an iPhone IP camera
Exec=$LAUNCHER
TryExec=$LAUNCHER
Icon=ajt
Terminal=false
Categories=Utility;
Keywords=japanese;kobun;古典;古文;翻訳;OCR;カメラ;kuzushiji;
StartupNotify=false
StartupWMClass=ajt
Actions=stop;

[Desktop Action stop]
Name=サーバーを停止
Exec=$LAUNCHER --stop
EOF
}

entry_stop() {
    cat <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=古文翻訳サーバーを停止
GenericName=Stop Translation Server
Comment=バックグラウンドで動いている古文翻訳サーバーを停止します
Exec=$LAUNCHER --stop
TryExec=$LAUNCHER
Icon=process-stop
Terminal=false
Categories=Utility;
Keywords=古文;翻訳;停止;stop;
StartupNotify=false
EOF
}

install_all() {
    [ -f "$APP_DIR/pyproject.toml" ] || {
        echo "プロジェクトが見つかりません: $APP_DIR" >&2
        exit 1
    }

    mkdir -p "$BIN_DIR" "$APPS_DIR"
    install -m 0755 "$SRC_DIR/ajt-launcher.sh" "$LAUNCHER"

    # The installed copy no longer sits next to the checkout, so its
    # "guess from my own location" fallback would resolve to $HOME/.local.
    # Bake the real path in; AJT_APP_DIR still overrides at runtime.
    sed -i "s|^DEFAULT_APP_DIR=.*|DEFAULT_APP_DIR=\"$APP_DIR\"|" "$LAUNCHER"
    grep -q "^DEFAULT_APP_DIR=\"$APP_DIR\"$" "$LAUNCHER" || {
        echo "ランチャーのパス埋め込みに失敗しました" >&2
        exit 1
    }

    entry_launch >"$APPS_DIR/ajt.desktop"
    entry_stop >"$APPS_DIR/ajt-stop.desktop"
    chmod 0644 "$APPS_DIR/ajt.desktop" "$APPS_DIR/ajt-stop.desktop"

    write_icon

    command -v update-desktop-database >/dev/null 2>&1 &&
        update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true

    local ok=1
    command -v desktop-file-validate >/dev/null 2>&1 &&
        desktop-file-validate "$APPS_DIR/ajt.desktop" "$APPS_DIR/ajt-stop.desktop" || ok=0

    echo "登録しました:"
    echo "  ランチャー: $APPS_DIR/ajt.desktop"
    echo "  停止用:     $APPS_DIR/ajt-stop.desktop"
    echo "  実行:       $LAUNCHER"
    echo "  アプリ名:   「古文 → 現代口語」"
    [ "$ok" -eq 1 ] || echo "  ※ desktop-file-validate が警告を出しています" >&2
}

uninstall_all() {
    rm -f "$APPS_DIR/ajt.desktop" "$APPS_DIR/ajt-stop.desktop" "$LAUNCHER"
    for size in 48 128 256 512; do
        rm -f "$ICON_ROOT/${size}x${size}/apps/ajt.png"
    done
    rm -f "$ICON_ROOT/scalable/apps/ajt.svg"
    command -v update-desktop-database >/dev/null 2>&1 &&
        update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    echo "登録を削除しました。"
}

case "${1:-}" in
    --uninstall) uninstall_all ;;
    -h|--help) usage ;;
    "") install_all ;;
    *) usage; exit 2 ;;
esac

#!/usr/bin/env bash
# Adiciona o "Sinal" ao menu de aplicativos (só para o seu usuário).
#   ./instalar.sh            instala o atalho e o ícone
#   ./instalar.sh --remover  remove o atalho e o ícone
set -euo pipefail

APP_ID="io.github.sinal.Sinal"
OLD_APP_ID="io.github.placadecaptura.PlacaDeCaptura"   # nome antigo ("Placa de Captura")
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$DIR/sinal.py"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST_DIR="$DATA_DIR/applications"
DEST="$DEST_DIR/$APP_ID.desktop"
ICON_DIR="$DATA_DIR/icons/hicolor"
ICON="$ICON_DIR/scalable/apps/$APP_ID.svg"
ICON_SYMBOLIC="$ICON_DIR/symbolic/apps/$APP_ID-symbolic.svg"

refresh() {
    command -v update-desktop-database >/dev/null && update-desktop-database "$DEST_DIR" >/dev/null 2>&1 || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -f -t "$ICON_DIR" >/dev/null 2>&1 || true
    command -v kbuildsycoca6 >/dev/null && kbuildsycoca6 >/dev/null 2>&1 || true
}

# Atalho da versão antiga, se existir.
rm -f "$DEST_DIR/$OLD_APP_ID.desktop"

if [[ "${1:-}" == "--remover" ]]; then
    rm -f "$DEST" "$ICON" "$ICON_SYMBOLIC"
    refresh
    echo "Atalho e ícone removidos."
    exit 0
fi

# O campo Exec do .desktop aceita espaços entre aspas, mas não estes caracteres.
if [[ "$SCRIPT" == *[\"\`\$\\]* ]]; then
    echo "O caminho da pasta contém caracteres especiais (\" \` \$ \\); mova-a para outro lugar." >&2
    exit 1
fi

chmod +x "$SCRIPT"
mkdir -p "$DEST_DIR" "$(dirname "$ICON")" "$(dirname "$ICON_SYMBOLIC")"
install -m 644 "$DIR/icones/$APP_ID.svg" "$ICON"
install -m 644 "$DIR/icones/$APP_ID-symbolic.svg" "$ICON_SYMBOLIC"

cat > "$DEST" <<EOF
[Desktop Entry]
Type=Application
Name=Sinal
GenericName=Visualizador de placa de captura
Comment=Mostra a imagem da webcam ou placa de captura e devolve o áudio para a caixa de som
Exec="$SCRIPT"
Icon=$APP_ID
Terminal=false
Categories=AudioVideo;Video;Player;
Keywords=PS5;PlayStation;console;HDMI;capture;captura;placa;webcam;câmera;sinal;
StartupNotify=true
StartupWMClass=$APP_ID
EOF

refresh
echo "Atalho instalado: $DEST"
echo "Procure por \"Sinal\" no menu de aplicativos."

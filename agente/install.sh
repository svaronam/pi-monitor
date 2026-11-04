#!/usr/bin/env bash
# Instala o actualiza pi-agent (Pi Monitor v2.1) en una Raspberry Pi. Uso:  sudo bash install.sh
# Si ya había una versión, hace copia de seguridad en /opt/pi-agent.bak-FECHA y conserva token, bots y ajustes.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Ejecútalo con sudo:  sudo bash install.sh"; exit 1; }
RUN_USER="${SUDO_USER:-sergio}"
PORT="${PORT:-8790}"
SRC="$(cd "$(dirname "$0")" && pwd)"
DIR=/opt/pi-agent
CONF=/etc/pi-agent/config.json
SYSTEMCTL="$(command -v systemctl)"

python3 -m py_compile "$SRC/pi-agent.py" "$SRC/pi-agent-unit" "$SRC/enlazar_bots.py"
rm -rf "$SRC/__pycache__"
if [ -d "$DIR" ]; then
  TS="$(date +%Y%m%d-%H%M%S)"
  cp -a "$DIR" "$DIR.bak-$TS"
  [ -f "$CONF" ] && cp -a "$CONF" "$CONF.bak-$TS"
  echo "→ Copia de seguridad: $DIR.bak-$TS"
  echo "  (para volver atrás: sudo rm -rf $DIR && sudo mv $DIR.bak-$TS $DIR && sudo systemctl restart pi-agent)"
fi

echo "→ Instalando en $DIR (usuario: $RUN_USER, puerto: $PORT)"
install -d "$DIR" "$DIR/bots"
install -m 755 "$SRC/pi-agent.py" "$DIR/pi-agent.py"
install -m 644 "$SRC/index.html" "$DIR/index.html"
# Ayudante (root) que crea los servicios de los bots; los bots corren como $RUN_USER
sed "s/__RUN_USER__/$RUN_USER/g" "$SRC/pi-agent-unit" > "$DIR/pi-agent-unit"
chown root:root "$DIR" "$DIR/pi-agent-unit" "$DIR/bots"
chmod 755 "$DIR/pi-agent-unit"

install -d -o "$RUN_USER" -m 700 /etc/pi-agent
if [ ! -f "$CONF" ]; then
  TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
  SERVICES="[]"
  for s in tailscaled.service ssh.service; do
    systemctl list-unit-files "$s" --no-legend 2>/dev/null | grep -q . && SERVICES="$(python3 -c "import json,sys;l=json.loads(sys.argv[1]);l.append(sys.argv[2]);print(json.dumps(l))" "$SERVICES" "$s")"
  done
  cat > "$CONF" <<EOF
{
  "token": "$TOKEN",
  "port": $PORT,
  "bind": "0.0.0.0",
  "services": $SERVICES,
  "bots": [],
  "allow_exec": true,
  "exec_timeout": 30
}
EOF
  echo "→ Configuración nueva creada"
else
  echo "→ Configuración existente conservada ($CONF)"
fi
chown "$RUN_USER" "$CONF"; chmod 600 "$CONF"
# la carpeta de datos (eventos, historial, latidos, resúmenes) tiene que ser del usuario del agente
chown "$RUN_USER" /etc/pi-agent

echo "→ Enlazando tus bots"
python3 "$SRC/enlazar_bots.py" "$CONF"
command -v git >/dev/null || echo "  (Aviso: git no está instalado; «Actualizar bot» no funcionará hasta que hagas: sudo apt install git)"

# sudo sin contraseña SOLO para: gestionar servicios, reiniciar/apagar y el ayudante de bots
SUDOERS=/etc/sudoers.d/pi-agent
cat > "$SUDOERS.tmp" <<EOF
$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart *, $SYSTEMCTL start *, $SYSTEMCTL stop *, $SYSTEMCTL reboot, $SYSTEMCTL poweroff, $DIR/pi-agent-unit *
EOF
chmod 440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null && mv "$SUDOERS.tmp" "$SUDOERS"

cat > /etc/systemd/system/pi-agent.service <<EOF
[Unit]
Description=pi-agent (diagnóstico y control para Pi Monitor)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
SupplementaryGroups=adm systemd-journal video
ExecStart=/usr/bin/python3 $DIR/pi-agent.py
Restart=always
RestartSec=5
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable pi-agent.service >/dev/null 2>&1
systemctl restart pi-agent.service
sleep 2
systemctl is-active --quiet pi-agent.service && echo "→ pi-agent está corriendo" || { echo "✗ No arrancó. Mira: journalctl -u pi-agent -n 30"; exit 1; }

TOKEN="$(python3 -c "import json;print(json.load(open('$CONF'))['token'])")"
TSIP="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
LANIP="$(hostname -I | awk '{print $1}')"
TZNOW="$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo '?')"
echo
echo "================ Datos para la app ================"
echo " Nombre : $(hostname)"
[ -n "$TSIP" ] && echo " URL    : http://$TSIP:$PORT   (Tailscale, funciona fuera de casa)"
echo " URL LAN: http://$LANIP:$PORT"
echo " Token  : $TOKEN"
echo "==================================================="
[ -z "$TSIP" ] && echo "Aviso: Tailscale no está activo en esta Pi; solo funcionará en tu WiFi."
echo "Zona horaria: $TZNOW (los bots programados usan esta hora)."
[ "$TZNOW" != "Europe/Madrid" ] && echo "  Para ponerla en hora de España:  sudo timedatectl set-timezone Europe/Madrid"
exit 0

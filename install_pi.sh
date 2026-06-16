#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  TetraMonitor — Raspberry Pi installer (headless + webserver)
#
#  Installeert rtl_tcp + numpy, blokkeert de DVB-T-kerneldriver (anders kan
#  rtl_tcp de dongle niet pakken) en zet een systemd-service neer die de
#  detector + webserver automatisch start bij het opstarten. Daarna open je
#  op je telefoon http://<pi-ip>:8080.
#
#  Gebruik (op de Pi, in de projectmap):
#       chmod +x install_pi.sh
#       ./install_pi.sh
# ─────────────────────────────────────────────────────────────────────────────
set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="$(whoami)"
HTTP_PORT="${1:-8080}"          # optioneel: ./install_pi.sh 80
PY="$(command -v python3)"

echo "📦 1/4  Pakketten installeren (rtl-sdr + numpy)…"
sudo apt-get update -qq
sudo apt-get install -y rtl-sdr python3-numpy

echo "🚫 2/4  DVB-T-kerneldriver blokkeren (zodat rtl_tcp de dongle kan pakken)…"
sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf >/dev/null <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
# Nu meteen uitladen (geen reboot nodig); negeer fout als hij niet geladen is.
sudo modprobe -r dvb_usb_rtl28xxu 2>/dev/null || true

echo "⚙️  3/4  systemd-service aanmaken (autostart bij opstarten)…"
sudo tee /etc/systemd/system/tetramonitor.service >/dev/null <<EOF
[Unit]
Description=TetraMonitor (headless TETRA-activiteitsmonitor + webserver)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$REPO
ExecStart=$PY $REPO/tetra_web.py --http-port $HTTP_PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "🚀 4/4  Service inschakelen en starten…"
sudo systemctl daemon-reload
sudo systemctl enable --now tetramonitor.service

# Pi-IP bepalen voor de URL
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -z "$IP" ] && IP="<pi-ip>"

echo
echo "✅ Klaar! TetraMonitor draait nu headless en start automatisch bij elke boot."
echo
echo "   📱 Open op je telefoon (zelfde wifi / Pi-hotspot):"
echo "        http://$IP:$HTTP_PORT"
echo
echo "   Handige commando's:"
echo "     sudo systemctl status  tetramonitor   # draait hij?"
echo "     sudo systemctl restart tetramonitor   # herstarten"
echo "     sudo systemctl stop    tetramonitor   # stoppen"
echo "     sudo systemctl disable tetramonitor   # niet meer autostarten"
echo "     journalctl -u tetramonitor -f         # live log bekijken"
echo
echo "   Zie je 'No supported devices found'? Sluit de RTL-SDR aan (of opnieuw"
echo "   in-/uitpluggen) — de service probeert het daarna vanzelf opnieuw."

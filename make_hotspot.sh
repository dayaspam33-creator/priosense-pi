#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  TetraMonitor — Pi eigen wifi-hotspot (plug-and-play in de auto)
#
#  Zet de Pi om in een wifi-toegangspunt met een VAST IP, zodat je geen router
#  nodig hebt en de URL altijd hetzelfde is. Telefoon op de Pi-hotspot →
#  open http://10.42.0.1:8080. Blijft aan bij elke boot.
#
#  Gebruik (op de Pi):
#       ./make_hotspot.sh                       # standaard SSID/wachtwoord
#       ./make_hotspot.sh MijnSSID MijnWachtwoord
#       ./make_hotspot.sh off                   # weer terug naar gewone wifi
#
#  Vereist NetworkManager (standaard op Raspberry Pi OS Bookworm).
# ─────────────────────────────────────────────────────────────────────────────
set -e
NAME="tetra-hotspot"

if ! command -v nmcli >/dev/null 2>&1; then
    echo "❌ Dit script vereist NetworkManager (nmcli) — standaard op Raspberry Pi OS"
    echo "   Bookworm. Draai je een ouder OS? Zeg het, dan maak ik een hostapd-versie."
    exit 1
fi

if [ "$1" = "off" ]; then
    sudo nmcli connection modify "$NAME" autoconnect no 2>/dev/null || true
    sudo nmcli connection down "$NAME" 2>/dev/null || true
    echo "✅ Hotspot uit. De Pi verbindt na een reboot weer met je gewone wifi."
    exit 0
fi

SSID="${1:-TetraMonitor}"
PASS="${2:-tetra1234}"
if [ ${#PASS} -lt 8 ]; then
    echo "❌ Het wachtwoord moet minstens 8 tekens zijn (WPA-eis)."
    exit 1
fi

echo "⚠️  Let op: hierna wordt wlan0 een hotspot. De Pi verliest dan z'n"
echo "    wifi-internet en een SSH-sessie via wifi valt weg. Doe dit via het"
echo "    Pi-scherm of via een netwerkkabel — of verbind daarna gewoon met de"
echo "    Pi-hotspot zelf."
echo

# Wifi-land instellen (AP-modus start anders soms niet).
sudo raspi-config nonint do_wifi_country NL 2>/dev/null || true

sudo nmcli connection delete "$NAME" 2>/dev/null || true
sudo nmcli connection add type wifi ifname wlan0 con-name "$NAME" autoconnect yes ssid "$SSID"
sudo nmcli connection modify "$NAME" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    ipv4.method shared \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.psk "$PASS"
sudo nmcli connection up "$NAME"
sleep 3

IP="$(nmcli -g IP4.ADDRESS device show wlan0 2>/dev/null | head -1 | cut -d/ -f1)"
[ -z "$IP" ] && IP="10.42.0.1"

echo
echo "✅ Hotspot actief — en start automatisch bij elke boot."
echo "   📶 Wifi-netwerk:  $SSID"
echo "   🔑 Wachtwoord:    $PASS"
echo "   📱 Open daarna op je telefoon:   http://$IP:8080"
echo
echo "   Terug naar gewone wifi:   ./make_hotspot.sh off"

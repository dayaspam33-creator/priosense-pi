#!/usr/bin/env python3
"""
TetraMonitor — headless webserver voor de Raspberry Pi.

Draait de detector zonder scherm en serveert een pagina die je op je telefoon
opent: de 3 activiteitsbalken, het alarm-banner en knoppen voor de modi
(rijmodus, band, gain, geluid). Gebruikt alleen numpy + Python-stdlib (geen Qt,
geen pyqtgraph) → draait op zwakke hardware zoals een Pi 3B+.

Gebruik op de Pi:
    python3 tetra_web.py                 # poort 8080
    python3 tetra_web.py --http-port 80  # of een andere poort

Open daarna op je telefoon (zelfde wifi / Pi-hotspot):  http://<pi-ip>:8080
"""
import argparse
import json
import os
import socket
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tetra_core import (Detector, RtlTcpSource, RIJMODI, BANDS, CUSTOM_IDX,
                        SOFT_THRESHOLD_DB, HARD_THRESHOLD_DB, DEFAULT_GAIN_DB)

GAIN_MODES = ["Handmatig", "Auto-reductie", "Volautomatisch"]
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "tetra_web_settings.json")


# ── Bediening (headless, gedeeld door alle webverzoeken) ─────────────────────
class Controller:
    def __init__(self, det: Detector):
        self.det = det
        s = self._load()
        self.mode_idx  = s.get("mode_idx", CUSTOM_IDX)
        self.custom    = s.get("custom", {"soft": det.soft_thr, "hard": det.hard_thr})
        self.band_idx  = s.get("band_idx", 1)
        self.gain_mode = s.get("gain_mode", 1)
        self.det.muted = s.get("muted", False)
        self.apply_mode(self.mode_idx)
        self.apply_gain_mode(self.gain_mode)
        self.det.retune(BANDS[self.band_idx][1])

    def _load(self):
        try:
            with open(SETTINGS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        try:
            with open(SETTINGS_FILE, "w") as f:
                json.dump({"mode_idx": self.mode_idx, "custom": self.custom,
                           "band_idx": self.band_idx, "gain_mode": self.gain_mode,
                           "muted": self.det.muted}, f)
        except OSError:
            pass

    def apply_mode(self, idx):
        self.mode_idx = idx
        m = RIJMODI[idx]
        soft, hard = (self.custom["soft"], self.custom["hard"]) \
            if m["name"] == "Custom" else (m["soft"], m["hard"])
        self.det.soft_thr, self.det.hard_thr = soft, hard

    def apply_gain_mode(self, idx):
        self.gain_mode = idx
        self.det.auto_gain_reduction = (idx == 1)
        self.det.src.auto_gain = (idx == 2)
        self.det.src.apply_gain()

    def cmd(self, action):
        if action == "mode":
            self.apply_mode((self.mode_idx + 1) % len(RIJMODI))
        elif action == "band":
            self.band_idx = (self.band_idx + 1) % len(BANDS)
            self.det.retune(BANDS[self.band_idx][1])
        elif action == "gain":
            self.apply_gain_mode((self.gain_mode + 1) % len(GAIN_MODES))
        elif action == "mute":
            self.det.muted = not self.det.muted
        elif action == "reset":
            self.det.reset_noise_floor()
        elif action == "blacklist":
            self.det.clear_blacklist()
        self._save()

    def state(self):
        snap = self.det.snapshot_lite()
        return {
            "active":    [[round(f, 4), round(l, 1), t] for f, l, t in snap["active"][:3]],
            "total":     len(snap["active"]),
            "alarm":     snap["alarm_level"],
            "alarm_freq": round(snap["alarm_freq"], 4),
            "alarm_db":  round(snap["alarm_db"], 1),
            "status":    snap["status"],
            "overload":  snap["overload"],
            "haze_db":   round(snap["haze_db"], 0),
            "blacklist": snap["blacklist"],
            "gain":      round(snap["gain"], 0),
            "soft":      self.det.soft_thr,
            "hard":      self.det.hard_thr,
            "mode":      RIJMODI[self.mode_idx]["name"],
            "band":      BANDS[self.band_idx][0],
            "gainmode":  GAIN_MODES[self.gain_mode],
            "muted":     self.det.muted,
        }


# ── Telefoonpagina ───────────────────────────────────────────────────────────
PAGE = r"""<!doctype html><html lang="nl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>TetraMonitor</title>
<style>
  :root{--bg:#101216;--panel:#181b20;--panel2:#23272e;--sep:#2c313a;
        --green:#34d27b;--yellow:#ffcc33;--red:#ff4d4d;--orange:#ff9933;
        --gray1:#c7cdd6;--gray2:#8a92a0;}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;background:var(--bg);color:var(--gray1);
       font-family:-apple-system,system-ui,Roboto,sans-serif;}
  #banner{margin:8px;padding:14px;border-radius:14px;border:2px solid var(--sep);
          background:var(--panel);text-align:center}
  #banner b{font-size:20px;display:block}
  #banner span{font-size:13px;color:var(--gray1)}
  #bars{display:flex;gap:8px;margin:8px;height:46vh}
  .bar{flex:1;background:var(--panel);border-radius:12px;display:flex;
       flex-direction:column;align-items:center;padding:8px 4px;overflow:hidden}
  .track{flex:1;width:46%;background:var(--panel2);border-radius:8px;
         position:relative;overflow:hidden;min-height:40px}
  .fill{position:absolute;left:0;right:0;bottom:0;border-radius:8px;
        transition:height .15s,background .15s}
  .lbl{margin-top:6px;text-align:center;line-height:1.25}
  .lbl .f{font-weight:700;font-size:16px}
  .lbl .d{font-weight:700;font-size:15px}
  .lbl .t{font-size:13px;font-weight:700}
  .empty{color:var(--gray2);font-size:22px;font-weight:700;margin:auto}
  #btns{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:8px}
  button{padding:14px 8px;border-radius:10px;border:1px solid var(--sep);
         background:var(--panel);color:var(--gray1);font-size:15px;font-weight:600}
  button:active{background:var(--panel2)}
  button small{display:block;color:var(--gray2);font-weight:400;font-size:11px;margin-top:2px}
  #info{margin:8px;color:var(--gray2);font-size:12px;text-align:center}
</style></head><body>
<div id="banner"><b id="bt">●</b><span id="bd">verbinden…</span></div>
<div id="bars"><div class="bar" id="b0"></div><div class="bar" id="b1"></div><div class="bar" id="b2"></div></div>
<div id="btns">
  <button onclick="cmd('mode')">Rijmodus<small id="m">—</small></button>
  <button onclick="cmd('band')">Band<small id="bn">—</small></button>
  <button onclick="cmd('gain')">Gain<small id="g">—</small></button>
  <button onclick="cmd('mute')">Geluid<small id="mu">—</small></button>
  <button onclick="cmd('reset')">Reset ruisvloer<small>opnieuw inregelen</small></button>
  <button onclick="cmd('blacklist')">Wis negeerlijst<small id="bl">—</small></button>
</div>
<div id="info">—</div>
<script>
function colorFor(l,soft,hard){return l>=hard?'var(--red)':l>=soft?'var(--yellow)':'var(--green)';}
function renderBar(i,s){
  var el=document.getElementById('b'+i), a=s.active[i];
  if(!a){el.innerHTML='<div class="empty">—</div>';return;}
  var f=a[0], lvl=a[1], tr=a[2];
  var full=Math.max(1,s.hard+6), pct=Math.max(0,Math.min(1,lvl/full))*100;
  var col=colorFor(lvl,s.soft,s.hard);
  var arrow=tr>0?'▲ nadert':tr<0?'▼ gaat weg':'► stabiel';
  var ac=tr>0?'var(--green)':tr<0?'var(--orange)':'var(--gray2)';
  el.innerHTML='<div class="track"><div class="fill" style="height:'+pct+'%;background:'+col+'"></div></div>'+
    '<div class="lbl"><div class="f" style="color:'+col+'">'+f.toFixed(3)+' MHz</div>'+
    '<div class="d">+'+Math.round(lvl)+' dB</div>'+
    '<div class="t" style="color:'+ac+'">'+arrow+'</div></div>';
}
function render(s){
  var bt=document.getElementById('bt'), bd=document.getElementById('bd'),
      ban=document.getElementById('banner');
  var bg='var(--panel)',bc='var(--sep)',tc='var(--gray2)';
  if(s.overload){bg='#2d0b0b';bc='var(--red)';tc='var(--red)';
     bt.textContent='🚨 ZEER STERK SIGNAAL DICHTBIJ';bd.textContent='zender vlakbij — overstuur';}
  else if(s.alarm==2){bg='#2d0b0b';bc='var(--red)';tc='var(--red)';
     bt.textContent='🚨 ACTIVITEIT';bd.textContent=s.alarm_freq.toFixed(3)+' MHz   +'+Math.round(s.alarm_db)+' dB';}
  else if(s.alarm==1){bg='#2a1f00';bc='var(--orange)';tc='var(--orange)';
     bt.textContent='◆ MOGELIJKE ACTIVITEIT';bd.textContent=s.alarm_freq.toFixed(3)+' MHz   +'+Math.round(s.alarm_db)+' dB';}
  else{bt.textContent='● GEEN ACTIVITEIT';bd.textContent=s.status;}
  ban.style.background=bg;ban.style.borderColor=bc;bt.style.color=tc;
  for(var i=0;i<3;i++)renderBar(i,s);
  document.getElementById('m').textContent=s.mode;
  document.getElementById('bn').textContent=s.band.split(' ').slice(0,2).join(' ');
  document.getElementById('g').textContent=s.gainmode;
  document.getElementById('mu').textContent=s.muted?'gedempt':'aan';
  document.getElementById('bl').textContent=s.blacklist+' genegeerd';
  document.getElementById('info').textContent=
    'actief: '+s.total+'   ·   gain '+Math.round(s.gain)+' dB   ·   drempel '+
    Math.round(s.soft)+'/'+Math.round(s.hard)+' dB'+(s.haze_db>0?'   ·   ⚠ vloer +'+s.haze_db+' dB':'');
}
async function poll(){try{var r=await fetch('/state');render(await r.json());}catch(e){}}
async function cmd(a){try{var r=await fetch('/cmd?action='+a,{method:'POST'});render(await r.json());}catch(e){}}
setInterval(poll,400);poll();
</script></body></html>"""


# ── HTTP-server ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    controller = None

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        elif self.path.startswith("/state"):
            self._send(200, "application/json", json.dumps(self.controller.state()).encode())
        else:
            self._send(404, "text/plain", b"404")

    def do_POST(self):
        if self.path.startswith("/cmd"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.controller.cmd(q.get("action", [""])[0])
            self._send(200, "application/json", json.dumps(self.controller.state()).encode())
        else:
            self._send(404, "text/plain", b"404")

    def log_message(self, *a):
        pass   # stille server


def _lan_ip():
    # 1) Uitgaand IP (werkt als er internet is).
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # 2) Geen internet (bijv. eigen Pi-hotspot): pak het eerste echte IP.
    try:
        for ip in subprocess.check_output(["hostname", "-I"], text=True).split():
            if "." in ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return "127.0.0.1"


def main():
    p = argparse.ArgumentParser(description="TetraMonitor headless webserver")
    p.add_argument("--center", type=float, default=382.5, help="Center MHz (default 382.5)")
    p.add_argument("--gain", type=float, default=DEFAULT_GAIN_DB)
    p.add_argument("--ppm", type=int, default=0)
    p.add_argument("--port", type=int, default=1234, help="rtl_tcp poort")
    p.add_argument("--device", type=int, default=0)
    p.add_argument("--extern", action="store_true")
    p.add_argument("--http-port", type=int, default=8080, help="webserver-poort")
    args = p.parse_args()

    source = RtlTcpSource(args)
    try:
        source.connect()
    except Exception as e:
        print(f"❌ Geen verbinding met rtl_tcp op poort {args.port}: {e}")
        print("   Sluit de RTL-SDR aan en zorg dat rtl_tcp draait (of laat de app "
              "hem zelf starten).")
        raise SystemExit(1)

    det = Detector(source)
    det.start()
    Handler.controller = Controller(det)

    srv = ThreadingHTTPServer(("0.0.0.0", args.http_port), Handler)
    url = f"http://{_lan_ip()}:{args.http_port}"
    print(f"✅ TetraMonitor draait headless.")
    print(f"   Open op je telefoon (zelfde wifi/hotspot):  {url}")
    print("   Stoppen: Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStoppen…")
    finally:
        det.stop()
        source.close()


if __name__ == "__main__":
    main()

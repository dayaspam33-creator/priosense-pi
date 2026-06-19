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
import struct
import subprocess
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from tetra_core import (Detector, RtlTcpSource, RIJMODI, BANDS, CUSTOM_IDX,
                        SOFT_THRESHOLD_DB, HARD_THRESHOLD_DB, DEFAULT_GAIN_DB)

GAIN_MODES = ["Handmatig", "Auto-reductie", "Volautomatisch"]
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "tetra_web_settings.json")


# ── App-icoon (zelf gegenereerd, geen externe bestanden nodig) ───────────────
def _png_bytes(rgb):
    """Minimale PNG-encoder (RGB, 8-bit) — alleen numpy + zlib + struct."""
    h, w, _ = rgb.shape
    raw = bytearray()
    for y in range(h):
        raw.append(0)                       # filterbyte 0 per scanlijn
        raw.extend(rgb[y].tobytes())
    def _chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))

def _make_icon(size=512):
    """Donkere PrioSense-tegel met één rechtopstaande balk (groen→oranje→rood).
    iOS maakt de hoeken zelf rond, dus we vullen het hele vierkant."""
    img = np.empty((size, size, 3), dtype=np.uint8)
    img[:] = (39, 40, 50)                                   # achtergrond #272832
    bw   = int(size * 0.30)
    x0   = (size - bw) // 2
    top  = int(size * 0.16)
    base = int(size * 0.84)
    h    = base - top
    # van onder naar boven: groen (#30d158), oranje (#ff9f0a), rood (#ff453a)
    img[base - int(h * 0.45):base,                 x0:x0 + bw] = (48, 209, 88)
    img[base - int(h * 0.78):base - int(h * 0.45), x0:x0 + bw] = (255, 159, 10)
    img[top:base - int(h * 0.78),                  x0:x0 + bw] = (255, 69, 58)
    return _png_bytes(img)

ICON_PNG = _make_icon()
MANIFEST = json.dumps({
    "name": "PrioSense", "short_name": "PrioSense",
    "display": "standalone", "background_color": "#272832",
    "theme_color": "#272832", "start_url": "/",
    "icons": [{"src": "/icon.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/icon.png", "sizes": "512x512", "type": "image/png"}],
})


# ── Bediening (headless, gedeeld door alle webverzoeken) ─────────────────────
class Controller:
    def __init__(self, det: Detector):
        self.det = det
        s = self._load()
        self.mode_idx  = s.get("mode_idx", CUSTOM_IDX)
        self.custom    = s.get("custom", {"groen": RIJMODI[CUSTOM_IDX]["groen"],
                                          "soft": det.soft_thr, "hard": det.hard_thr})
        self.green     = self.custom.get("groen", RIJMODI[CUSTOM_IDX]["groen"])  # vloer (alleen weergave)
        self.band_idx  = s.get("band_idx", 1)
        self.gain_mode = s.get("gain_mode", 1)
        self.gain      = s.get("gain", det.src.gain_db)   # ingestelde gain (dB)
        self.det.muted = s.get("muted", False)
        self.det.src.gain_db = self.gain
        self.det.agc_max     = self.gain                  # plafond voor auto-reductie
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
                           "gain": self.gain, "muted": self.det.muted}, f)
        except OSError:
            pass

    def apply_mode(self, idx):
        self.mode_idx = idx
        m = RIJMODI[idx]
        if m["name"] == "Custom":
            green, soft, hard = (self.custom.get("groen", m["groen"]),
                                 self.custom["soft"], self.custom["hard"])
        else:
            green, soft, hard = m["groen"], m["soft"], m["hard"]
        self.green = green                                   # vloer (alleen weergave)
        self.det.soft_thr, self.det.hard_thr = soft, hard

    def apply_gain_mode(self, idx):
        self.gain_mode = idx
        self.det.auto_gain_reduction = (idx == 1)
        self.det.src.auto_gain = (idx == 2)
        self.det.src.apply_gain()

    def cmd(self, action, value=None):
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
        elif action in ("groen", "soft", "hard") and value is not None:
            v = max(1.0, min(80.0, float(value)))
            if action == "groen":
                self.custom["groen"] = v
                self.green = v
            elif action == "soft":
                self.custom["soft"] = v
                self.det.soft_thr = v
            else:
                self.custom["hard"] = v
                self.det.hard_thr = v
            self.mode_idx = CUSTOM_IDX
        elif action == "gainval" and value is not None:
            # Zet de gain-waarde én het auto-reductie-plafond; werkt zo in elke
            # modus (handmatig = vast, auto-reductie = normale/maximale gain).
            v = max(0.0, min(49.0, float(value)))
            self.gain = v
            self.det.src.gain_db = v
            self.det.agc_max = v
            self.det.src.apply_gain()
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
            "connected": snap["connected"],
            "overload":  snap["overload"],
            "haze_db":   round(snap["haze_db"], 0),
            "blacklist": snap["blacklist"],
            "gain":      round(snap["gain"], 0),       # live (kan auto-gedaald zijn)
            "gain_set":  round(self.gain, 0),          # ingesteld (schuifstand)
            "groen":     self.green,                    # vloer: vanaf hier loopt de balk
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
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>PrioSense</title>
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="PrioSense">
<meta name="theme-color" content="#272832">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="icon" href="/icon.png">
<link rel="manifest" href="/manifest.json">
<style>
  :root{--bg:#272832;--green:#30d158;--orange:#ff9f0a;--red:#ff453a;--blue:#0a84ff;
        --g1:rgba(235,235,245,.85);--g2:rgba(235,235,245,.65);
        --glass:rgba(255,255,255,.12);--gline:rgba(255,255,255,.22);--ghi:rgba(255,255,255,.3);}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;background:var(--bg);color:#fff;
       font-family:-apple-system,system-ui,Roboto,sans-serif;
       padding:env(safe-area-inset-top) 14px env(safe-area-inset-bottom);}
  .glass{backdrop-filter:blur(20px) saturate(160%);-webkit-backdrop-filter:blur(20px) saturate(160%);
         background:var(--glass);border:1px solid var(--gline);box-shadow:inset 0 1px 0 var(--ghi);}
  #top{display:flex;align-items:center;justify-content:space-between;margin:14px 0 16px}
  #brand{display:flex;align-items:center;gap:9px;padding:9px 16px 9px 13px;border-radius:22px}
  #brand b{font-size:18px;font-weight:500;letter-spacing:.3px}
  #dot{width:10px;height:10px;border-radius:50%;background:var(--blue);display:inline-block}
  #gear{width:42px;height:42px;border-radius:50%;color:#fff;display:flex;align-items:center;justify-content:center;padding:0}
  #banner{border-radius:22px;padding:13px;text-align:center;margin-bottom:18px;transition:border-color .3s,background .3s}
  #banner b{font-size:17px;font-weight:500;display:block;color:var(--g1)}
  #banner span{font-size:13px;color:var(--g2)}
  #barwrap{border-radius:30px;padding:16px;margin:0 auto 16px;width:212px;
           background:rgba(255,255,255,.09);backdrop-filter:blur(22px) saturate(140%);
           -webkit-backdrop-filter:blur(22px) saturate(140%);
           border:1px solid rgba(255,255,255,.18);box-shadow:inset 0 1px 0 rgba(255,255,255,.26)}
  #bar{display:flex;flex-direction:column-reverse;gap:6px;height:46vh;min-height:240px;max-height:330px}
  .seg{flex:1;border-radius:9px;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.14);
       transition:background .12s,border-color .12s}
  #read{text-align:center}
  #db{font-size:46px;font-weight:500;line-height:1;color:var(--g1)}
  #freq{font-size:18px;font-weight:500;margin-top:8px;color:var(--g2)}
  #trend{font-size:14px;font-weight:500;margin-top:6px;color:var(--g2)}
  #scrim{position:fixed;inset:0;z-index:4;background:rgba(0,0,0,.4);opacity:0;pointer-events:none;transition:opacity .3s}
  #scrim.open{opacity:1;pointer-events:auto}
  #drawer{position:fixed;left:0;right:0;bottom:0;z-index:5;
          padding:18px 16px calc(26px + env(safe-area-inset-bottom));
          border-radius:30px 30px 0 0;background:rgba(54,55,66,.74);
          backdrop-filter:blur(34px) saturate(170%);-webkit-backdrop-filter:blur(34px) saturate(170%);
          border-top:1px solid rgba(255,255,255,.24);box-shadow:inset 0 1px 0 rgba(255,255,255,.28);
          transform:translateY(108%);transition:transform .32s cubic-bezier(.32,.72,0,1)}
  #drawer.open{transform:translateY(0)}
  #grab{width:38px;height:5px;border-radius:3px;background:rgba(255,255,255,.4);margin:0 auto 14px}
  #dhead{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
  #dhead b{font-size:16px;font-weight:500}
  #close{display:flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:50%;
         background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.24);color:#fff;padding:0}
  #btns{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-bottom:14px}
  #btns button{padding:12px 6px;border-radius:14px;border:1px solid rgba(255,255,255,.2);
        background:rgba(255,255,255,.12);color:#fff;font-size:14px;font-weight:500;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.22)}
  #btns button:active{background:rgba(255,255,255,.2)}
  #btns button.wide{grid-column:span 2}
  #btns small{display:block;color:var(--g2);font-weight:400;font-size:11px;margin-top:2px}
  .sl{display:flex;align-items:center;gap:10px;margin:9px 0}
  .sl label{flex:0 0 64px;font-size:13px;color:var(--g2)}
  .sl input{flex:1}
  .sl .v{flex:0 0 46px;text-align:right;font-weight:500;font-size:14px}
</style></head><body>
<div id="top">
  <div id="brand" class="glass"><span id="dot"></span><b>PrioSense</b></div>
  <button id="gear" class="glass" aria-label="Instellingen" onclick="toggleDrawer()">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
  </button>
</div>
<div id="banner"><b id="bt">●</b><span id="bd">verbinden…</span></div>
<div id="barwrap"><div id="bar"></div></div>
<div id="read">
  <div id="db">—</div>
  <div id="freq">—</div>
  <div id="trend">geen contact</div>
</div>
<div id="scrim" onclick="closeDrawer()"></div>
<div id="drawer">
  <div id="grab"></div>
  <div id="dhead"><b>Instellingen</b>
    <button id="close" aria-label="Sluiten" onclick="closeDrawer()">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
    </button>
  </div>
  <div id="btns">
    <button onclick="cmd('mode')">Rijmodus<small id="m">—</small></button>
    <button onclick="cmd('band')">Band<small id="bn">—</small></button>
    <button onclick="cmd('gain')">Gain<small id="g">—</small></button>
    <button onclick="cmd('mute')">Geluid<small id="mu">—</small></button>
    <button class="wide" onclick="cmd('reset')">Reset ruisvloer<small>opnieuw inregelen</small></button>
  </div>
  <div class="sl"><label>Groen ≥</label>
    <input id="sgroen" type="range" min="5" max="50" step="1" style="accent-color:var(--green)"
      oninput="document.getElementById('vgroen').textContent=this.value+' dB'"
      onchange="cmdVal('groen',this.value)">
    <span class="v" id="vgroen">— dB</span></div>
  <div class="sl"><label>Oranje ≥</label>
    <input id="ssoft" type="range" min="10" max="60" step="1" style="accent-color:var(--orange)"
      oninput="document.getElementById('vsoft').textContent=this.value+' dB'"
      onchange="cmdVal('soft',this.value)">
    <span class="v" id="vsoft">— dB</span></div>
  <div class="sl"><label>Rood ≥</label>
    <input id="shard" type="range" min="15" max="70" step="1" style="accent-color:var(--red)"
      oninput="document.getElementById('vhard').textContent=this.value+' dB'"
      onchange="cmdVal('hard',this.value)">
    <span class="v" id="vhard">— dB</span></div>
  <div class="sl"><label>Gain</label>
    <input id="sgain" type="range" min="0" max="49" step="1" style="accent-color:var(--blue)"
      oninput="document.getElementById('vgain').textContent=this.value+' dB'"
      onchange="cmdVal('gainval',this.value)">
    <span class="v" id="vgain">— dB</span></div>
</div>
<script>
var GREEN=20, SOFT=35, HARD=45;
function openDrawer(){ensureAudio();document.getElementById('drawer').classList.add('open');document.getElementById('scrim').classList.add('open');}
function closeDrawer(){document.getElementById('drawer').classList.remove('open');document.getElementById('scrim').classList.remove('open');}
function toggleDrawer(){if(document.getElementById('drawer').classList.contains('open'))closeDrawer();else openDrawer();}
function hexFor(db){return db>=HARD?'#ff453a':db>=SOFT?'#ff9f0a':'#30d158';}
function rgba(hex,a){var n=parseInt(hex.slice(1),16);return 'rgba('+(n>>16)+','+((n>>8)&255)+','+(n&255)+','+a+')';}
var bar=document.getElementById('bar'), segs=[];
for(var i=0;i<10;i++){var s=document.createElement('div');s.className='seg';bar.appendChild(s);segs.push(s);}
function renderBar(lvl){
  var top=Math.max(GREEN+5, HARD+7);
  var lit=lvl<GREEN?0:Math.max(0,Math.min(10,Math.round((lvl-GREEN)/(top-GREEN)*10)));
  for(var i=0;i<10;i++){
    if(i<lit){var h=hexFor(GREEN+(i+0.5)/10*(top-GREEN));segs[i].style.background=rgba(h,.85);segs[i].style.borderColor=rgba(h,.5);}
    else{segs[i].style.background='rgba(255,255,255,.1)';segs[i].style.borderColor='rgba(255,255,255,.14)';}
  }
}
function setBanner(hex,title,sub){
  var ban=document.getElementById('banner');
  ban.style.background=rgba(hex,.15);ban.style.borderColor=rgba(hex,.55);
  var bt=document.getElementById('bt'),bd=document.getElementById('bd');
  bt.textContent=title;bt.style.color=hex;bd.textContent=sub;bd.style.color=rgba(hex,.85);
}
function setBannerNeutral(title,sub){
  var ban=document.getElementById('banner');
  ban.style.background='var(--glass)';ban.style.borderColor='var(--gline)';
  var bt=document.getElementById('bt'),bd=document.getElementById('bd');
  bt.textContent=title;bt.style.color='var(--g1)';bd.textContent=sub;bd.style.color='var(--g2)';
}
var sliderTouchedAt=0;
function setS(id,vid,val){document.getElementById(id).value=Math.round(val);document.getElementById(vid).textContent=Math.round(val)+' dB';}
function syncSliders(s){
  if(Date.now()-sliderTouchedAt<1500) return;
  setS('sgroen','vgroen',s.groen);setS('ssoft','vsoft',s.soft);
  setS('shard','vhard',s.hard);setS('sgain','vgain',s.gain_set);
}
['sgroen','ssoft','shard','sgain'].forEach(function(id){
  document.getElementById(id).addEventListener('input',function(){sliderTouchedAt=Date.now();});
});
function render(s){
  GREEN=s.groen;SOFT=s.soft;HARD=s.hard;
  var prim=s.active.length?s.active[0]:null;
  var lvl=prim?prim[1]:0;
  if(s.overload) lvl=Math.max(lvl,s.hard+10);
  if(s.connected===false) setBanner('#ff453a','SDR losgekoppeld','steek de dongle terug — verbindt automatisch');
  else if(s.overload) setBanner('#ff453a','Zeer sterk signaal dichtbij','zender vlakbij — overstuur');
  else if(s.alarm==2) setBanner('#ff453a','Activiteit',s.alarm_freq.toFixed(3)+' MHz   +'+Math.round(s.alarm_db)+' dB');
  else if(s.alarm==1) setBanner('#ff9f0a','Mogelijke activiteit',s.alarm_freq.toFixed(3)+' MHz   +'+Math.round(s.alarm_db)+' dB');
  else setBannerNeutral('Geen activiteit',s.status);
  renderBar(lvl);
  var db=document.getElementById('db'),freq=document.getElementById('freq'),trend=document.getElementById('trend');
  if(prim && lvl>=GREEN){
    var h=hexFor(lvl);
    db.textContent='+'+Math.round(lvl)+' dB';db.style.color=h;
    freq.textContent=prim[0].toFixed(3)+' MHz';freq.style.color=rgba(h,.8);
    var tr=prim[2];
    trend.textContent=tr>0?'▲ nadert':tr<0?'▼ gaat weg':'► stabiel';
    trend.style.color=tr>0?'#30d158':tr<0?'#ff9f0a':'var(--g2)';
  }else{
    db.textContent='—';db.style.color='var(--g1)';
    freq.textContent='—';freq.style.color='var(--g2)';
    trend.textContent='geen contact';trend.style.color='var(--g2)';
  }
  syncSliders(s);
  document.getElementById('m').textContent=s.mode;
  document.getElementById('bn').textContent=s.band.split(' ').slice(0,2).join(' ');
  document.getElementById('g').textContent=s.gainmode;
  document.getElementById('mu').textContent=s.muted?'gedempt':'aan';
}
var AC=null, nextBeep=0, lastLvl=0, lastSoft=18, lastHard=30, muted=false;
function ensureAudio(){if(!AC){try{AC=new (window.AudioContext||window.webkitAudioContext)();}catch(e){}}}
document.body.addEventListener('click',ensureAudio,{once:false});
function beep(lvl,hard){
  if(!AC) return;
  var t=AC.currentTime, o=AC.createOscillator(), g=AC.createGain();
  var pitch=600+Math.min(1,lvl/Math.max(1,hard))*900;
  o.frequency.value=pitch; o.type='square';
  g.gain.setValueAtTime(0.0001,t);
  g.gain.exponentialRampToValueAtTime(0.25,t+0.005);
  g.gain.exponentialRampToValueAtTime(0.0001,t+0.06);
  o.connect(g); g.connect(AC.destination);
  o.start(t); o.stop(t+0.07);
}
function geigerTick(){
  if(!muted && AC && lastLvl>=lastSoft){
    var now=AC.currentTime*1000;
    var span=Math.max(4,lastHard-lastSoft);
    var x=Math.max(0,(lastLvl-lastSoft)/span);
    var interval=Math.max(50, 800 - x*730);
    if(now>=nextBeep){beep(lastLvl,lastHard); nextBeep=now+interval;}
  }
  requestAnimationFrame(geigerTick);
}
requestAnimationFrame(geigerTick);
async function poll(){try{var r=await fetch('/state');var s=await r.json();
  lastSoft=s.soft; lastHard=s.hard; muted=s.muted;
  lastLvl=s.active.length?s.active[0][1]:0;
  if(s.overload) lastLvl=Math.max(lastLvl,s.hard+10);
  render(s);}catch(e){}}
async function cmd(a){ensureAudio();try{var r=await fetch('/cmd?action='+a,{method:'POST'});render(await r.json());}catch(e){}}
async function cmdVal(a,v){ensureAudio();try{var r=await fetch('/cmd?action='+a+'&value='+v,{method:'POST'});render(await r.json());}catch(e){}}
setInterval(poll,400);poll();
</script></body></html>"""


# ── HTTP-server ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    controller = None

    def _send(self, code, ctype, body, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Pagina/JSON nooit cachen, anders blijft een telefoon (zeker als
        # beginscherm-app) hangen op oude JavaScript en mist hij nieuwe functies.
        if not cache:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", PAGE.encode())
        elif self.path.startswith("/state"):
            self._send(200, "application/json", json.dumps(self.controller.state()).encode())
        elif self.path.startswith("/icon"):
            self._send(200, "image/png", ICON_PNG)
        elif self.path.startswith("/manifest"):
            self._send(200, "application/manifest+json", MANIFEST.encode())
        else:
            self._send(404, "text/plain", b"404")

    def do_POST(self):
        if self.path.startswith("/cmd"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.controller.cmd(q.get("action", [""])[0], q.get("value", [None])[0])
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

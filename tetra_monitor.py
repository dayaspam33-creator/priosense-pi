#!/usr/bin/env python3
"""
TetraMonitor — TETRA/C2000 activiteitsmonitor voor RTL-SDR Blog V3

Meet of er activiteit is in de TETRA-downlinkband (380–385 MHz) en zet dat
om in beeld: live spectrum, waterfall, activiteitsbalken per kanaal,
richting (nadert / gaat weg), geluidsalarm en CSV-logging.

Belangrijk: dit programma DECODEERT NIETS. Het meet alleen signaalsterkte
(energie boven de ruisvloer) om te laten zien DAT er activiteit is.

Hardware:  RTL-SDR Blog V3 + TETRA-antenne, via rtl_tcp.
Gebruik:   python3 tetra_monitor.py   (zie README.md voor opties)
"""

from __future__ import annotations

import argparse
import math
import os
import platform
import socket
import struct
import subprocess
import sys
import threading
import time
import wave
from collections import deque
from datetime import datetime

import numpy as np

from PyQt6.QtCore import Qt, QTimer, QRectF, QSettings, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QTransform
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)
import pyqtgraph as pg


# ── Instellingen ────────────────────────────────────────────────────────────
APP_NAME      = "TetraMonitor"

# TETRA-banden in NL (C2000) — vaste ETSI/CEPT-indeling, 10 MHz duplex:
#   Uplink   (portofoons/voertuigen zenden):  380–385 MHz  ← magneetantenne
#   Downlink (basisstations zenden continu):   390–395 MHz
# Met een magneetantenne in/bij de auto pik je vooral de UPLINK op: een eenheid
# die vlakbij zendt geeft daar een sterk, kortdurend signaal. De downlink staat
# juist continu aan (controlekanaal) en wijst op infrastructuur in de buurt.
# De Blog V3 haalt niet de hele 5 MHz in één keer binnen; we kijken naar ~3.2 MHz
# rond de center. Verschuif de center (banddropdown) om een ander stuk te zien.
BAND_LOW_MHZ  = 380.0
BAND_HIGH_MHZ = 385.0
DEFAULT_CENTER_MHZ = 382.5     # uplink-midden (nabije eenheden)

SAMPLE_RATE   = 3_200_000      # 3.2 MS/s: breder venster (Blog V3 aan; bij
                               # sample-drops eventueel terug naar 2_400_000)
FFT_SIZE      = 4096           # 0.78 kHz/bin: fijne resolutie + betere scheiding
                               # tussen naburige 25 kHz-kanalen (minder vals alarm)
CHANNEL_KHZ   = 25.0           # TETRA-kanaalraster: 25 kHz
WFALL_ROWS    = 120

# Detectie: per kanaal integreren we de energie over de volle 25 kHz (zoals
# professionele TETRA-sensoren) en drukken die uit als dB boven de ruisvloer.
DEFAULT_GAIN_DB   = 36.0       # Blog V3 + resonante TETRA-antenne in een actieve
                               # RF-omgeving: iets lager startpunt = minder kans op
                               # verzadiging vlakbij zenders (auto-reductie regelt bij)
NOISE_PERCENTILE  = 30         # ruisvloer (weergave) = 30e percentiel spectrum
SOFT_THRESHOLD_DB = 12.0       # oranje: waarschijnlijk activiteit
HARD_THRESHOLD_DB = 22.0       # rood: duidelijke, sterke activiteit

# CFAR (Constant False Alarm Rate): i.p.v. één globale ruisvloer schatten we de
# ruis LOKAAL rond elk kanaal (mediaanfilter over naburige kanalen). Zo past de
# drempel zich aan een scheve ruisvloer aan (band-randen, helling) → minder vals
# alarm en betere zwakke detectie.
CFAR_HALF_CHANS   = 12         # halve venstergrootte (kanalen) voor lokale ruis
CHAN_SMOOTH_A     = 0.20       # tijdmiddeling per kanaal (minder ruisvariantie)
# Piek-hold (burst-detectie): vangt korte registratiepulsjes van passerende
# voertuigen, zodat een puls van ~14 ms de drempel even haalt. Tijd-gebaseerde
# decay (tijdconstante in seconden) → framerate-onafhankelijk. De zichtbare
# persistentie regelt de hold/release hierboven; deze hold is kort.
PEAK_TAU          = 0.3        # s — hoe lang de detectiepiek nadreunt
# DC-spike: de RTL-SDR heeft altijd een neppiek op de centerfrequentie (LO-lek).
DC_NULL_BINS      = 2          # ± dit aantal bins rond center "dempen"
# Bezetting: een echte TETRA-draaggolf vult het kanaal breed; zit bijna alle
# energie in één bin, dan is het een smalle storing (birdie/CW) → negeren.
OCC_PEAK_FRAC     = 0.40       # één bin > 40% van kanaalenergie = smalle piek
                               # (gemeten: pure toon ~0.58, breed TETRA ~0.14)

# Rijmodi: gevoeligheids-presets (drempels in dB boven de lokale ruis).
#   Stad    = druk RF → minder gevoelig (minder vals alarm)
#   Snelweg = weinig signalen → gevoeliger (vangt zwakke/korte bursts)
#   Custom  = je eigen schuif-instelling
RIJMODI = [
    {"name": "Stad",    "soft": 14.0, "hard": 24.0},
    {"name": "Snelweg", "soft": 6.0,  "hard": 14.0},
    {"name": "Custom",  "soft": SOFT_THRESHOLD_DB, "hard": HARD_THRESHOLD_DB},
]
CUSTOM_IDX  = 2
MODE_COLORS = {"Stad": "orange", "Snelweg": "green", "Custom": "blue"}
WARMUP_FRAMES     = 60         # frames om de ruisvloer op te bouwen
HANG_TIME_S       = 4.0        # balk blijft staan (HOLD) na het laatste contact
RELEASE_DB_S      = 12.0       # daarna zakt de balk geleidelijk (dB per seconde)
TREND_HISTORY     = 12         # samples voor nadert/gaat-weg bepaling

# Auto-blacklist: een kanaal dat heel lang ononderbroken "actief" is, is bijna
# zeker een constante storing (TETRA-verkeer is juist kort/sporadisch). Dat
# kanaal wordt genegeerd tot het lang genoeg stil is (of handmatig gewist).
BLACKLIST_SECONDS   = 20.0     # zo lang continu actief → negeren
UNBLACKLIST_QUIET_S = 15.0     # zo lang stil → weer meedoen

# Oversturing (front-end overload): als een zender vlakbij staat klipt de dongle
# en valt de meting weg. Dat herkennen we aan de piek van de ruwe IQ-samples en
# behandelen we als "zeer sterk signaal dichtbij" → direct rood alarm.
OVERLOAD_CLIP     = 0.90       # gemiddelde clip-piek hierboven = oversturing
# "Waas": een zender vlakbij kan de front-end desensitiseren zonder dat de ADC
# hard klipt — de HELE ruisvloer tilt dan gelijkmatig omhoog (oranje waas over de
# waterfall). CFAR is relatief en ziet dat niet, dus meten we de absolute stijging
# van de ruisvloer t.o.v. de "normale" (stille) vloer.
HAZE_RISE_DB      = 10.0       # ruisvloer zoveel dB boven normaal = sterk-signaal
FLOOR_BASE_UP     = 0.00002    # baseline stijgt heel traag (~1 min) maar zakt snel,
                               # zodat een aanhoudende waas alarm blijft geven
LOG_COOLDOWN_S    = 10.0       # min. tijd tussen logregels per kanaal
SIREN_COOLDOWN_S  = 10.0

TCP_HOST = "127.0.0.1"

# rtl_tcp zoekpaden (Homebrew Intel + Apple Silicon + PATH)
_RTL_PATHS = [
    "/opt/homebrew/bin/rtl_tcp",
    "/usr/local/bin/rtl_tcp",
    "rtl_tcp",
]

def _script_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

LOG_PATH   = os.path.join(_script_dir(), "tetra_activiteit.csv")
SIREN_WAV  = os.path.join(_script_dir(), "_alarm.wav")


# ── Argumenten ──────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="TetraMonitor — TETRA activiteitsmonitor")
    p.add_argument("--center", type=float, default=DEFAULT_CENTER_MHZ,
                   help="Centerfrequentie in MHz (default 382.5)")
    p.add_argument("--gain", type=float, default=DEFAULT_GAIN_DB,
                   help="Tuner gain in dB (default 40)")
    p.add_argument("--ppm", type=int, default=0,
                   help="Frequentiecorrectie in ppm (default 0)")
    p.add_argument("--port", type=int, default=1234, help="rtl_tcp poort")
    p.add_argument("--device", type=int, default=0, help="Dongle index")
    p.add_argument("--extern", action="store_true",
                   help="rtl_tcp draait al; niet zelf starten/stoppen")
    return p.parse_args()


# ── Geluid ──────────────────────────────────────────────────────────────────
def _make_alarm_wav(path, rate=44100, volume=0.10):
    """Korte sirene: twee sweeps 800→1400→800 Hz."""
    frames, n = [], int(rate * 0.4)
    for _ in range(2):
        for i in range(n):
            f = 800 + 600 * i / n
            frames.append(struct.pack("<h", int(32767 * volume * math.sin(2 * math.pi * f * i / rate))))
        for i in range(n):
            f = 1400 - 600 * i / n
            frames.append(struct.pack("<h", int(32767 * volume * math.sin(2 * math.pi * f * i / rate))))
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(frames))

try:
    if not os.path.exists(SIREN_WAV):
        _make_alarm_wav(SIREN_WAV)
except OSError as e:
    print(f"[geluid] kon alarm niet aanmaken: {e}")

def play_alarm():
    try:
        if sys.platform == "darwin":
            os.system(f"afplay '{SIREN_WAV}' >/dev/null 2>&1")
        elif sys.platform == "win32":
            import winsound
            winsound.PlaySound(SIREN_WAV, winsound.SND_FILENAME)
        else:
            os.system(f"aplay '{SIREN_WAV}' 2>/dev/null || paplay '{SIREN_WAV}' 2>/dev/null")
    except Exception:
        pass


# ── Kleurenpalet ────────────────────────────────────────────────────────────
C = {
    "bg":     "#101216", "panel":  "#181b20", "panel2": "#23272e",
    "sep":    "#2c313a", "blue":   "#3aa0ff", "green":  "#34d27b",
    "yellow": "#ffcc33", "red":    "#ff4d4d", "orange": "#ff9933",
    "white":  "#f2f4f8", "gray1":  "#c7cdd6", "gray2":  "#8a92a0",
    "gray3":  "#4a515c",
}
def qc(k): return QColor(C[k])

def sys_font(size, bold=False):
    f = QFont()
    name = platform.system()
    f.setFamily(".AppleSystemUIFont" if name == "Darwin"
                else "Segoe UI" if name == "Windows" else "Sans Serif")
    f.setPointSize(size)
    if bold:
        f.setWeight(QFont.Weight.Bold)
    return f


# ── rtl_tcp bron ────────────────────────────────────────────────────────────
def _send_cmd(sock, cmd, param):
    sock.sendall(struct.pack(">BI", cmd, param & 0xFFFFFFFF))

class RtlTcpSource:
    """Beheert het rtl_tcp-proces en de TCP-verbinding naar de dongle."""

    def __init__(self, args):
        self.host = TCP_HOST
        self.port = args.port
        self.device = args.device
        self.extern = args.extern
        self.ppm = args.ppm
        self.center_hz = int(round(args.center * 1e6))
        self.gain_db = args.gain
        self.auto_gain = False
        self._sock = None
        self._proc = None

    def _rtl_path(self):
        return next((p for p in _RTL_PATHS if os.path.exists(p)), _RTL_PATHS[-1])

    def connect(self):
        if not self.extern:
            if sys.platform != "win32":
                os.system("pkill rtl_tcp 2>/dev/null")
                time.sleep(0.4)
            path = self._rtl_path()
            if os.path.exists(path) or path == "rtl_tcp":
                flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
                self._proc = subprocess.Popen(
                    [path, "-a", self.host, "-p", str(self.port),
                     "-d", str(self.device), "-f", str(self.center_hz),
                     "-s", str(SAMPLE_RATE)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=flags)
                threading.Thread(target=self._drain, daemon=True).start()
                time.sleep(2.5)
        self._open_socket()

    def _open_socket(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(5)
        self._sock.connect((self.host, self.port))
        self._sock.settimeout(2)
        try:
            self._sock.recv(12)   # dongle-info header
        except Exception:
            pass
        _send_cmd(self._sock, 0x01, self.center_hz)
        _send_cmd(self._sock, 0x02, SAMPLE_RATE)
        _send_cmd(self._sock, 0x05, self.ppm)
        self.apply_gain()

    def reconnect(self):
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._open_socket()

    def apply_gain(self):
        if not self._sock:
            return
        try:
            if self.auto_gain:
                _send_cmd(self._sock, 0x03, 0)
            else:
                _send_cmd(self._sock, 0x03, 1)
                _send_cmd(self._sock, 0x04, int(self.gain_db * 10))
        except Exception:
            pass

    def set_center(self, hz):
        self.center_hz = int(hz)
        if self._sock:
            try:
                _send_cmd(self._sock, 0x01, self.center_hz)
            except Exception:
                pass

    def recv(self, n):
        return self._sock.recv(n)

    def _drain(self):
        try:
            for line in self._proc.stdout:
                t = line.decode(errors="replace").rstrip()
                if t:
                    print(f"[rtl_tcp] {t}")
        except Exception:
            pass

    def close(self):
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        if self._proc and not self.extern:
            self._proc.terminate()


# ── Detector ────────────────────────────────────────────────────────────────
class Channel:
    __slots__ = ("freq", "level", "peak", "hang_until", "history",
                 "active_since", "quiet_since", "blacklisted")
    def __init__(self, freq):
        self.freq = freq
        self.level = 0.0           # huidig niveau boven ruisvloer (dB)
        self.peak = 0.0            # hoogste recente niveau (dB)
        self.hang_until = 0.0
        self.history = deque(maxlen=TREND_HISTORY)
        self.active_since = 0.0    # begin van huidige aaneengesloten activiteit
        self.quiet_since = 0.0     # sinds wanneer stil (voor un-blacklist)
        self.blacklisted = False   # constante storing → negeren

class Detector(threading.Thread):
    """Leest IQ van rtl_tcp, berekent per-kanaal activiteit boven de ruisvloer."""

    def __init__(self, source: RtlTcpSource):
        super().__init__(daemon=True)
        self.src = source
        self.running = False
        self.lock = threading.Lock()

        # Blackman: lagere zijlobben dan Hanning → betere scheiding van naburige
        # kanalen, zodat een sterk signaal niet "lekt" naar de buren.
        self.window = np.blackman(FFT_SIZE).astype(np.float32)
        self.freqs = self._calc_freqs(source.center_hz)
        self.power = np.full(FFT_SIZE, -90.0)
        self.noise_floor = -70.0           # weergave (amplitude-dB, oranje lijn)
        self.ch_avg = None                 # tijd-gemiddelde energie per kanaal
        self.ch_peak = None                # piek-hold per kanaal (burst-detectie)
        self._dc_bin = FFT_SIZE // 2       # center-bin (DC-spike) na fftshift
        self.wfall = np.full((WFALL_ROWS, FFT_SIZE), -90.0)

        self.soft_thr = SOFT_THRESHOLD_DB
        self.hard_thr = HARD_THRESHOLD_DB
        self.muted = False

        # Auto gain-reductie: bij oversturing (clipping) gain omlaag, bij ruimte
        # weer terug omhoog tot agc_max (= de door de gebruiker ingestelde gain).
        self.auto_gain_reduction = False
        self.agc_max = source.gain_db
        self.clip_peak = 0.0           # 1.0 = tegen clipping aan (per frame)
        self.clip_avg = 0.2            # gladgestreken clip-piek (voor oversturing)
        self.overload = False          # front-end overstuur via harde clipping
        self.floor_baseline = None     # geleerde "normale" (stille) ruisvloer
        self.haze = False              # brede oversturing (ruisvloer opgetild)
        self.haze_db = 0.0             # hoeveel dB de vloer boven normaal staat
        self._agc_last = 0.0

        self.auto_blacklist = True     # constante storingskanalen negeren
        self.channels: dict[float, Channel] = {}
        self.status = "Opstarten…"
        self.n_frames = 0
        self.alarm_level = 0       # 0 = stil, 1 = oranje, 2 = rood
        self.alarm_freq = 0.0
        self.alarm_db = 0.0
        self._alarm_until = 0.0
        self._prev_level = 0       # voor flank-detectie (geschiedenis/sirene)
        self._last_t = None        # vorige frame-tijd (voor release-snelheid)

        self._last_log = {}
        self._last_siren = 0.0
        self.on_detection = None   # callback(freq, db, level) — gezet door GUI

        self._chan_idx = []
        self._build_channels()

    # ── frequentie-helpers ──
    def _calc_freqs(self, center_hz):
        return np.linspace((center_hz - SAMPLE_RATE / 2) / 1e6,
                           (center_hz + SAMPLE_RATE / 2) / 1e6, FFT_SIZE)

    def _build_channels(self):
        """Bepaal één keer per afstemming de bin-indices van elk 25 kHz-kanaal,
        zodat de energie-integratie per frame goedkoop blijft."""
        step = CHANNEL_KHZ / 1000.0
        half = step / 2.0
        lo, hi = self.freqs[0], self.freqs[-1]
        start = math.ceil(lo / step) * step
        chans = []
        for cf in np.arange(start, hi, step):
            idx = np.where((self.freqs >= cf - half) & (self.freqs < cf + half))[0]
            if idx.size:
                chans.append((round(float(cf), 4), idx))
        self._chan_idx = chans

    def retune(self, center_mhz):
        hz = int(round(center_mhz * 1e6))
        self.src.set_center(hz)
        with self.lock:
            self.freqs = self._calc_freqs(hz)
            self._build_channels()
            self.channels.clear()
            self.n_frames = 0
            self.noise_floor = -70.0
            self.ch_avg = None
            self.ch_peak = None
            self.floor_baseline = None
            self.haze = False

    def reset_noise_floor(self):
        with self.lock:
            self.n_frames = 0
            self.channels.clear()

    def clear_blacklist(self):
        with self.lock:
            for ch in self.channels.values():
                ch.blacklisted = False
                ch.active_since = 0.0

    def blacklist_count(self):
        return sum(1 for ch in self.channels.values() if ch.blacklisted)

    # ── hoofdlus ──
    def run(self):
        self.running = True
        buf = bytearray()
        need = FFT_SIZE * 2
        while self.running:
            try:
                chunk = self.src.recv(8192)
                if not chunk:
                    self._reconnect()
                    buf.clear(); continue
                buf.extend(chunk)
                while len(buf) >= need:
                    raw = bytes(buf[:need]); del buf[:need]
                    self._process(raw)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[detector] {e}")
                self._reconnect()
                buf.clear()

    def _agc_step(self, peak, now):
        """Automatische gain-reductie met hysterese en cooldown."""
        if now - self._agc_last < 0.4:
            return
        g = self.src.gain_db
        if peak >= 0.99 and g > 0:                # harde clipping → grote stap
            self.src.gain_db = max(0.0, g - 6.0)
            self.src.apply_gain()
            self._agc_last = now
            self.n_frames = 0                     # ruisvloer opnieuw inregelen
        elif (peak > 0.95 or self.haze) and g > 0:  # oversturing of brede waas → omlaag
            self.src.gain_db = max(0.0, g - 3.0)
            self.src.apply_gain()
            self._agc_last = now
            self.n_frames = 0
        elif peak < 0.5 and not self.haze and g < self.agc_max:  # ruim onder → omhoog
            self.src.gain_db = min(self.agc_max, g + 1.0)
            self.src.apply_gain()
            self._agc_last = now
            self.n_frames = 0

    def _reconnect(self):
        self.status = "Herverbinden…"
        while self.running:
            try:
                self.src.reconnect()
                with self.lock:
                    self.n_frames = 0
                    self.channels.clear()
                self.status = "Herverbonden"
                return
            except Exception as e:
                print(f"[detector] herverbinden mislukt: {e}")
                time.sleep(3)

    def _process(self, raw):
        iq = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 127.5) / 127.5
        self.clip_peak = float(np.abs(iq).max())   # 1.0 = tegen clipping aan
        # Gladgestreken clip-piek: één ruisspikkel telt niet, aanhoudende
        # oversturing (zender vlakbij) wél.
        self.clip_avg = 0.85 * self.clip_avg + 0.15 * self.clip_peak
        self.overload = self.clip_avg > OVERLOAD_CLIP
        samples = (iq[0::2] + 1j * iq[1::2]) * self.window
        spec = np.fft.fftshift(np.abs(np.fft.fft(samples, FFT_SIZE)))
        lin = (spec / FFT_SIZE) ** 2 + 1e-20         # lineair vermogen per bin
        # DC-spike (LO-lek op de centerfrequentie) dempen: vervang de centerbins
        # door de lokale mediaan, zodat hij geen vals signaal op center geeft.
        dc = self._dc_bin
        ref = np.concatenate([lin[dc - 9:dc - 3], lin[dc + 4:dc + 10]])
        if ref.size:
            lin[dc - DC_NULL_BINS:dc + DC_NULL_BINS + 1] = np.median(ref)
        power = 10.0 * np.log10(lin)                 # dB (== 20·log10(amplitude))
        now = time.time()

        if self.auto_gain_reduction:
            self._agc_step(self.clip_peak, now)

        with self.lock:
            self.wfall = np.roll(self.wfall, 1, axis=0)
            self.wfall[0] = power
            self.power = power
            self.n_frames += 1

            # Energie per kanaal (integratie over de volle 25 kHz).
            ch_energy = np.array([lin[idx].sum() for _, idx in self._chan_idx])

            # noise_floor (dB) is alleen voor de oranje weergavelijn.
            nf_now = float(np.percentile(power, NOISE_PERCENTILE))
            if self.n_frames <= WARMUP_FRAMES:
                a = 0.1
                self.noise_floor = nf_now if self.n_frames == 1 else \
                    (1 - a) * self.noise_floor + a * nf_now
                self.ch_avg = ch_energy if self.ch_avg is None else \
                    (1 - a) * self.ch_avg + a * ch_energy
                self.ch_peak = ch_energy.copy() if self.ch_peak is None else \
                    np.maximum(ch_energy, self.ch_peak)
                self.floor_baseline = self.noise_floor
                self.status = f"Ruisvloer meten  {int(100 * self.n_frames / WARMUP_FRAMES)}%"
                self.alarm_level = 0
                return
            self.noise_floor = 0.995 * self.noise_floor + 0.005 * nf_now
            # "Waas"-detectie: baseline volgt de stille vloer snel omlaag, maar
            # heel langzaam omhoog. Tilt de hele vloer plots op (zender vlakbij),
            # dan blijft de baseline laag en onthult het gat de oversturing.
            if self.noise_floor < self.floor_baseline:
                self.floor_baseline = self.noise_floor
            else:
                self.floor_baseline += FLOOR_BASE_UP * (self.noise_floor - self.floor_baseline)
            self.haze_db = self.noise_floor - self.floor_baseline
            self.haze = self.haze_db > HAZE_RISE_DB
            # Tijd sinds vorige frame (voor framerate-onafhankelijke ballistiek).
            dt = 0.0 if self._last_t is None else min(0.5, now - self._last_t)
            self._last_t = now

            # Tijdmiddeling per kanaal (minder ruisvariantie → minder vals alarm).
            self.ch_avg = (1 - CHAN_SMOOTH_A) * self.ch_avg + CHAN_SMOOTH_A * ch_energy
            # Piek-hold per kanaal: vangt korte registratiepulsjes (passerend
            # voertuig dat niet praat), zodat een burst van ~14 ms de drempel haalt.
            self.ch_peak = np.maximum(ch_energy, self.ch_peak * math.exp(-dt / PEAK_TAU))
            self.status = "Scannen"

            # CFAR: lokale ruis uit de stabiele gemiddelde energie (mediaan buren).
            local = self._cfar(self.ch_avg)
            # Detectieniveau = het hoogste van: langere transmissie (gemiddelde) en
            # korte burst (piek). Zo zien we beide.
            level_avg = 10.0 * np.log10(self.ch_avg / local)
            level_peak = 10.0 * np.log10(self.ch_peak / local)
            levels = np.maximum(level_avg, level_peak)
            self._detect(levels, lin, now, dt)

    @staticmethod
    def _cfar(ch_avg):
        """Lokale ruis per kanaal via een gecentreerd mediaanfilter over de
        naburige kanalen (robuust tegen losse sterke kanalen = OS-CFAR-achtig)."""
        h = CFAR_HALF_CHANS
        padded = np.pad(ch_avg, h, mode="edge")
        win = np.lib.stride_tricks.sliding_window_view(padded, 2 * h + 1)
        return np.median(win, axis=1)

    def _detect(self, levels, lin, now, dt):
        best_freq, best_db = 0.0, 0.0

        for i, (cf_key, idx) in enumerate(self._chan_idx):
            level = float(levels[i])     # dB t.o.v. de lokale (CFAR-)ruis
            ch = self.channels.get(cf_key)
            if ch is None:
                ch = Channel(cf_key)
                self.channels[cf_key] = ch
            ch.history.append(level)

            contact = False
            if level > self.soft_thr:
                # Bezettingscheck: zit bijna alle energie in één bin, dan is het
                # een smalle storing (birdie/CW), geen breed TETRA-signaal.
                seg = lin[idx]
                seg_sum = float(seg.sum())
                is_spike = seg_sum > 0 and float(seg.max()) / seg_sum > OCC_PEAK_FRAC
                if is_spike:
                    ch.active_since = 0.0          # smalle piek telt niet als contact
                else:
                    if ch.active_since == 0.0:
                        ch.active_since = now
                    ch.quiet_since = 0.0
                    # Te lang ononderbroken actief = constante storing → blacklist.
                    if (self.auto_blacklist and not ch.blacklisted
                            and now - ch.active_since > BLACKLIST_SECONDS):
                        ch.blacklisted = True
                    contact = not ch.blacklisted
            else:
                ch.active_since = 0.0
                if ch.quiet_since == 0.0:
                    ch.quiet_since = now
                if ch.blacklisted and now - ch.quiet_since > UNBLACKLIST_QUIET_S:
                    ch.blacklisted = False

            # Balk-ballistiek (piek-meter): bij contact springt de balk naar de
            # piek (attack) en houdt die HANG_TIME_S vast (hold); daarna zakt hij
            # geleidelijk (release). Nieuw contact zet 'm meteen weer vol → de
            # balk blijft staan tussen pulsjes door i.p.v. te flikkeren.
            if contact:
                if level > ch.level:
                    ch.level = level                # attack: naar de piek
                ch.hang_until = now + HANG_TIME_S
                if level > best_db:
                    best_db, best_freq = level, cf_key
                self._log(cf_key, level)
            elif ch.blacklisted:
                ch.level = 0.0
            elif now <= ch.hang_until:
                pass                                # HOLD: piek blijft staan
            elif ch.level > 0.0:
                ch.level = max(0.0, ch.level - RELEASE_DB_S * dt)   # RELEASE

        # Alarmniveau bepalen. Oversturing/waas eerst: staat er iets zeer sterks
        # vlakbij (harde clipping óf opgetilde ruisvloer), dan juist rood alarm
        # i.p.v. stil vallen — precies de situatie vóór een politiebureau.
        if self.overload or self.haze:
            lvl, afreq, adb = 2, best_freq, max(best_db, self.hard_thr)
            self._alarm_until = now + 2.0
        elif best_db >= self.hard_thr:
            lvl, afreq, adb = 2, best_freq, best_db
            self._alarm_until = now + 2.0
        elif best_db >= self.soft_thr:
            lvl, afreq, adb = 1, best_freq, best_db
            self._alarm_until = now + 2.0
        elif now < self._alarm_until:
            # Korte nahang ná de laatste echte detectie — venster NIET verlengen.
            lvl, afreq, adb = self.alarm_level, self.alarm_freq, self.alarm_db
        else:
            lvl, afreq, adb = 0, 0.0, 0.0

        # Alleen op een stijgende flank loggen/piepen (anders loopt alles vol).
        if lvl > self._prev_level and lvl >= 1 and afreq > 0 and self.on_detection:
            self.on_detection(afreq, adb, lvl)
        if lvl == 2 and not self.muted and now - self._last_siren >= SIREN_COOLDOWN_S:
            self._last_siren = now
            threading.Thread(target=play_alarm, daemon=True).start()

        self.alarm_level, self.alarm_freq, self.alarm_db = lvl, afreq, adb
        self._prev_level = lvl

    def _log(self, freq, level):
        now = time.time()
        if now - self._last_log.get(freq, 0) < LOG_COOLDOWN_S:
            return
        self._last_log[freq] = now
        try:
            new = not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                if new:
                    f.write("tijd,frequentie_mhz,niveau_db\n")
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"{ts},{freq:.4f},{level:.1f}\n")
        except OSError:
            pass

    def snapshot(self):
        """Thread-veilige kopie van wat de GUI nodig heeft."""
        with self.lock:
            active = sorted(
                ((ch.freq, ch.level, self._trend(ch))
                 for ch in self.channels.values() if ch.level > 0),
                key=lambda x: -x[1])
            return {
                "power": self.power.copy(),
                "noise_floor": self.noise_floor,
                "freqs": self.freqs.copy(),
                "wfall": self.wfall.copy(),
                "status": self.status,
                "alarm_level": self.alarm_level,
                "alarm_freq": self.alarm_freq,
                "alarm_db": self.alarm_db,
                "active": active,
                "gain": self.src.gain_db,
                "clip_peak": self.clip_peak,
                "agc": self.auto_gain_reduction,
                "overload": self.overload or self.haze,
                "haze_db": self.haze_db if self.haze else 0.0,
                "blacklist": sum(1 for ch in self.channels.values() if ch.blacklisted),
            }

    @staticmethod
    def _trend(ch):
        h = list(ch.history)
        if len(h) < 6:
            return 0
        recent = sum(h[-3:]) / 3
        older = sum(h[:3]) / 3
        if recent - older > 2.5:
            return 1
        if recent - older < -2.5:
            return -1
        return 0

    def stop(self):
        self.running = False


# ── Activiteitsbanner ───────────────────────────────────────────────────────
class StatusBanner(QFrame):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(96)
        self._level = 0
        self._freq = 0.0
        self._db = 0.0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 12, 20, 12)
        lay.setSpacing(2)
        self.title = QLabel("● GEEN ACTIVITEIT")
        self.title.setFont(sys_font(20, bold=True))
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail = QLabel("Ruisvloer aan het meten…")
        self.detail.setFont(sys_font(12))
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.title)
        lay.addWidget(self.detail)
        self._apply(0)

    def update_state(self, level, freq, db, status, overload=False):
        if level != self._level or freq != self._freq:
            self._apply(level)
        self._level, self._freq, self._db = level, freq, db
        if overload:
            self.title.setText("🚨 ZEER STERK SIGNAAL DICHTBIJ")
            self.detail.setText("Zender vlakbij — front-end overstuurt")
        elif level == 0:
            self.title.setText("● GEEN ACTIVITEIT")
            self.detail.setText(status)
        elif level == 1:
            self.title.setText("◆ MOGELIJKE ACTIVITEIT")
            self.detail.setText(f"{freq:.4f} MHz   +{db:.0f} dB boven ruis")
        else:
            self.title.setText("🚨 ACTIVITEIT GEDETECTEERD")
            self.detail.setText(f"{freq:.4f} MHz   +{db:.0f} dB boven ruis")

    def _apply(self, level):
        bg, border, col = {
            0: (C["panel"], C["sep"], C["gray2"]),
            1: ("#2a1f00", C["orange"], C["orange"]),
            2: ("#2d0b0b", C["red"], C["red"]),
        }[level]
        self.setStyleSheet(
            f"StatusBanner {{ background:{bg}; border:2px solid {border}; border-radius:14px; }}")
        self.title.setStyleSheet(f"color:{col}; background:transparent;")
        self.detail.setStyleSheet(f"color:{C['gray1']}; background:transparent;")


# ── Kanaalbalken ────────────────────────────────────────────────────────────
class ChannelBars(QWidget):
    """Top-actieve kanalen als grote verticale balken met richting-indicator.
    Elke balk = één zendende eenheid (voertuig/portofoon) op z'n eigen kanaal."""
    N_BARS = 3
    N_SEG = 14

    def __init__(self):
        super().__init__()
        self._active = []
        self._total = 0
        self._soft = SOFT_THRESHOLD_DB
        self._hard = HARD_THRESHOLD_DB
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def update_data(self, active, soft, hard):
        self._total = len(active)               # totaal aantal actieve eenheden
        self._active = active[:self.N_BARS]
        self._soft, self._hard = soft, hard
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, qc("panel"))

        p.setFont(sys_font(12, bold=True))
        if self._total > 0:
            p.setPen(qc("green"))
            title = f"ACTIEVE EENHEDEN · {self._total}"
            if self._total > self.N_BARS:
                title += f"  (sterkste {self.N_BARS})"
        else:
            p.setPen(qc("gray2"))
            title = "ACTIEVE EENHEDEN"
        p.drawText(0, 4, W, 28, int(Qt.AlignmentFlag.AlignCenter), title)

        top = 38
        label_h = 76
        bars_h = H - top - label_h
        seg_gap = 4
        sect_w = W / self.N_BARS
        seg_w = sect_w * 0.42
        seg_h = (bars_h - (self.N_SEG - 1) * seg_gap) / self.N_SEG
        full = max(1.0, self._hard + 6.0)   # dB-schaal voor volle balk

        # Kleuren naar verhouding van het aantal segmenten (groen-geel-rood).
        n_green = round(self.N_SEG * 0.42)
        n_yellow = round(self.N_SEG * 0.33)
        n_red = self.N_SEG - n_green - n_yellow
        on = [qc("green")] * n_green + [qc("yellow")] * n_yellow + [qc("red")] * n_red
        off = ([QColor("#10261a")] * n_green + [QColor("#26220c")] * n_yellow
               + [QColor("#2a1010")] * n_red)

        for i in range(self.N_BARS):
            cx = sect_w * i + sect_w / 2
            x = cx - seg_w / 2
            if i < len(self._active):
                freq, level, trend = self._active[i]
                n_lit = int(min(self.N_SEG, max(0, level / full * self.N_SEG)))
                col = qc("red") if level >= self._hard else qc("yellow") if level >= self._soft else qc("green")
            else:
                freq, level, trend, n_lit, col = None, 0.0, 0, 0, qc("gray3")

            for s in range(self.N_SEG):
                li = self.N_SEG - 1 - s
                y = top + s * (seg_h + seg_gap)
                rect = QRectF(x, y, seg_w, seg_h)
                path = QPainterPath(); path.addRoundedRect(rect, 3, 3)
                p.fillPath(path, on[li] if li < n_lit else off[li])

            lx = int(cx - sect_w / 2)
            lw = int(sect_w)
            ly = H - label_h + 6
            if freq is not None:
                p.setFont(sys_font(15, bold=True)); p.setPen(col)
                p.drawText(lx, ly, lw, 26,
                           int(Qt.AlignmentFlag.AlignCenter), f"{freq:.3f} MHz")
                p.setFont(sys_font(13, bold=True)); p.setPen(qc("gray1"))
                p.drawText(lx, ly + 26, lw, 22,
                           int(Qt.AlignmentFlag.AlignCenter), f"+{level:.0f} dB")
                arrow, ac = (("▲ nadert", qc("green")) if trend == 1 else
                             ("▼ gaat weg", qc("orange")) if trend == -1 else
                             ("► stabiel", qc("gray2")))
                p.setFont(sys_font(12, bold=True)); p.setPen(ac)
                p.drawText(lx, ly + 49, lw, 22,
                           int(Qt.AlignmentFlag.AlignCenter), arrow)
            else:
                p.setFont(sys_font(16, bold=True)); p.setPen(qc("gray3"))
                p.drawText(lx, ly, lw, 28,
                           int(Qt.AlignmentFlag.AlignCenter), "—")
        p.end()


# ── Geschiedenislijst ───────────────────────────────────────────────────────
class HistoryList(QWidget):
    def __init__(self):
        super().__init__()
        self._rows = []   # (tijd, freq, db, level)
        self.setMinimumWidth(220)

    def add(self, freq, db, level):
        self._rows.insert(0, (datetime.now().strftime("%H:%M:%S"), freq, db, level))
        del self._rows[40:]
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, qc("panel"))
        p.setFont(sys_font(9, bold=True)); p.setPen(qc("gray2"))
        p.drawText(0, 0, W, 24, int(Qt.AlignmentFlag.AlignCenter), "GESCHIEDENIS")
        if not self._rows:
            p.setFont(sys_font(9)); p.setPen(qc("gray3"))
            p.drawText(0, 24, W, H - 24, int(Qt.AlignmentFlag.AlignCenter),
                       "Nog geen activiteit")
            p.end(); return
        row_h, y = 26, 28
        for ts, freq, db, level in self._rows:
            if y + row_h > H:
                break
            bg = QColor("#2d0b0b") if level == 2 else QColor("#2a1f00")
            p.fillRect(3, y, W - 6, row_h - 2, bg)
            dot = qc("red") if level == 2 else qc("orange")
            p.setBrush(dot); p.setPen(dot)
            p.drawEllipse(9, y + row_h // 2 - 4, 8, 8)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setFont(sys_font(8)); p.setPen(qc("gray2"))
            p.drawText(24, y, 60, row_h, int(Qt.AlignmentFlag.AlignVCenter), ts)
            p.setFont(sys_font(8, bold=True)); p.setPen(qc("white"))
            p.drawText(86, y, 90, row_h, int(Qt.AlignmentFlag.AlignVCenter), f"{freq:.3f} MHz")
            p.setPen(dot)
            p.drawText(W - 56, y, 50, row_h,
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                       f"+{db:.0f}")
            y += row_h
        p.end()


# ── Schuifregelaar met label ────────────────────────────────────────────────
class Slider(QWidget):
    changed = pyqtSignal(float)

    def __init__(self, label, lo, hi, init, step=1.0, fmt="{:.0f}", color=None):
        super().__init__()
        self.lo, self.hi, self.step, self.fmt = lo, hi, step, fmt
        color = color or C["blue"]
        v = QVBoxLayout(self); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(3)
        row = QHBoxLayout()
        name = QLabel(label); name.setFont(sys_font(9)); name.setStyleSheet(f"color:{C['gray2']};")
        self.val = QLabel(fmt.format(init)); self.val.setFont(sys_font(9, bold=True))
        self.val.setStyleSheet(f"color:{C['gray1']};")
        self.val.setAlignment(Qt.AlignmentFlag.AlignRight)
        row.addWidget(name); row.addWidget(self.val); v.addLayout(row)
        self.s = QSlider(Qt.Orientation.Horizontal)
        self.s.setRange(0, round((hi - lo) / step))
        self.s.setValue(round((init - lo) / step))
        self.s.setStyleSheet(f"""
            QSlider::groove:horizontal {{ height:4px; background:{C['sep']}; border-radius:2px; }}
            QSlider::sub-page:horizontal {{ background:{color}; border-radius:2px; }}
            QSlider::handle:horizontal {{ background:{color}; width:14px; margin:-5px 0; border-radius:7px; }}
            QSlider::handle:horizontal:hover {{ background:white; }}
        """)
        self.s.valueChanged.connect(self._emit)
        v.addWidget(self.s)

    def _emit(self, sv):
        val = self.lo + sv * self.step
        self.val.setText(self.fmt.format(val))
        self.changed.emit(val)

    def value(self):
        return self.lo + self.s.value() * self.step

    def set_value(self, val):
        # Stelt de schuif in zonder changed-signaal (label wel bijwerken).
        self.s.blockSignals(True)
        self.s.setValue(round((val - self.lo) / self.step))
        self.s.blockSignals(False)
        self.val.setText(self.fmt.format(val))


# ── Hoofdvenster ────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, detector: Detector):
        super().__init__()
        self.det = detector
        self.det.on_detection = self._on_detection
        self._pending = []   # detecties uit detector-thread, in GUI-thread verwerkt

        # Bewaarde instellingen laden en toepassen vóór de UI wordt opgebouwd,
        # zodat schuiven/dropdowns meteen op de juiste waarde starten.
        self._settings = QSettings(APP_NAME, APP_NAME)
        s = self._load_settings()
        self.det.soft_thr      = s["soft_thr"]
        self.det.hard_thr      = s["hard_thr"]
        self.det.muted         = s["muted"]
        self.det.src.gain_db   = s["gain"]
        self.det.agc_max       = s["gain"]
        self._gain_mode        = s["gain_mode"]
        self._init_band_idx    = s["band_idx"]
        self._mode_idx         = s["mode_idx"]
        self._custom           = {"soft": s["custom_soft"], "hard": s["custom_hard"]}

        self.setWindowTitle(f"{APP_NAME} — TETRA activiteitsmonitor")
        self.setMinimumSize(1080, 680)
        self.setStyleSheet(f"QMainWindow, QWidget {{ background:{C['bg']}; color:{C['gray1']}; }}")

        root = QWidget(); self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 10, 12, 10); outer.setSpacing(10)

        self.banner = StatusBanner()
        outer.addWidget(self.banner)

        body = QHBoxLayout(); body.setSpacing(12)
        outer.addLayout(body, stretch=1)

        # Linkerkolom: spectrum + waterfall
        left = QVBoxLayout(); left.setSpacing(8)
        self.spec = pg.PlotWidget()
        self.spec.setBackground(C["panel"])
        self.spec.showGrid(x=True, y=True, alpha=0.1)
        self.spec.setLabel("left", "dB"); self.spec.setLabel("bottom", "MHz")
        self.spec.setMouseEnabled(x=False, y=False)
        self.spec.setYRange(-90, -20)
        self.spec.getAxis("left").setTextPen(qc("gray2"))
        self.spec.getAxis("bottom").setTextPen(qc("gray2"))
        self.curve = self.spec.plot(self.det.freqs, self.det.power,
                                    pen=pg.mkPen(C["blue"], width=1.6))
        nf_pen = pg.mkPen(C["orange"], width=1.0); nf_pen.setStyle(Qt.PenStyle.DashLine)
        self.nf_line = pg.InfiniteLine(angle=0, pen=nf_pen)
        self.spec.addItem(self.nf_line)
        left.addWidget(self.spec, stretch=2)

        self.wfall = pg.PlotWidget()
        self.wfall.setBackground(C["panel"])
        self.wfall.setLabel("bottom", "MHz"); self.wfall.setLabel("left", "tijd")
        self.wfall.setMouseEnabled(x=False, y=False)
        self.wfall.getAxis("left").setTextPen(qc("gray2"))
        self.wfall.getAxis("bottom").setTextPen(qc("gray2"))
        self.img = pg.ImageItem()
        self.img.setColorMap(pg.colormap.get("inferno"))
        self.img.setLevels((-90, -30))
        self.wfall.addItem(self.img)
        self._apply_wfall_transform()
        left.addWidget(self.wfall, stretch=2)

        # Grote balken direct onder de waterfall (volle breedte linkerkolom).
        self.bars = ChannelBars()
        left.addWidget(self.bars, stretch=3)
        body.addLayout(left, stretch=3)

        # Rechterkolom: geschiedenis, regelaars
        right = QVBoxLayout(); right.setSpacing(8)
        right.setContentsMargins(0, 0, 0, 0)
        rw = QWidget(); rw.setFixedWidth(300)
        rw.setLayout(right)

        self.history = HistoryList()
        right.addWidget(self.history, stretch=1)

        self.sl_gain = Slider("Gain (dB)", 0, 49, self.det.src.gain_db, color=C["blue"])
        self.sl_gain.changed.connect(self._on_gain)
        right.addWidget(self._panel(self.sl_gain))

        # Rijmodus-knop: cyclet Stad → Snelweg → Custom (zet de drempels).
        self.btn_mode = QPushButton()
        self.btn_mode.clicked.connect(self._cycle_mode)
        right.addWidget(self.btn_mode)

        self.sl_soft = Slider("Drempel oranje (dB)", 3, 50, self.det.soft_thr, color=C["orange"])
        self.sl_soft.changed.connect(self._on_soft)
        right.addWidget(self._panel(self.sl_soft))

        self.sl_hard = Slider("Drempel rood (dB)", 8, 70, self.det.hard_thr, color=C["red"])
        self.sl_hard.changed.connect(self._on_hard)
        right.addWidget(self._panel(self.sl_hard))

        self.band = QComboBox()
        self.band.setStyleSheet(
            f"QComboBox {{ background:{C['panel2']}; color:{C['gray1']}; "
            f"border:1px solid {C['sep']}; border-radius:5px; padding:4px 8px; }}")
        self._bands = [("Uplink 379.9–383.1 (laag)", 381.5),
                       ("Uplink 380.9–384.1 (midden)", 382.5),
                       ("Uplink 381.9–385.1 (hoog)", 383.5),
                       ("Downlink 389.9–393.1 (laag)", 391.5),
                       ("Downlink 390.9–394.1 (midden)", 392.5),
                       ("Downlink 391.9–395.1 (hoog)", 393.5)]
        for name, _ in self._bands:
            self.band.addItem(name)
        self.band.setCurrentIndex(self._init_band_idx)
        self.band.currentIndexChanged.connect(self._on_band)
        right.addWidget(self.band)

        # Gain-modus dropdown
        self.gain_mode = QComboBox()
        self.gain_mode.setStyleSheet(self.band.styleSheet())
        for name in ("Gain: Handmatig", "Gain: Auto-reductie", "Gain: Volautomatisch"):
            self.gain_mode.addItem(name)
        self.gain_mode.setCurrentIndex(self._gain_mode)
        self.gain_mode.currentIndexChanged.connect(self._set_gain_mode)
        right.addWidget(self.gain_mode)

        row = QHBoxLayout()
        self.btn_mute = QPushButton("🔊 Geluid aan")
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.btn_reset = QPushButton("Reset ruisvloer")
        self.btn_reset.clicked.connect(self.det.reset_noise_floor)
        self.btn_bl = QPushButton("Wis negeerlijst")
        self.btn_bl.clicked.connect(self.det.clear_blacklist)
        for b in (self.btn_mute, self.btn_reset, self.btn_bl):
            b.setStyleSheet(
                f"QPushButton {{ background:{C['panel']}; color:{C['gray1']}; "
                f"border:1px solid {C['sep']}; border-radius:8px; padding:7px; }}"
                f"QPushButton:hover {{ background:{C['panel2']}; }}")
            row.addWidget(b)
        right.addLayout(row)

        self.stat = QLabel("Opstarten…")
        self.stat.setFont(sys_font(9)); self.stat.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stat.setStyleSheet(f"color:{C['gray2']};")
        right.addWidget(self.stat)
        body.addWidget(rw)

        # Bewaarde stand op de hardware/UI toepassen.
        self._update_mute_button()
        self._update_mode_button()
        self._set_gain_mode(self._gain_mode)
        self._on_band(self._init_band_idx)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(250)

    @staticmethod
    def _panel(widget):
        f = QFrame()
        f.setStyleSheet(f"QFrame {{ background:{C['panel']}; border:1px solid {C['sep']}; border-radius:10px; }}")
        lay = QVBoxLayout(f); lay.setContentsMargins(12, 9, 12, 9); lay.addWidget(widget)
        return f

    def _apply_wfall_transform(self):
        freqs = self.det.freqs
        tr = QTransform()
        tr.translate(freqs[0], 0)
        tr.scale((freqs[-1] - freqs[0]) / FFT_SIZE, 1)
        self.img.setTransform(tr)
        self.wfall.setXRange(freqs[0], freqs[-1])
        self.wfall.setYRange(0, WFALL_ROWS)

    # ── callbacks ──
    def _on_detection(self, freq, db, level):
        # Draait in detector-thread; alleen vlaggen, GUI verwerkt in _tick.
        self._pending.append((freq, db, level))

    def _on_gain(self, v):
        self.det.src.gain_db = v
        self.det.src.auto_gain = False
        self.det.src.apply_gain()
        # Handmatige gain bepaalt ook het plafond voor de auto-reductie.
        self.det.agc_max = v

    def _set_gain_mode(self, idx):
        """0 = Handmatig, 1 = Auto-reductie (software), 2 = Volautomatisch (tuner)."""
        self._gain_mode = idx
        g = self.sl_gain.value()
        if idx == 0:        # Handmatig
            self.det.auto_gain_reduction = False
            self.det.src.auto_gain = False
            self.det.src.gain_db = g
            self.det.agc_max = g
            self.sl_gain.setEnabled(True)
        elif idx == 1:      # Auto-reductie: plafond = ingestelde gain
            self.det.auto_gain_reduction = True
            self.det.src.auto_gain = False
            self.det.src.gain_db = g
            self.det.agc_max = g
            self.sl_gain.setEnabled(True)
        else:               # Volautomatisch: tuner regelt zelf
            self.det.auto_gain_reduction = False
            self.det.src.auto_gain = True
            self.sl_gain.setEnabled(False)
        self.det.src.apply_gain()

    def _on_soft(self, v):
        self.det.soft_thr = v
        # Rood mag nooit onder oranje zakken.
        if self.det.hard_thr < v:
            self.det.hard_thr = v
            self.sl_hard.set_value(v)
        self._to_custom()

    def _on_hard(self, v):
        self.det.hard_thr = max(v, self.det.soft_thr)
        self._to_custom()

    # ── Rijmodus (Stad / Snelweg / Custom) ──
    def _cycle_mode(self):
        self._apply_mode((self._mode_idx + 1) % len(RIJMODI))

    def _apply_mode(self, idx):
        self._mode_idx = idx
        m = RIJMODI[idx]
        soft, hard = (self._custom["soft"], self._custom["hard"]) \
            if m["name"] == "Custom" else (m["soft"], m["hard"])
        # set_value blokkeert de signalen → _on_soft/_on_hard vuren niet (geen
        # ongewenste terugschakeling naar Custom); det dus zelf bijwerken.
        self.sl_soft.set_value(soft)
        self.sl_hard.set_value(hard)
        self.det.soft_thr = soft
        self.det.hard_thr = hard
        self._update_mode_button()

    def _update_mode_button(self):
        m = RIJMODI[self._mode_idx]
        col = C[MODE_COLORS[m["name"]]]
        self.btn_mode.setText(f"Rijmodus:  {m['name']}")
        self.btn_mode.setStyleSheet(
            f"QPushButton {{ background:{C['panel']}; color:{col}; "
            f"border:1px solid {col}; border-radius:8px; padding:8px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:{C['panel2']}; }}")

    def _to_custom(self):
        # Handmatig aan een drempel draaien → de modus wordt Custom.
        self._custom = {"soft": self.sl_soft.value(), "hard": self.sl_hard.value()}
        if self._mode_idx != CUSTOM_IDX:
            self._mode_idx = CUSTOM_IDX
            self._update_mode_button()

    def _on_band(self, idx):
        _, center = self._bands[idx]
        self.det.retune(center)
        self._apply_wfall_transform()
        self.spec.setXRange(self.det.freqs[0], self.det.freqs[-1])
        self.curve.setData(self.det.freqs, self.det.power)

    def _toggle_mute(self):
        self.det.muted = not self.det.muted
        self._update_mute_button()

    def _update_mute_button(self):
        if self.det.muted:
            self.btn_mute.setText("🔇 Gedempt")
            self.btn_mute.setStyleSheet(
                f"QPushButton {{ background:{C['panel']}; color:{C['gray2']}; "
                f"border:1px solid {C['gray2']}; border-radius:8px; padding:7px; }}")
        else:
            self.btn_mute.setText("🔊 Geluid aan")
            self.btn_mute.setStyleSheet(
                f"QPushButton {{ background:{C['panel']}; color:{C['gray1']}; "
                f"border:1px solid {C['sep']}; border-radius:8px; padding:7px; }}"
                f"QPushButton:hover {{ background:{C['panel2']}; }}")

    def _tick(self):
        snap = self.det.snapshot()
        self.curve.setData(snap["freqs"], snap["power"])
        self.nf_line.setValue(snap["noise_floor"])
        self.img.setImage(snap["wfall"].T, autoLevels=False)
        self.banner.update_state(snap["alarm_level"], snap["alarm_freq"],
                                 snap["alarm_db"], snap["status"], snap["overload"])
        self.bars.update_data(snap["active"], self.det.soft_thr, self.det.hard_thr)

        # Auto gain-reductie: schuif volgen + oversturing tonen.
        if snap["agc"] and abs(snap["gain"] - self.sl_gain.value()) >= 0.5:
            self.sl_gain.set_value(snap["gain"])
        extra = ""
        if snap["haze_db"] > 0:
            extra = f"   ·   ⚠ OVERSTUUR (vloer +{snap['haze_db']:.0f} dB)"
        elif snap["overload"]:
            extra = "   ·   ⚠ OVERSTUUR"
        elif snap["agc"]:
            extra = f"   ·   gain {snap['gain']:.0f} dB (auto)"
        if snap["blacklist"]:
            extra += f"   ·   {snap['blacklist']} genegeerd"
        self.stat.setText(snap["status"] +
                          f"   ·   ruisvloer {snap['noise_floor']:.0f} dB" + extra)
        # Nieuwe detecties → geschiedenis
        pending, self._pending = self._pending, []
        for freq, db, level in pending:
            self.history.add(freq, db, level)

    # ── instellingen bewaren/laden ──
    def _load_settings(self):
        st = self._settings

        def num(key, default, cast, lo=None, hi=None):
            try:
                v = cast(st.value(key, default))
            except (TypeError, ValueError):
                return default
            if lo is not None and v < lo: return default
            if hi is not None and v > hi: return default
            return v

        return {
            "gain":      num("gain",      self.det.src.gain_db, float, 0,  49),
            "soft_thr":  num("soft_thr",  self.det.soft_thr,    float, 3,  50),
            "hard_thr":  num("hard_thr",  self.det.hard_thr,    float, 8,  70),
            "band_idx":  num("band_idx",  1,                    int,   0,  5),
            "gain_mode": num("gain_mode", 1,                    int,   0,  2),
            "mode_idx":  num("mode_idx",  CUSTOM_IDX,           int,   0,  len(RIJMODI) - 1),
            "custom_soft": num("custom_soft", SOFT_THRESHOLD_DB, float, 3, 50),
            "custom_hard": num("custom_hard", HARD_THRESHOLD_DB, float, 8, 70),
            "muted":     st.value("muted", "false") == "true",
        }

    def _save_settings(self):
        st = self._settings
        st.setValue("gain",      self.sl_gain.value())
        st.setValue("soft_thr",  self.det.soft_thr)
        st.setValue("hard_thr",  self.det.hard_thr)
        st.setValue("band_idx",  self.band.currentIndex())
        st.setValue("gain_mode", self._gain_mode)
        st.setValue("mode_idx",  self._mode_idx)
        st.setValue("custom_soft", self._custom["soft"])
        st.setValue("custom_hard", self._custom["hard"])
        st.setValue("muted",     "true" if self.det.muted else "false")

    def closeEvent(self, event):
        self._save_settings()
        self.timer.stop()
        self.det.stop()
        self.det.src.close()
        event.accept()


# ── Opstarten ───────────────────────────────────────────────────────────────
def main():
    args = parse_args()
    app = QApplication(sys.argv)

    source = RtlTcpSource(args)
    try:
        source.connect()
    except Exception as e:
        QMessageBox.critical(None, "Verbindingsfout",
            f"Kan geen verbinding maken met rtl_tcp op {TCP_HOST}:{args.port}.\n\n"
            f"Controleer of de RTL-SDR Blog V3 is aangesloten en of rtl_tcp "
            f"beschikbaar is (brew install librtlsdr).\n\nFout: {e}")
        sys.exit(1)

    detector = Detector(source)
    detector.start()

    win = MainWindow(detector)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

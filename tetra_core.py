#!/usr/bin/env python3
"""TetraMonitor — detector-kern (geen GUI). Gedeeld door de desktop-app
en de headless webserver. Heeft alleen numpy + stdlib nodig."""

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
SOFT_THRESHOLD_DB = 18.0       # oranje: waarschijnlijk activiteit
HARD_THRESHOLD_DB = 30.0       # rood: duidelijke, sterke activiteit

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
    {"name": "Stad",    "soft": 30.0, "hard": 45.0},
    {"name": "Snelweg", "soft": 25.0, "hard": 35.0},
    {"name": "Custom",  "soft": SOFT_THRESHOLD_DB, "hard": HARD_THRESHOLD_DB},
]
CUSTOM_IDX  = 2
MODE_COLORS = {"Stad": "orange", "Snelweg": "green", "Custom": "blue"}

# Bandkeuzes (label, centerfrequentie in MHz) — gedeeld door beide vensters.
BANDS = [("Uplink 379.9–383.1 (laag)", 381.5),
         ("Uplink 380.9–384.1 (midden)", 382.5),
         ("Uplink 381.9–385.1 (hoog)", 383.5),
         ("Downlink 389.9–393.1 (laag)", 391.5),
         ("Downlink 390.9–394.1 (midden)", 392.5),
         ("Downlink 391.9–395.1 (hoog)", 393.5)]
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
    "/opt/homebrew/bin/rtl_tcp",   # macOS Apple Silicon
    "/usr/local/bin/rtl_tcp",      # macOS Intel
    "/usr/bin/rtl_tcp",            # Linux / Raspberry Pi (apt)
    "rtl_tcp",                     # val terug op PATH
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

    def snapshot_lite(self):
        """Lichte snapshot zonder de zware spectrum-/waterfall-arrays — voor de
        headless webserver (telefoon), zodat elke poll goedkoop blijft."""
        with self.lock:
            active = sorted(
                ((ch.freq, ch.level, self._trend(ch))
                 for ch in self.channels.values() if ch.level > 0),
                key=lambda x: -x[1])
            return {
                "status": self.status,
                "alarm_level": self.alarm_level,
                "alarm_freq": self.alarm_freq,
                "alarm_db": self.alarm_db,
                "active": active,
                "gain": self.src.gain_db,
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



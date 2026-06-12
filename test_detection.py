#!/usr/bin/env python3
"""
Offline zelftest voor de detectiekern van TetraMonitor.

Voert synthetische IQ-frames door exact dezelfde verwerking als de live-detector
en controleert:
  1. pure ruis blijft onder de drempel (geen vals alarm);
  2. een sterke draaggolf wordt op het juiste 25 kHz-kanaal gedetecteerd (rood);
  3. een kanaal dat te lang continu actief is, komt op de negeerlijst (blacklist)
     en stopt met alarmeren — en "Wis negeerlijst" maakt hem weer leeg;
  4. oversturing (clipping, zender zeer dichtbij) forceert rood alarm i.p.v.
     stil te vallen.

Draait zonder dongle:  python3 test_detection.py
"""
import numpy as np

import tetra_monitor as tm
from tetra_monitor import Detector, FFT_SIZE, SAMPLE_RATE


class FakeClock:
    """Bestuurbare klok zodat de 20s-blacklist niet echt 20 s hoeft te duren."""
    def __init__(self): self.t = 10_000.0
    def time(self): return self.t
    def sleep(self, _): pass


clock = FakeClock()
tm.time = clock   # tetra_monitor gebruikt time.time()/time.sleep()


class StubSource:
    def __init__(self, center_mhz=382.5, gain_db=40.0):
        self.center_hz = int(center_mhz * 1e6)
        self.gain_db = gain_db
    def apply_gain(self): pass
    def set_center(self, hz): self.center_hz = hz


def noise_frame(std=6.0):
    raw = np.random.normal(127.5, std, 2 * FFT_SIZE)
    return raw.clip(0, 255).astype(np.uint8).tobytes()


def signal_frame(offset_hz, amp=35.0, std=6.0):
    n = FFT_SIZE; t = np.arange(n)
    c = np.exp(2j * np.pi * offset_hz * t / SAMPLE_RATE)
    I = 127.5 + amp * c.real + np.random.normal(0, std, n)
    Q = 127.5 + amp * c.imag + np.random.normal(0, std, n)
    raw = np.empty(2 * n); raw[0::2] = I; raw[1::2] = Q
    return raw.clip(0, 255).astype(np.uint8).tobytes()


def clip_frame(offset_hz=300_000, amp=220.0):
    """Veel te sterk → samples klippen op 0/255 (front-end overstuur)."""
    n = FFT_SIZE; t = np.arange(n)
    c = np.exp(2j * np.pi * offset_hz * t / SAMPLE_RATE)
    I = 127.5 + amp * c.real; Q = 127.5 + amp * c.imag
    raw = np.empty(2 * n); raw[0::2] = I; raw[1::2] = Q
    return raw.clip(0, 255).astype(np.uint8).tobytes()


def feed(det, frame_fn, n, dt=0.05, **kw):
    for _ in range(n):
        clock.t += dt
        det._process(frame_fn(**kw))


def run():
    np.random.seed(0)
    det = Detector(StubSource(center_mhz=382.5))
    det.soft_thr = tm.SOFT_THRESHOLD_DB
    det.hard_thr = tm.HARD_THRESHOLD_DB
    det._log = lambda *a, **k: None   # zelftest niet naar de echte CSV schrijven

    # 1) Warmup + ruis
    feed(det, noise_frame, tm.WARMUP_FRAMES + 10, dt=0.02)
    snap = det.snapshot()
    noise_max = max((lvl for _, lvl, _ in snap["active"]), default=0.0)
    print(f"[ruis]     actief: {len(snap['active'])}, hoogste: {noise_max:.1f} dB, "
          f"alarm: {snap['alarm_level']}")
    assert snap["alarm_level"] == 0 and noise_max < det.soft_thr, "Vals alarm op ruis!"

    # 2) Draaggolf op center + 0.5 MHz → kanaal ~383.000 MHz
    feed(det, signal_frame, 25, dt=0.05, offset_hz=500_000)
    snap = det.snapshot()
    top_freq, top_lvl, _ = snap["active"][0]
    print(f"[signaal]  sterkste: {top_freq:.4f} MHz @ {top_lvl:.1f} dB, "
          f"alarm: {snap['alarm_level']}")
    assert abs(top_freq - 383.0) <= 0.0125, f"Verkeerd kanaal: {top_freq:.4f}"
    assert top_lvl >= det.hard_thr and snap["alarm_level"] == 2, "Geen rood alarm!"

    # 3) Zelfde draaggolf blijft >20 s continu aan → moet op de negeerlijst
    feed(det, signal_frame, 60, dt=0.5, offset_hz=500_000)   # 30 s
    snap = det.snapshot()
    in_active = any(abs(f - 383.0) <= 0.0125 for f, _, _ in snap["active"])
    print(f"[blacklist] genegeerd: {snap['blacklist']}, 383 nog actief: {in_active}, "
          f"alarm: {snap['alarm_level']}")
    assert snap["blacklist"] >= 1, "Constante storing niet geblacklist!"
    assert not in_active, "Geblacklist kanaal staat nog in de actieve lijst!"
    assert snap["alarm_level"] == 0, "Alarm blijft hangen na blacklist (latch-bug)!"
    det.clear_blacklist()
    assert det.blacklist_count() == 0, "Wis negeerlijst werkt niet!"

    # 4) Oversturing (clipping) → moet rood alarm forceren
    feed(det, clip_frame, 30, dt=0.02)
    snap = det.snapshot()
    print(f"[overstuur] overload: {snap['overload']}, clip_peak: {snap['clip_peak']:.2f}, "
          f"alarm: {snap['alarm_level']}")
    assert snap["overload"] and snap["alarm_level"] == 2, "Oversturing gaf geen alarm!"

    print("\n✅ Alle checks geslaagd — detectie, blacklist en oversturing werken.")


if __name__ == "__main__":
    run()

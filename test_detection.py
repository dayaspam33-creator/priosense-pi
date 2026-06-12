#!/usr/bin/env python3
"""
Offline zelftest voor de detectiekern van TetraMonitor.

Voert synthetische IQ-frames (ruis + een bekende draaggolf) door exact dezelfde
verwerking als de live-detector en controleert:
  1. pure ruis blijft onder de drempel (geen vals alarm);
  2. een sterke draaggolf wordt gedetecteerd op het juiste 25 kHz-kanaal met
     een hoog niveau (rood alarm).

Draait zonder dongle:  python3 test_detection.py
"""
import numpy as np

import tetra_monitor as tm
from tetra_monitor import Detector, FFT_SIZE, SAMPLE_RATE


class StubSource:
    """Minimale nep-bron: de detectiekern gebruikt alleen center_hz/gain_db."""
    def __init__(self, center_mhz=382.5, gain_db=40.0):
        self.center_hz = int(center_mhz * 1e6)
        self.gain_db = gain_db
    def apply_gain(self): pass
    def set_center(self, hz): self.center_hz = hz


def noise_frame(std=6.0):
    raw = np.random.normal(127.5, std, 2 * FFT_SIZE)
    return raw.clip(0, 255).astype(np.uint8).tobytes()


def signal_frame(offset_hz, amp=35.0, std=6.0):
    """Ruis + complexe draaggolf op center+offset_hz."""
    n = FFT_SIZE
    t = np.arange(n)
    c = np.exp(2j * np.pi * offset_hz * t / SAMPLE_RATE)
    I = 127.5 + amp * c.real + np.random.normal(0, std, n)
    Q = 127.5 + amp * c.imag + np.random.normal(0, std, n)
    raw = np.empty(2 * n)
    raw[0::2] = I
    raw[1::2] = Q
    return raw.clip(0, 255).astype(np.uint8).tobytes()


def run():
    np.random.seed(0)
    det = Detector(StubSource(center_mhz=382.5))
    det.soft_thr = tm.SOFT_THRESHOLD_DB
    det.hard_thr = tm.HARD_THRESHOLD_DB
    det._log = lambda *a, **k: None   # zelftest niet naar de echte CSV schrijven

    # 1) Warmup + ruis: ruisvloer opbouwen, daarna een paar ruisframes.
    for _ in range(tm.WARMUP_FRAMES + 10):
        det._process(noise_frame())

    snap = det.snapshot()
    noise_max = max((lvl for _, lvl, _ in snap["active"]), default=0.0)
    print(f"[ruis]   actieve kanalen: {len(snap['active'])}, "
          f"hoogste niveau: {noise_max:.1f} dB, alarm: {snap['alarm_level']}")
    assert snap["alarm_level"] == 0, "Vals alarm op pure ruis!"
    assert noise_max < det.soft_thr, f"Ruis ({noise_max:.1f} dB) boven drempel!"

    # 2) Draaggolf op center + 0.500 MHz  → verwacht kanaal ~383.000 MHz.
    target_mhz = 382.5 + 0.5
    for _ in range(20):
        det._process(signal_frame(offset_hz=500_000))

    snap = det.snapshot()
    assert snap["active"], "Geen enkel kanaal actief na sterke draaggolf!"
    top_freq, top_lvl, _ = snap["active"][0]
    print(f"[signaal] sterkste kanaal: {top_freq:.4f} MHz @ {top_lvl:.1f} dB, "
          f"alarm: {snap['alarm_level']}")
    assert abs(top_freq - target_mhz) <= 0.0125, \
        f"Draaggolf op verkeerd kanaal: {top_freq:.4f} i.p.v. ~{target_mhz:.3f}"
    assert top_lvl >= det.hard_thr, \
        f"Draaggolf te zwak gemeten: {top_lvl:.1f} < {det.hard_thr} dB"
    assert snap["alarm_level"] == 2, "Sterke draaggolf gaf geen rood alarm!"

    print("\n✅ Alle checks geslaagd — detectiekern werkt correct.")


if __name__ == "__main__":
    run()

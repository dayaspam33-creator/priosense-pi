#!/usr/bin/env python3
"""
Offline zelftest voor de detectiekern van TetraMonitor.

Voert synthetische IQ-frames door exact dezelfde verwerking als de live-detector
en controleert de hele keten:
  1. pure ruis blijft onder de drempel (geen vals alarm);
  2. een breed TETRA-achtig signaal wordt op het juiste 25 kHz-kanaal gedetecteerd;
  3. een kanaal dat te lang continu actief is komt op de negeerlijst (blacklist);
  4. oversturing (clipping) forceert rood alarm i.p.v. stil te vallen;
  5. de DC-spike op de centerfrequentie geeft GEEN vals alarm;
  6. een smalle toon (birdie/CW) wordt door de bezettingscheck genegeerd;
  7. CFAR maakt een scheve ruisvloer vlak (lokale i.p.v. globale ruis).

Draait zonder dongle:  python3 test_detection.py
"""
import numpy as np

import tetra_monitor as tm
from tetra_monitor import Detector, FFT_SIZE, SAMPLE_RATE


class FakeClock:
    def __init__(self): self.t = 10_000.0
    def time(self): return self.t
    def sleep(self, _): pass


clock = FakeClock()
tm.time = clock


class StubSource:
    def __init__(self, center_mhz=382.5, gain_db=40.0):
        self.center_hz = int(center_mhz * 1e6)
        self.gain_db = gain_db
    def apply_gain(self): pass
    def set_center(self, hz): self.center_hz = hz


def noise_frame(std=6.0):
    raw = np.random.normal(127.5, std, 2 * FFT_SIZE)
    return raw.clip(0, 255).astype(np.uint8).tobytes()


def _to_raw(I, Q):
    raw = np.empty(2 * FFT_SIZE); raw[0::2] = I; raw[1::2] = Q
    return raw.clip(0, 255).astype(np.uint8).tobytes()


def tetra_frame(offset_hz, amp=32.0, width_hz=18000.0, std=6.0):
    """Breed (~18 kHz) TETRA-achtig signaal: bandbegrensde ruis op center+offset."""
    n = FFT_SIZE
    w = np.fft.fftfreq(n, d=1 / SAMPLE_RATE)
    sp = np.random.randn(n) + 1j * np.random.randn(n)
    sp[np.abs(w) > width_hz / 2] = 0
    base = np.fft.ifft(sp); base /= np.std(base.real)
    t = np.arange(n)
    sig = base * np.exp(2j * np.pi * offset_hz * t / SAMPLE_RATE)
    return _to_raw(127.5 + amp * sig.real + np.random.normal(0, std, n),
                   127.5 + amp * sig.imag + np.random.normal(0, std, n))


def tone_frame(offset_hz, amp=35.0, std=6.0):
    """Smalle pure toon (birdie/CW) — moet door de bezettingscheck genegeerd worden."""
    n = FFT_SIZE; t = np.arange(n)
    c = np.exp(2j * np.pi * offset_hz * t / SAMPLE_RATE)
    return _to_raw(127.5 + amp * c.real + np.random.normal(0, std, n),
                   127.5 + amp * c.imag + np.random.normal(0, std, n))


def multi_tetra_frame(offsets, amp=30.0, width_hz=18000.0, std=6.0):
    """Meerdere gelijktijdige TETRA-achtige signalen (verschillende eenheden)."""
    n = FFT_SIZE; t = np.arange(n)
    I = np.full(n, 127.5); Q = np.full(n, 127.5)
    w = np.fft.fftfreq(n, d=1 / SAMPLE_RATE)
    for off in offsets:
        sp = np.random.randn(n) + 1j * np.random.randn(n)
        sp[np.abs(w) > width_hz / 2] = 0
        base = np.fft.ifft(sp); base /= np.std(base.real)
        sig = base * np.exp(2j * np.pi * off * t / SAMPLE_RATE)
        I += amp * sig.real; Q += amp * sig.imag
    I += np.random.normal(0, std, n); Q += np.random.normal(0, std, n)
    return _to_raw(I, Q)


def clip_frame(offset_hz=300_000, amp=220.0):
    n = FFT_SIZE; t = np.arange(n)
    c = np.exp(2j * np.pi * offset_hz * t / SAMPLE_RATE)
    return _to_raw(127.5 + amp * c.real, 127.5 + amp * c.imag)


def feed(det, frame_fn, n, dt=0.05, **kw):
    for _ in range(n):
        clock.t += dt
        det._process(frame_fn(**kw))


def fresh(center_mhz=382.5):
    det = Detector(StubSource(center_mhz=center_mhz))
    det.soft_thr = tm.SOFT_THRESHOLD_DB
    det.hard_thr = tm.HARD_THRESHOLD_DB
    det._log = lambda *a, **k: None
    feed(det, noise_frame, tm.WARMUP_FRAMES + 5, dt=0.02)
    return det


def run():
    np.random.seed(0)

    # 1) Ruis → geen vals alarm
    det = fresh()
    feed(det, noise_frame, 10, dt=0.02)
    snap = det.snapshot()
    nmax = max((l for _, l, _ in snap["active"]), default=0.0)
    print(f"[ruis]      actief: {len(snap['active'])}, hoogste: {nmax:.1f} dB")
    assert snap["alarm_level"] == 0 and nmax < det.soft_thr, "Vals alarm op ruis!"

    # 2) Breed TETRA-achtig signaal op +0.5 MHz → kanaal ~383.000
    feed(det, tetra_frame, 25, dt=0.05, offset_hz=500_000)
    snap = det.snapshot()
    top_freq, top_lvl, _ = snap["active"][0]
    print(f"[signaal]   sterkste: {top_freq:.4f} MHz @ {top_lvl:.1f} dB, alarm: {snap['alarm_level']}")
    assert abs(top_freq - 383.0) <= 0.0125, f"Verkeerd kanaal: {top_freq:.4f}"
    assert snap["alarm_level"] == 2, "Breed signaal gaf geen rood alarm!"

    # 3) Blijft >20 s continu aan → negeerlijst, alarm zakt naar 0
    feed(det, tetra_frame, 60, dt=0.5, offset_hz=500_000)
    snap = det.snapshot()
    in_active = any(abs(f - 383.0) <= 0.0125 for f, _, _ in snap["active"])
    print(f"[blacklist] genegeerd: {snap['blacklist']}, alarm: {snap['alarm_level']}")
    assert snap["blacklist"] >= 1 and not in_active, "Storing niet geblacklist!"
    assert snap["alarm_level"] == 0, "Alarm blijft hangen na blacklist!"
    det.clear_blacklist(); assert det.blacklist_count() == 0

    # 4) Oversturing → rood alarm
    feed(det, clip_frame, 30, dt=0.02)
    snap = det.snapshot()
    print(f"[overstuur] overload: {snap['overload']}, alarm: {snap['alarm_level']}")
    assert snap["overload"] and snap["alarm_level"] == 2, "Oversturing gaf geen alarm!"

    # 5) DC-spike op center (offset 0) → geen vals alarm op de centerfrequentie
    det = fresh()
    feed(det, tone_frame, 25, dt=0.05, offset_hz=0)
    snap = det.snapshot()
    on_center = any(abs(f - 382.5) <= 0.0125 for f, _, _ in snap["active"])
    print(f"[dc-spike]  center actief: {on_center}, alarm: {snap['alarm_level']}")
    assert not on_center, "DC-spike geeft vals signaal op center!"

    # 6) Smalle toon (birdie) op +0.7 MHz → bezettingscheck negeert hem
    det = fresh()
    feed(det, tone_frame, 25, dt=0.05, offset_hz=700_000)
    snap = det.snapshot()
    tone_active = any(abs(f - 383.2) <= 0.02 for f, _, _ in snap["active"])
    print(f"[birdie]    toon actief: {tone_active}, alarm: {snap['alarm_level']}")
    assert not tone_active, "Smalle toon niet door bezettingscheck genegeerd!"

    # 7) CFAR vlakt een scheve ruisvloer (unit-check op _cfar)
    tilt = np.linspace(1.0, 50.0, 120)          # sterk oplopende ruis
    tilt[60] = 3000.0                           # één echt signaal-kanaal
    local = Detector._cfar(tilt)
    snr = 10 * np.log10(tilt / local)
    flat = np.concatenate([snr[5:55], snr[65:115]])   # alles behalve de piek
    print(f"[cfar]      vlakke kanalen max {flat.max():.1f} dB, piek {snr[60]:.1f} dB")
    assert flat.max() < 6.0, "CFAR maakt de scheve ruisvloer niet vlak!"
    assert snr[60] > 15.0, "CFAR onderdrukt het echte signaal!"

    # 8) "Waas": hele ruisvloer tilt op (zender vlakbij, geen harde clipping) →
    #    moet rood alarm geven, ook al steekt geen enkel kanaal er relatief uit.
    det = fresh()                                  # baseline op stille ruis
    feed(det, noise_frame, 600, dt=0.01, std=24.0) # ~+12 dB vloer, geen clip
    snap = det.snapshot()
    print(f"[waas]      vloer +{snap['haze_db']:.0f} dB, clip {snap['clip_peak']:.2f}, "
          f"alarm: {snap['alarm_level']}")
    assert snap["clip_peak"] < 0.90, "Test klipt — waas niet geïsoleerd!"
    assert snap["haze_db"] > 0 and snap["alarm_level"] == 2, "Waas gaf geen alarm!"

    # 9) Meerdere eenheden tegelijk → meerdere aparte actieve kanalen (balken)
    det = fresh()
    offs = [-700_000, -200_000, 400_000, 900_000]   # 4 eenheden op eigen kanalen
    feed(det, multi_tetra_frame, 25, dt=0.05, offsets=offs)
    snap = det.snapshot()
    freqs = sorted(f for f, _, _ in snap["active"])
    print(f"[multi]     {len(snap['active'])} eenheden actief: "
          f"{', '.join(f'{f:.3f}' for f in freqs)}")
    assert len(snap["active"]) >= 4, \
        f"Maar {len(snap['active'])} eenheden zichtbaar i.p.v. 4!"

    # 10) Korte registratiepuls (burst) → piek-hold houdt 'm vast, ook nadat de
    #     burst allang voorbij is (passerend voertuig dat niet praat).
    det = fresh()
    feed(det, noise_frame, 5, dt=0.02)
    feed(det, tetra_frame, 3, dt=0.02, offset_hz=-400_000)   # korte burst op 382.1
    feed(det, noise_frame, 40, dt=0.02)                      # burst voorbij
    snap = det.snapshot()
    held = any(abs(f - 382.1) <= 0.0125 for f, _, _ in snap["active"])
    print(f"[burst]     puls op 382.1 nog vastgehouden: {held}, alarm: {snap['alarm_level']}")
    assert held, "Korte burst niet vastgehouden door piek-hold!"

    print("\n✅ Alle 10 checks geslaagd — detectie, CFAR, DC-spike, bezetting,"
          " blacklist, oversturing, waas, meerdere eenheden en burst-puls werken.")


if __name__ == "__main__":
    run()

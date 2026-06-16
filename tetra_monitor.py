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

import platform
import sys
import numpy as np

from tetra_core import *

from PyQt6.QtCore import Qt, QTimer, QRectF, QSettings, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QTransform
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)
import pyqtgraph as pg

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

        # Bewaarde stand op de hardware/UI toepassen. _apply_mode zet meteen de
        # juiste (actuele) drempels voor de bewaarde modus, zodat verhoogde
        # presets ook gelden na een update i.p.v. oude waarden uit QSettings.
        self._update_mute_button()
        self._apply_mode(self._mode_idx)
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

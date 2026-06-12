#!/usr/bin/env python3
"""Tekent een eenvoudig TetraMonitor-icoon (donker met signaalbogen) naar PNG.

Gebruik:  QT_QPA_PLATFORM=offscreen python3 make_icon.py uit.png [grootte]
Gebruikt PyQt6 (al geïnstalleerd). Faalt stil als er geen render mogelijk is.
"""
import sys

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPainter, QColor, QPixmap, QPen, QPainterPath
from PyQt6.QtCore import Qt, QRectF


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "icon.png"
    sz = int(sys.argv[2]) if len(sys.argv) > 2 else 1024
    app = QApplication(sys.argv)            # noqa: F841 (nodig voor QPixmap)

    px = QPixmap(sz, sz)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Afgeronde donkere achtergrond (zoals de app-UI)
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(0, 0, sz, sz), sz * 0.22, sz * 0.22)
    p.fillPath(bg, QColor("#101216"))

    # Drie signaalbogen + middelpunt
    cx, cy = sz * 0.5, sz * 0.60
    for i, r in enumerate([0.15, 0.27, 0.39]):
        col = QColor("#3aa0ff")
        col.setAlpha(255 - i * 60)
        pen = QPen(col, sz * (0.055 - i * 0.012))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        rr = r * sz
        p.drawArc(QRectF(cx - rr, cy - rr, rr * 2, rr * 2), 35 * 16, 110 * 16)

    p.setBrush(QColor("#3aa0ff"))
    p.setPen(Qt.PenStyle.NoPen)
    d = sz * 0.05
    p.drawEllipse(QRectF(cx - d, cy - d, d * 2, d * 2))
    p.end()

    if not px.save(out, "PNG"):
        sys.exit(1)


if __name__ == "__main__":
    main()

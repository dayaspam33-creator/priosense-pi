# TetraMonitor

Een TETRA/C2000 **activiteitsmonitor** voor de RTL-SDR Blog V3. Hij meet of er
zenders actief zijn in de TETRA-band en zet dat om in beeld: een groot
activiteitsbanner, live spectrum, waterfall, activiteitsbalken per kanaal met
richting (nadert/gaat weg), een geluidsalarm en een CSV-log.

Standaard kijkt hij naar de **uplink** (390–395 MHz) — de portofoons en
voertuigen die zelf zenden, wat je met een magneetantenne dichtbij het beste
oppikt. Via de banddropdown schakel je naar de **downlink** (380–385 MHz, de
basisstations).

> **Let op:** dit programma **decodeert niets**. Het meet alleen signaalsterkte
> (energie boven de ruisvloer) om te laten zien *dát* er activiteit is. Het
> luistert geen gesprekken af en leest geen data — dat is in Nederland niet
> toegestaan en is hier ook niet nodig.

## Hoe het werkt

- De band wordt opgedeeld in kanalen van 25 kHz (het TETRA-raster).
- Het programma schat continu de **ruisvloer** en meet per kanaal hoeveel dB een
  signaal daarbovenuit komt. Zo zie je echte activiteit in plaats van ruis.
- **Oranje** = mogelijke activiteit, **rood** = sterke, duidelijke activiteit.
- Pijlen tonen of een signaal **sterker wordt** (▲ nadert) of **zwakker** (▼ gaat
  weg) — handig met een magneetantenne onderweg.

## Vereisten

- Python 3.10+
- RTL-SDR Blog V3 + TETRA-antenne (bijv. de Motorola magneetantenne)
- `rtl_tcp` uit librtlsdr:
  - macOS: `brew install librtlsdr`
  - Linux: `sudo apt install rtl-sdr`

```bash
pip3 install -r requirements.txt
```

## Gebruik

```bash
python3 tetra_monitor.py
```

Het programma start zelf `rtl_tcp`, bouwt ~15 seconden de ruisvloer op en gaat
daarna scannen. Sluit het venster om te stoppen.

### Opties

| Optie | Betekenis |
|---|---|
| `--center` | centerfrequentie in MHz (default 392.5 = uplink midden) |
| `--gain` | tuner gain in dB (default 40) |
| `--ppm` | frequentiecorrectie in ppm (bij de V3 vaak 0–1) |
| `--port` | rtl_tcp poort (default 1234) |
| `--device` | dongle index (default 0) |
| `--extern` | rtl_tcp draait al; niet zelf starten/stoppen |

De band is breder dan wat de dongle in één keer ziet (~3.2 MHz). Met de
**banddropdown** rechtsonder schuif je tussen het lage, midden- en hoge deel van
380–385 MHz.

## Afstellen

- **Gain** te hoog → veel ruis en valse activiteit; te laag → je mist zwakke
  signalen. Begin bij 40 dB en stel bij.
- **Drempels** bepalen wanneer iets oranje/rood wordt. Op een rustige plek kun je
  ze verlagen, in een drukke RF-omgeving verhogen.
- Verandert de omgeving sterk? Klik **Reset ruisvloer**.

Activiteit wordt gelogd naar `tetra_activiteit.csv` naast het script.

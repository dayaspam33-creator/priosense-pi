# TetraMonitor

Een TETRA/C2000 **activiteitsmonitor** voor de RTL-SDR Blog V3. Hij meet of er
zenders actief zijn in de TETRA-band en zet dat om in beeld: een groot
activiteitsbanner, live spectrum, waterfall, activiteitsbalken per kanaal met
richting (nadert/gaat weg), een geluidsalarm en een CSV-log.

Standaard kijkt hij naar de **uplink** (380–385 MHz) — de portofoons en
voertuigen die zelf zenden. Een eenheid die vlakbij zendt geeft daar een sterk,
kortdurend signaal, dus dat is wat je met een magneetantenne in/bij de auto het
beste oppikt. Via de banddropdown schakel je naar de **downlink** (390–395 MHz):
de basisstations, die continu zenden (controlekanaal) en dus wijzen op C2000-
infrastructuur in de buurt.

> **Bandindeling (NL C2000, vaste ETSI/CEPT-indeling, 10 MHz duplex):**
> portofoons/voertuigen zenden op **380–385 MHz** (uplink), basisstations op
> **390–395 MHz** (downlink).

> **Let op:** dit programma **decodeert niets**. Het meet alleen signaalsterkte
> (energie boven de ruisvloer) om te laten zien *dát* er activiteit is. Het
> luistert geen gesprekken af en leest geen data — dat is in Nederland niet
> toegestaan en is hier ook niet nodig.

## Hoe het werkt

- De band wordt opgedeeld in kanalen van 25 kHz (het TETRA-raster). Een 4096-punts
  FFT met Blackman-venster geeft fijne resolutie (~0,8 kHz/bin) en houdt naburige
  kanalen netjes uit elkaar.
- Per kanaal wordt de **energie over de volle 25 kHz geïntegreerd** en uitgedrukt
  als dB boven de ruisvloer — dezelfde aanpak als professionele TETRA-sensoren,
  robuuster dan losse pieken meten.
- Het programma schat continu de **ruisvloer**, zodat je echte activiteit ziet in
  plaats van ruis.
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
| `--center` | centerfrequentie in MHz (default 382.5 = downlink midden) |
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
- **Gain-modus** (dropdown):
  - *Handmatig* — je stelt de gain zelf in met de schuif.
  - *Auto-reductie* — bij oversturing (clipping) draait hij de gain automatisch
    omlaag en, zodra er weer ruimte is, terug omhoog tot de waarde die je zelf
    had ingesteld. De schuif volgt mee; de statusregel toont "⚠ OVERSTUUR".
  - *Volautomatisch* — de tuner regelt de gain zelf (hardware-AGC).

Instellingen (gain, drempels, band, gain-modus, mute) worden automatisch
bewaard en bij de volgende start weer geladen.
- **Drempels** bepalen wanneer iets oranje/rood wordt. Op een rustige plek kun je
  ze verlagen, in een drukke RF-omgeving verhogen.
- Verandert de omgeving sterk? Klik **Reset ruisvloer**.

Activiteit wordt gelogd naar `tetra_activiteit.csv` naast het script.

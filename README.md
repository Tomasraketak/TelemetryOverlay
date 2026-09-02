# Telemetry Overlay

Generátor **animovaných telemetrických overlayů s průhledným pozadím**.
Z CSV nebo XLSX logu vyrobí video (ProRes 4444 / QuickTime RLE / WebM s alfa
kanálem) nebo PNG sekvenci, které stačí položit v DaVinci Resolve na stopu nad
záběr. Zdarma, offline, bez nahrávání dat kamkoliv.

Dělá to samé co GoPro Telemetry Extractor, ale pro **libovolná data z vlastního
loggeru** – tah motoru, tlak, teplotu, rychlost, výšku, napětí, cokoliv co máte
ve sloupci.

> English summary: renders animated, alpha-transparent telemetry overlays from
> CSV/XLSX logs straight to video (ProRes 4444, QuickTime RLE, VP9/WebM) or a
> PNG sequence, for compositing in DaVinci Resolve. All documentation below is
> in Czech; the CLI and config keys are in English.

---

## Co to umí

- **Video s alfa kanálem** – ProRes 4444, QuickTime Animation (RLE) nebo
  VP9/WebM. Snímky jdou rovnou do ffmpegu, na disk se nic mezitím neukládá.
- **Nebo PNG sekvence** – když editor chce jednotlivé snímky, nebo ffmpeg není.
- **Zpomalování času** – globální rychlost, *rampy* (zpomalit jen zážeh) a
  *zámrazy* na konkrétním okamžiku, včetně plynulého náběhu.
- **Widgety** – velké číslo, tabulka hodnot, živý graf s přejezdem, ručičkový
  budík, sloupcový ukazatel, stavová kontrolka, misijní hodiny, logo.
- **Rozlišení až 8K** – layout se zadává v procentech, takže stejná konfigurace
  funguje pro 1080p náhled i 4K master.
- **Automatická detekce** – oddělovač, desetinná čárka, kódování, sloupec času
  (včetně milisekund), booleovské sloupce.
- **Detekce událostí** – zážeh, vrchol, dohoření a hrany pyro kanálu se najdou
  samy a vyznačí se v grafu.
- **Dopočítané veličiny** – např. celkový impuls jako integrál tahu, přímo
  v konfiguraci.

## Instalace

```bash
git clone <adresa-repozitáře>
cd TelemetryOverlay
pip install -e .
```

Tím se nainstaluje i `imageio-ffmpeg`, které s sebou nese vlastní ffmpeg –
**export videa funguje hned, nic dalšího instalovat nemusíte.**

Python 3.10 nebo novější.

## Rychlý start

```bash
# 1) Co v tom souboru vlastně je?
telemetry-overlay inspect examples/static_fire_thrust.csv

# 2) Vyrob konfiguraci na míru těmto datům
telemetry-overlay init examples/static_fire_thrust.csv -o overlay.yaml

# 3) Zkontroluj vzhled na jednom snímku (rychlé)
telemetry-overlay preview examples/static_fire_thrust.csv \
    -c overlay.yaml --at peak -o preview.png

# 4) Vyrenderuj video
telemetry-overlay render examples/static_fire_thrust.csv \
    -c overlay.yaml -o overlay.mov
```

Nebo rovnou s připravenou šablonou, bez psaní konfigurace:

```bash
telemetry-overlay presets                     # seznam šablon
telemetry-overlay render data.csv -c rocket_thrust -o overlay.mov
```

## Formát výstupu

Formát se pozná podle přípony souboru:

| `-o …`         | Kodek        | Alfa | Poznámka                                       |
|----------------|--------------|------|------------------------------------------------|
| `overlay.mov`  | ProRes 4444  | ano  | Výchozí. Standardní vstup do Resolve.          |
| `overlay.mov --codec qtrle` | QuickTime RLE | ano | Bezztrátové, **výrazně menší a rychlejší**. |
| `overlay.webm` | VP9          | ano  | Pro web, ne pro Resolve.                       |
| `overlay.mp4`  | H.264        | **ne** | Jen na rychlé kouknutí – průhlednost zčerná. |
| `frames/`      | PNG sekvence | ano  | Jednotlivé snímky, ffmpeg není potřeba.        |

Na 13sekundovém klipu v 1080p60 z přiloženého vzorku:
ProRes 4444 = 372 MB / 56 s, QuickTime RLE = 66 MB / 12 s. Overlay je z větší
části průhledný, a právě to RLE komprimuje mimořádně dobře – **`--codec qtrle`
je pro tenhle účel většinou lepší volba.**

## Zpomalení času

Nejjednodušší je globální rychlost. `--speed 0.25` znamená čtyřikrát pomaleji:

```bash
telemetry-overlay render data.csv -c rocket_thrust -o overlay.mov --speed 0.25
```

Většinou ale chcete zpomalit **jen zajímavý úsek**. K tomu slouží rampy –
`--ramp OD:DO:RYCHLOST[:NÁBĚH]`, časy jsou v sekundách dat:

```bash
# zbytek v reálném čase, zážeh 1,6–2,6 s pětkrát pomaleji,
# s 0,15 s plynulým náběhem na obou koncích
telemetry-overlay render data.csv -c rocket_thrust -o overlay.mov \
    --ramp 1.6:2.6:0.2:0.15
```

A `--hold KDY:SEKUND` zamrzne na daném okamžiku (video běží, data stojí):

```bash
telemetry-overlay render data.csv -c rocket_thrust -o overlay.mov \
    --ramp 1.6:2.6:0.2:0.15 --hold 2.30:1.5
```

Totéž jde zapsat i do konfigurace, viz [docs/CONFIG.md](docs/CONFIG.md).

> **Důležité:** tohle mění jen tempo *overlaye*. Aby seděl na obraz, musíte
> stejné zpomalení udělat i se záběrem – v Resolve přes Retime Controls.
> Postup je popsaný v [docs/DAVINCI_RESOLVE.md](docs/DAVINCI_RESOLVE.md).

## Import do DaVinci Resolve

Celý postup i s řešením průhlednosti je v
**[docs/DAVINCI_RESOLVE.md](docs/DAVINCI_RESOLVE.md)**. Zkráceně: overlay
patří na stopu **nad** záběr, alfa kanál se použije sám. Pokud je pozadí černé,
přepněte u klipu *Clip Attributes → Alpha Mode* na **Premultiplied**.

## Vlastní vzhled

Konfigurace je YAML (nebo JSON). Kompletní popis všech widgetů a jejich
parametrů je v **[docs/CONFIG.md](docs/CONFIG.md)**. Ukázka:

```yaml
output:
  resolution: 4k          # nebo 1080p, 2560x1440, …
  fps: 60

time:
  speed: 1.0
  ramps:
    - { from: 1.4, to: 3.6, speed: 0.25, ease: 0.25 }

theme:
  base: night             # night | day | hud | flame | clean
  palette: rocket

derived:                  # veličiny, které logger nezaznamenal
  - key: impulse
    label: Celkový impuls
    unit: "N·s"
    expression: integral(clip(thrust, 0, 1e9))

widgets:
  - type: readout
    channel: thrust
    label: Tah
    x: 4%
    y: 22%
    value_size: 92

  - type: graph
    channel: thrust
    x: 4%
    y: 60%
    width: 56%
    height: 33%
    show_value: true
```

## Vlastní data

Nic nastavovat nemusíte – oddělovač, desetinná čárka i sloupec času se poznají
samy. `inspect` ukáže, jak byl soubor pochopen:

```bash
telemetry-overlay inspect muj_log.csv
```

Když se sloupec času netrefí, přebijte to ručně:

```bash
telemetry-overlay inspect muj_log.csv --time-column "Cas" --time-unit ms
telemetry-overlay inspect data.xlsx --sheet "Data"
```

Jména kanálů pro konfiguraci vznikají z hlaviček: `Thrust [N]` → `thrust`,
`Pyro Active` → `pyro_active`. Jednotka v hranatých nebo kulatých závorkách se
z názvu odřízne a použije se jako popisek.

Když každý logger pojmenovává tutéž veličinu jinak, dá se místo jednoho názvu
uvést **seznam kandidátů** – použije se první, který v souboru existuje.
Přiložené šablony to tak dělají, takže `rocket_thrust` funguje na obou
vzorových souborech, přestože jeden má `pyro_active` a druhý `pyro_energised`:

```yaml
- type: led
  channel: [pyro_active, pyro_energised, pyro]
  optional: true      # chybí-li všechny, widget se jen vynechá
```

## Struktura projektu

```
telemetry_overlay/
├── data.py         načtení CSV/XLSX, detekce formátu
├── timeline.py     mapování času dat na snímky (rychlost, rampy, zámrazy)
├── expressions.py  dopočítané kanály (uzavřená sada funkcí)
├── events.py       detekce zážehu, vrcholu, dohoření, hran
├── config.py       čtení a validace konfigurace
├── render.py       skládání snímků
├── export.py       zápis videa a PNG sekvencí
├── theme.py        barvy, geometrie, kreslicí primitiva
├── fonts.py        hledání fontů napříč systémy
└── widgets/        label, readout, table, timer, graph, bar, gauge, led, image
presets/            hotové šablony
examples/           vzorová data
docs/               podrobná dokumentace
tests/              130 testů
```

## Testy

```bash
pip install -e ".[dev]"
python -m pytest -q
```

## Licence

MIT, viz [LICENSE](LICENSE).

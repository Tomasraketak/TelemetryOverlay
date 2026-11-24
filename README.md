# Telemetry Overlay 2.0

Univerzální open-source **extraktor, konvertor a generátor animovaných telemetrických overlayů**.
Zdarma, plně offline, bez nahrávání dat na servery, pro Windows, macOS i Linux.

Zvládá vše, co placený **GoPro Telemetry Extractor + Telemetry Overlay**, a přidává navíc plnou podporu pro **vlastní vědecké/raketové loggery**, skriptovatelné výrazy, pokročilé filtrování signálů, rampy zpomalení času, dávkové zpracování i lokální interaktivní webové studio.

> English summary: All-in-one offline telemetry extractor, format converter and
> high-speed animated overlay renderer supporting GoPro MP4 (GPMF), DJI SRT/MP4,
> Garmin/Wahoo FIT, GPX, KML/KMZ, TCX, NMEA 0183, FAI IGC, JSON/GeoJSON,
> CSV/TSV (RaceChrono/AiM/custom loggers) and XLSX. Renders alpha-transparent
> masters (ProRes 4444, QTRLE, DNxHR, VP9, WebP, GIF, PNG sequence) or composites
> directly onto source footage with audio passthrough.

---

## Co všechno umí

### 1. Vstupní formáty (11 rodin, nulové externí binárky)
- **Akční kamery a drony**: **GoPro MP4/MOV/LRV** (nativní binární parser GPMF: `GPS5`/`GPS9`, `ACCL`, `GYRO`, `CORI` pitch/roll/yaw, `TMPC`, `ISOE`, `SHUT`), **DJI SRT** (samostatné `.srt` i vestavěné titulky v MP4/MOV).
- **GPS, cyklistika, motorsport, letectví**: **Garmin / Wahoo `.FIT`** (čistý Python binární parser), **GPX 1.0/1.1** (včetně tepu, kadence, výkonu, teploty), **KML / KMZ**, **Garmin TCX**, **NMEA 0183** (včetně námořního větru `$xxMWV`), **FAI IGC** (plachtění/letectví).
- **Vlastní loggery a tabulky**: **CSV / TSV** (autodetekce oddělovače, desetinné čárky, ISO-8601 a `HH:MM:SS` časů, hlaviček RaceChrono/AiM, komentářů i **textových/enum sloupců** jako fáze letu nebo zařazený stupeň), **XLSX** (včetně listu s metadaty), **JSON / JSONL / GeoJSON**.
- **Více zdrojů najednou**: automatické spojování navazujících kapitol (např. GoPro klipy) i **slučování senzorů** (`--merge garmin.fit` s automatickou synchronizací podle UTC nebo ručním offsetem).

### 2. Extrakce a konverze dat (`extract`)
Nahrazuje *Telemetry Extractor* – z libovolného vstupu (GoPro MP4, FIT, GPX, DJI SRT, CSV, XLSX…) vyexportuje vyčištěná a dopočítaná data do:
- **CSV**, **JSON**, **GPX 1.1**, **Google Earth 3D KML**, **GeoJSON** (GIS), **Adobe After Effects `.mgjson`** a **SRT titulků**.

### 3. Automatické dopočty, kola a čištění signálu
- **Automatická GPS/IMU analýza**: u GPS logů sám dopočítá vzdálenost, rychlost (`m/s`, `km/h`, `mph`), azimut (`bearing`), sklon (`grade %`), vertikální rychlost (`vspeed`), nastoupané metry (`elevation_gain`), podélné/příčné/celkové přetížení (`g_lon`, `g_lat`, `g_total`) a náklon (`lean_angle`).
- **Automatická detekce kol (`--laps`)**: najde průjezdy cílovou čárou na okruhu a spočítá `lap`, `lap_time`, `last_lap`, `best_lap` a `lap_delta`.
- **Filtry signálu**: mediánové odstranění špiček (`despike`), klouzavý průměr (`smooth`), exponenciální vyhlazení (`ema`), dolní propust (`lowpass`), GPS filtr skoků (`gps_filter`), převzorkování (`resample`).
- **Bezpečné AST výrazy (`derived:` i přímo ve widgetech)**: `integral`, `derivative`, `smooth`, `ema`, `median_filter`, `despike`, `lowpass`, `norm`, `haversine_dist`, převody jednotek (`to_kmh`, `to_mph`, `to_knots`, `to_ft`, `to_psi`, `to_kgf`, …).

### 4. 22 typů widgetů, 10 motivů a 10 hotových šablon
- **Klasické & vědecké**: `label` (i šablony `{meta.*}`), `readout` (čísla, text i inline výrazy + barevné zóny), `table`, `timer` (misijní `T+` i reálný UTC čas), `graph` (gradientní výplň, legenda, plný i posuvný režim), `bar` (i obousměrný od nuly), `gauge` (s peak značkou i závodními shift-lights), `led`, `image` (včetně rotace podle libovolného kanálu!).
- **Profi prostorové, GPS, motorsport & letecké**:
  - `map` – GPS trasa s tepelnou mapou rychlosti/výšky, měřítkem, směrovou šipkou i follow-zoom režimem,
  - `gforce` – 2D G-force kruh s mizící stopou a soustřednými kružnicemi,
  - `attitude` – letecký umělý horizont (pitch ladder, náklonový oblouk, zaměřovač),
  - `compass` – letecký/dronový posuvný kurzový pásek nebo otočná kompasová růžice,
  - `tape` – posuvný vertikální žebřík (rychlost, výška, Mach, tah),
  - `lean` – ukazatel náklonu motorky/letadla s L/P maximy,
  - `elevation` – výškový profil trasy se sklonem `%` a převýšením,
  - `sparkline` – kompaktní vysílací KPI karta s minigrafem,
  - `pedals` – plyn / brzda / spojka + otočný volant + zařazený stupeň,
  - `sticks` – FPV/RC dvojice kniplů se stopou pohybu,
  - `lap_timer` – stopky kola, nejlepší/poslední kolo a live delta,
  - `progress` – časová lišta se značkami událostí a indikátorem zpomalení (`0.25x SLOW-MO`),
  - `badge` – barevný štítek pro textové/stavové kanály (fáze letu, stupeň, GPS fix).

### 5. Rychlý export s alfou i přímé spojení se záběrem
- **Průhledné video**: ProRes 4444, QuickTime RLE (`qtrle`), Avid **DNxHR 444**, PNG-in-MOV, VP9/WebM, animované **WebP** a **GIF**, nebo PNG sekvence.
- **Chroma-key**: `--chroma green` nebo `--chroma blue`.
- **Přímé vypálení na video (`--video`)**: spojí overlay rovnou se zdrojovým videem včetně bezztrátového zachování zvuku a synchronizace `--sync`.
- **Optimalizace výkonu**: kešování statických vrstev a textů se stínem, sloučené grafové pásy, O(1) peak-hold, deduplikace snímků během zámrazů (`holds`) a paralelní segmentový render (60+ fps v 1080p na jediném jádru).

### 6. Interaktivní lokální Web Studio (`studio`)
Bez instalace Electronu nebo dalších závislostí – spustí čistý lokální náhled v prohlížeči se scrubbováním časové osy, přepínáním šablon/motivů, živým editorem YAML a jednoklikovým exportem dat.

---

## Instalace

```bash
git clone https://github.com/Tomasraketak/TelemetryOverlay.git
cd TelemetryOverlay
pip install -e .
```

Součástí instalace je `imageio-ffmpeg` s vlastním binárním `ffmpeg`, takže export videa i přímé spojování se záběrem funguje okamžitě. Vyžaduje Python 3.10+.

---

## Rychlý start

```bash
# 1) Prozkoumej libovolný log (CSV, XLSX, GPX, FIT, GoPro MP4, DJI SRT, IGC, NMEA...)
telemetry-overlay inspect examples/static_fire_thrust.csv
telemetry-overlay inspect examples/circuit_lap.gpx --laps

# 2) Vygeneruj chytrou konfiguraci podle typu dat (raketa, kolo, motorsport, dron, letadlo)
telemetry-overlay init examples/circuit_lap.gpx -o overlay.yaml

# 3) Otevři interaktivní Web Studio v prohlížeči
telemetry-overlay studio examples/circuit_lap.gpx

# 4) Rychlý náhled jednoho snímku
telemetry-overlay preview examples/circuit_lap.gpx -c motorsport --at 65% -o preview.png

# 5) Vyrenderuj průhledné video pro DaVinci Resolve (QTRLE je nejrychlejší a nejmenší)
telemetry-overlay render examples/static_fire_thrust.csv -c rocket_thrust -o overlay.mov --codec qtrle

# 6) Nebo rovnou vypal overlay přes zdrojové GoPro / DJI video (včetně zvuku)
telemetry-overlay render GX010001.MP4 -c action_cam --video GX010001.MP4 -o final.mp4

# 7) Extrahuj telemetrii z kamery / FIT / SRT do GPX, KML, GeoJSON, CSV nebo After Effects MGJSON
telemetry-overlay extract GX010001.MP4 -o track.gpx
telemetry-overlay extract examples/circuit_lap.gpx -o ae_data.mgjson

# 8) Dávkové zpracování více souborů najednou
telemetry-overlay batch *.gpx -c cycling -o renders/ --codec qtrle
telemetry-overlay batch *.MP4 --extract-to gpx -o tracks/
```

Přehled všeho dostupného přímo v terminálu:

```bash
telemetry-overlay presets   # 10 hotových šablon
telemetry-overlay widgets   # 22 widgetů a 10 barevných motivů
telemetry-overlay formats   # 11 vstupních, 7 exportních a 9 video formátů
```

---

## Formáty výstupního videa

| Výstup / `--codec` | Kodek | Alfa | Poznámka |
|---|---|---|---|
| `overlay.mov` | `prores4444` | ano | Výchozí. 16bit alfa, standard pro Resolve / Premiere / FCP. |
| `overlay.mov --codec qtrle` | `qtrle` | ano | Bezztrátové, **několikanásobně menší a rychlejší** pro overlaye. |
| `overlay.mov --codec dnxhr` | `dnxhr` | ano | Avid DNxHR 444 s alfou pro Resolve/Avid. |
| `overlay.webm` | `vp9` | ano | Pro web a OBS. |
| `overlay.webp` / `.gif` | `webp` / `gif` | ano | Kompaktní animace pro web a sdílení. |
| `final.mp4 --video src.mp4` | `h264` / `h265` | – | Přímé sloučení se záběrem + kopie zvuku. |
| `frames/` | PNG sekvence | ano | Jednotlivé snímky bez potřeby ffmpegu. |

---

## Dokumentace

- **[docs/CONFIG.md](docs/CONFIG.md)** – kompletní reference konfigurace YAML/JSON, všech 22 widgetů, motivů, filtrů a výrazů.
- **[docs/DAVINCI_RESOLVE.md](docs/DAVINCI_RESOLVE.md)** – import průhledného videa i PNG sekvencí do DaVinci Resolve a sladění ramp zpomalení.

## Struktura projektu

```
telemetry_overlay/
├── data.py          datový model (Channel, Telemetry, LoadOptions, load, load_many)
├── formats/         CSV/XLSX/JSON/GeoJSON, GPX/KML/TCX/NMEA/IGC, Garmin FIT,
│                    DJI SRT/MP4, GoPro GPMF MP4, auto-enrichment, lap detection, filtry
├── extract.py       export do CSV, JSON, GPX, KML, GeoJSON, After Effects MGJSON, SRT
├── timeline.py      mapování času na snímky (rychlost, rampy, zámrazy)
├── expressions.py   AST-sandboxované výrazy a knihovna matematických/signálových funkcí
├── events.py        detekce zážehu, vrcholu, dohoření, hran i okruhových kol
├── config.py        validace konfigurace a chytrý generátor šablon (init)
├── render.py        optimalizovaný kompozitor snímků
├── export.py        paralelní video export, přímé spojování se záběrem, PNG sekvence
├── studio.py        lokální interaktivní Web Studio (zero-dependency)
├── theme.py         10 motivů, barevné mapy, alpha-safe kreslicí primitiva a keš textů
├── fonts.py         hledání fontů napříč OS
└── widgets/         22 widgetů (text, graph, indicators, spatial, telemetry_pro)
presets/             10 šablon (rocket_thrust, rocket_flight, motorsport, cycling,
                     aviation, drone_fpv, action_cam, broadcast, hud, minimal)
examples/            vzorová data (CSV, XLSX, GPX, DJI SRT)
tests/               201 automatických testů
```

## Testy

```bash
pip install -e ".[dev]"
python -m pytest -q
```

## Licence

MIT, viz [LICENSE](LICENSE).

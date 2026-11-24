# Konfigurace

Konfigurace je YAML (nebo JSON, podle přípony). Nejrychlejší start:

```bash
telemetry-overlay init muj_log.gpx -o overlay.yaml
```

Neznámá sekce nebo neznámý klíč se ohlásí jako chyba i s výpisem toho, co je
povolené – překlep se nikdy neprojeví tichým ignorováním.

## Kostra

```yaml
name: Popisek projektu       # nepovinné, jen pro přehled
output:   { … }              # rozlišení, fps, pozadí
time:     { … }              # rychlost, rampy, zámrazy
theme:    { … }              # barvy, palety a fonty
data:     { … }              # formát, synchronizace, kola, slučování souborů
filters:  [ … ]              # čištění a vyhlazování signálu
derived:  [ … ]              # dopočítané kanály (AST výrazy)
events:   true               # značky v grafu a časové liště
widgets:  [ … ]              # samotný overlay (22 typů)
```

## `output`

| Klíč | Výchozí | Význam |
|---|---|---|
| `resolution` | `1080p` | `720p`, `1080p`, `1440p`, `2k`, `4k`, `5.3k`, `8k`, `vertical1080`, `vertical4k`, `square1080` nebo `WxH` |
| `fps` | `60` | Snímková frekvence; musí sedět na projekt ve střihu |
| `background` | – | Vyplní pozadí místo průhlednosti, např. `"#000000"`, `"chroma_green"` |
| `max_duration` | `900` | Pojistka proti omylem nekonečnému renderu (v sekundách) |

Rozvržení se zadává vůči plátnu **1920 × 1080** a při jiném rozlišení se automaticky přeškáluje.

## `time`

```yaml
time:
  speed: 1.0        # 1 = reálný čas, 0.25 = 4× pomaleji, 2 = 2× rychleji
  start: -0.2       # od kdy (v sekundách dat); výchozí je začátek logu
  end: 6.0          # dokdy
  t0: 1.8           # okamžik zobrazený jako „T+0"
  ramps:
    - { from: 1.6, to: 2.6, speed: 0.2, ease: 0.15, label: zážeh }
  holds:
    - { at: 2.30, duration: 1.5, label: vrchol }
```

## `theme`

10 předpřipravených motivů:
`night`, `day`, `hud`, `flame`, `clean`, `motorsport`, `aviation`, `cycling`, `neon`, `minimal_light`.

Palety pro křivky: `default`, `rocket`, `mono`, `hot`, `motorsport`, `aviation`, `cycling`, `cyber`, `ocean`.
Barevné mapy (např. pro GPS trasu `map`): `speed`, `thermal`, `elevation`, `ocean`, `rdylgn`, `cyber`.

```yaml
theme:
  base: motorsport
  accent: "#ef4444"
  palette: motorsport
```

Barvy podporují `#rgb`, `#rrggbb`, `#rrggbbaa`, pojmenované barvy i ztlumení `"#ffffff @ 40%"`.

## `data`

```yaml
data:
  format: gpx          # volitelné vynucení: csv, xlsx, json, gpx, kml, tcx, fit, nmea, igc, srt, gpmf
  time_column: "Cas"
  time_unit: ms        # s | ms | us | ns
  delimiter: ";"
  decimal: ","
  encoding: cp1250
  sheet: "Data"        # list v XLSX
  skip_rows: 3         # přeskočit úvodní řádky přístroje
  enrich: true         # automatický dopočet GPS/IMU veličin
  detect_laps: true    # automatická detekce okruhových kol z GPS
  time_offset: 0.0     # posun časové osy v sekundách
  merge:
    - { path: garmin_hr.fit }            # automaticky sladěno podle UTC času
    - { path: extra_imu.csv, offset: 0.4, prefix: imu_ }
```

## `filters`

Vyhlazování a čištění signálu před výpočtem `derived` a renderem:

```yaml
filters:
  - { type: gps_filter, max_speed: 100 }               # odstraní nemožné GPS skoky
  - { type: despike, channel: altitude, window: 5, sigma: 3.5 }
  - { type: smooth, channel: [speed, speed_kmh], window: 5 }
  - { type: lowpass, channel: g_lat, cutoff_hz: 2.5 }
  - { type: ema, channel: power, alpha: 0.25 }
  - { type: clip, channel: grade, min: -30, max: 30 }
  - { type: resample, hz: 30 }
```

## `derived` (a inline `expression:`)

Výrazy běží v přísném **AST sandboxu** (žádný přístup k atributům ani importům, ale bezpečně dovolí i kanály jako `valve_open`).

```yaml
derived:
  - key: impulse
    label: Celkový impuls
    unit: "N·s"
    expression: integral(clip(thrust, 0, 1e9))
  - key: alt_ft
    label: Altitude
    unit: ft
    expression: to_ft(altitude)
    optional: true     # přeskočí se, pokud zdrojový kanál chybí
```

**Funkce:**
- Časové a signálové: `integral`, `derivative`, `smooth(x, n)`, `ema(x, alpha)`, `median_filter(x, n)`, `despike(x, n, sigma)`, `lowpass(x, cutoff_hz)`, `running_max`, `running_min`, `cumsum`, `diff`
- Matematické & geometrické: `abs`, `sqrt`, `exp`, `log`, `log10`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `hypot`, `degrees`, `radians`, `floor`, `ceil`, `round`, `sign`, `minimum`, `maximum`, `clip`, `where`, `nan_to_num`, `norm`, `normalize`, `haversine_dist`, `mean`, `std`, `min_val`, `max_val`
- Převody jednotek: `to_kmh`, `to_mph`, `to_knots`, `to_ft`, `to_psi`, `to_bar`, `to_lbf`, `to_kgf`, `c_to_f`, `f_to_c`
- Konstanty: `pi`, `e`, `g` (`9.80665`), `t` / `time`

Inline `expression:` funguje přímo na `readout`, řádcích `table`, `bar`, `gauge`, `graph`, `sparkline` i `tape`.

## `events`

```yaml
events:
  - { detect: [thrust, force], fraction: 0.05, only: [ignition, peak, burnout] }
  - { edges: [pyro_active, pyro], label: PYRO, edge: rising, optional: true }
  - { laps: true, color: "#facc15" }
  - { time: 2.75, label: ODHOZ, color: "#38bdf8" }
```

## `widgets` (22 typů)

Společné vlastnosti: `type`, `name`, `x`, `y`, `width`, `height`, `anchor`, `opacity`, `visible`, `optional`, `show_from`, `show_to`, `fade`, `panel`, `panel_color`, `panel_border`, `corner_radius`, `border_width`, `text_shadow`.

Každý kanál lze zadat jako jeden název nebo seznam alternativ (`[speed_kmh, speed]`).

### Základní a vědecké widgety
- **`label`** – statický text nebo dynamická šablona (`{meta.motor}`, `{speed:.2f}x`, `{time:.2f}`, `{event}`).
- **`readout`** – velké číslo (i textový kanál nebo inline `expression:`), `peak_hold`, `peak_mode: min|max`, barevné `zones`.
- **`table`** – tabulka hodnot (čísla, text i výrazy, `peak_hold`, `zones`).
- **`timer`** – misijní hodiny (`t+`, `t+mmss`, `mmss`, `hhmmss`, `plain`) nebo reálný UTC čas (`clock`, `utc`, `datetime`), `show_speed: true`.
- **`graph`** (alias `plot`, `chart`) – živá křivka (`mode: full | window`), `gradient: true`, `legend: true`, `ghost`, `glow`, `events`, `show_value`.
- **`bar`** – sloupcový ukazatel (`horizontal | vertical`), `bidirectional: true` (od nuly na obě strany), `ticks`, `peak_marker`, `zones`.
- **`gauge`** (alias `dial`, `speedometer`, `tachometer`) – kruhový budík, `show_scale`, `peak_marker`, `shift_lights: true` (závodní LED oblouk), `zones`.
- **`led`** – stavová kontrolka se září (`threshold`, `invert`, `on_text`, `off_text`).
- **`image`** (alias `logo`, `icon`) – logo nebo **otočná ikona** řízená telemetrií (`rotate_channel`, `rotate_scale`, `rotate_offset`).

### Prostorové, GPS, motorsport, cyklistika, letectví & FPV
- **`map`** (alias `gps_track`, `route`) – GPS mapa s tepelnou škálou (`color_channel`, `colormap`), `mode: full | follow`, `follow_radius`, `scale_bar`, `north_arrow`, `legend`.
- **`gforce`** (alias `g_ball`, `accel`) – 2D G-force kruh (`x_channel`, `y_channel`, `max_g`, `rings`, `trail`).
- **`attitude`** (alias `horizon`, `artificial_horizon`) – letecký umělý horizont (`pitch_channel`, `roll_channel`, `pitch_range`, `sky_color`, `ground_color`).
- **`compass`** (alias `heading`) – posuvná hlavičková páska (`style: tape`, `fov`) nebo otočný kompas (`style: rose`).
- **`tape`** (alias `ladder`) – posuvný vertikální žebřík (`channel`, `window`, `step`, `side: left | right`).
- **`lean`** (alias `bank`) – ukazatel náklonu (`max_angle`, `peak_hold`).
- **`elevation`** (alias `profile`) – výškový profil trasy (`channel`, `grade_channel`, `gain_channel`, `distance_channel`).
- **`sparkline`** (alias `mini_graph`) – kompaktní KPI karta s hodnotou a živým minigrafem.
- **`pedals`** (alias `inputs`) – plyn, brzda, spojka, otočný volant a zařazený stupeň.
- **`sticks`** (alias `rc_sticks`, `gimbal`) – FPV/RC dvojice thumbsticků se stopou pohybu (automaticky rozpozná i PWM `1000..2000`).
- **`lap_timer`** (alias `laps`) – číslo kola, aktuální/nejlepší/poslední čas kola a live delta.
- **`progress`** (alias `timeline_bar`) – časová lišta klipu s event piny a `speed_badge` (`0.25x SLOW-MO` / `HOLD`).
- **`badge`** (alias `status`, `mode`) – barevný štítek pro textové/enum nebo diskrétní kanály (`colors: { boost: "#ef4444", ... }`).

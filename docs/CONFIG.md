# Konfigurace

Konfigurace je YAML (nebo JSON, podle přípony). Nejrychlejší start:

```bash
telemetry-overlay init muj_log.csv -o overlay.yaml
```

Neznámá sekce nebo neznámý klíč se ohlásí jako chyba i s výpisem toho, co je
povolené – překlep se nikdy neprojeví tichým ignorováním.

## Kostra

```yaml
name: Popisek projektu       # nepovinné, jen pro přehled
output:   { … }              # rozlišení, fps, pozadí
time:     { … }              # rychlost, rampy, zámrazy
theme:    { … }              # barvy a fonty
data:     { … }              # jen když selže autodetekce
derived:  [ … ]              # dopočítané kanály
events:   true               # značky v grafu
widgets:  [ … ]              # samotný overlay
```

## `output`

| Klíč | Výchozí | Význam |
|---|---|---|
| `resolution` | `1080p` | `720p`, `1080p`, `1440p`, `4k`, `8k`, `vertical1080`, `square1080` nebo `2560x1440` |
| `fps` | `60` | Snímková frekvence; musí sedět na projekt ve střihu |
| `background` | – | Vyplní pozadí místo průhlednosti, např. `"#000000"` |
| `max_duration` | `900` | Pojistka proti omylem nekonečnému renderu (v sekundách) |

Rozvržení se zadává vůči plátnu **1920 × 1080**. Při jiném rozlišení se celý
layout přeškáluje poměrem šířek, takže konfigurace psaná pro 1080p sedí i ve 4K.

## `time`

```yaml
time:
  speed: 1.0        # 1 = reálný čas, 0.25 = 4× pomaleji, 2 = 2× rychleji
  start: -0.2       # od kdy (v sekundách dat); výchozí je začátek logu
  end: 6.0          # dokdy
  t0: 1.8           # který okamžik se zobrazuje jako „T+0"
  ramps:
    - from: 1.6     # úsek hraný jinou rychlostí
      to: 2.6
      speed: 0.2
      ease: 0.15    # plynulý náběh na obou koncích, v sekundách dat
      label: zážeh
  holds:
    - at: 2.30      # zámraz: video běží, data stojí
      duration: 1.5
      label: vrchol
```

Rampy se aplikují v pořadí zápisu; kde se překrývají, vyhrává pozdější.
Rychlost musí být kladná – k zastavení slouží `holds`.

Mapování je integrál `1/rychlost` přes čas dat, takže výsledek je vždy
monotónní: přejezd v grafu se nikdy nevrátí zpět.

## `theme`

```yaml
theme:
  base: night       # night | day | hud | flame | clean
  accent: "#ff9f0a"
  palette: rocket   # default | rocket | mono | hot
```

| Motiv | K čemu |
|---|---|
| `night` | Výchozí. Bílý text na tmavé podložce, čitelný na čemkoliv. |
| `day` | Tmavý text na světlé podložce, pro přesvětlené záběry. |
| `hud` | Přístrojový vzhled, tenké linky, neproporcionální písmo. |
| `flame` | Bez podložek, silný stín – přímo přes plamen. |
| `clean` | Vysílací styl, zaoblené panely. |

Přebít se dá kterýkoliv klíč: `text`, `text_dim`, `accent`, `good`, `warn`,
`bad`, `panel`, `panel_border`, `grid`, `axis`, `shadow`, `font`, `font_bold`,
`font_mono`, `corner_radius`, `shadow_blur`, `shadow_offset`.

**Barvy** se zapisují jako `#rgb`, `#rrggbb`, `#rrggbbaa`, jménem (`white`,
`red`, `amber`, …) nebo se ztlumením: `"#ffffff @ 40%"`.

**Fonty**: logické názvy `sans`, `sans-bold`, `mono`, `mono-bold`, `condensed`,
`condensed-bold`, konkrétní rodina (`Roboto-Bold`) nebo přímo cesta k `.ttf`.
Když rodina v systému není, sáhne se po nejbližší náhradě, aby render nespadl.

## `data`

Potřeba jen když autodetekce selže:

```yaml
data:
  time_column: "Cas"
  time_unit: ms       # s | ms | us
  delimiter: ";"
  decimal: ","
  encoding: cp1250
  sheet: "Data"       # list v XLSX
  skip_rows: 3        # přeskočit hlavičku přístroje
```

## `derived`

Veličiny, které logger nezaznamenal, ale jdou z dat spočítat:

```yaml
derived:
  - key: impulse
    label: Celkový impuls
    unit: "N·s"
    expression: integral(clip(thrust, 0, 1e9))
  - key: thrust_kgf
    expression: thrust / g
```

Definice se vyhodnocují v pořadí zápisu, takže pozdější může stavět na dřívější.

**Dostupné funkce:** `integral`, `derivative`, `smooth(x, n)`, `running_max`,
`running_min`, `clip(x, lo, hi)`, `abs`, `sqrt`, `exp`, `log`, `log10`, `sin`,
`cos`, `tan`, `atan`, `atan2`, `floor`, `ceil`, `round`, `sign`, `minimum`,
`maximum`, `where`, `nan_to_num`.
**Konstanty:** `pi`, `e`, `g` (9,80665), `t` / `time` (čas v sekundách).

Výrazy běží v uzavřeném prostředí – dostupné jsou jen kanály a funkce výše,
nic jiného. Neznámé jméno se ohlásí i se seznamem toho, co existuje.

## `events`

```yaml
events: true            # automaticky: zážeh, vrchol, dohoření, hrany
```

```yaml
events: false           # žádné značky
```

```yaml
events:
  - detect: thrust      # zážeh/vrchol/dohoření z prahu 5 % vrcholu
    fraction: 0.05
    only: [ignition, peak]
    color: "#ff9f0a"
  - edges: pyro_active  # hrany booleovského kanálu
    label: PYRO
    edge: rising        # rising | falling | both
  - time: 2.75          # ruční značka
    label: ODHOZ
```

## `widgets`

Společné klíče pro všechny typy:

| Klíč | Význam |
|---|---|
| `type` | Povinné. `label`, `readout`, `table`, `timer`, `graph`, `bar`, `gauge`, `led`, `image` |
| `name` | Popisek do výpisů a chybových hlášek |
| `x`, `y` | Pozice: `120` (px), `"6%"` nebo `0.06` (podíl) |
| `width`, `height` | Rozměr, stejné jednotky |
| `anchor` | Ke kterému rohu se pozice vztahuje: `top-left` (výchozí), `top-center`, `middle-center`, `bottom-right`, … |
| `opacity` | 0–1 |
| `show_from`, `show_to` | Zobrazit jen v tomto úseku dat (v sekundách) |
| `fade` | Délka prolnutí na okrajích úseku |
| `panel` | `false` vypne podložku |
| `panel_color`, `panel_border`, `corner_radius` | Vzhled podložky |
| `text_shadow` | `false` vypne stín textu |

### Kanál pod více jmény

Každý widget i pravidlo v `events` přijme místo jednoho názvu **seznam
kandidátů**; použije se první, který v logu existuje. Díky tomu jedna
konfigurace pokryje loggery, které tutéž veličinu pojmenovaly jinak:

```yaml
- type: led
  channel: [pyro_active, pyro_energised, pyro]
  optional: true     # když žádný z nich není, widget se prostě vynechá
```

Bez `optional: true` je chybějící kanál chyba i s výpisem dostupných jmen –
překlep se tedy pořád pozná. `optional` omlouvá jen chybějící kanál, nikoliv
neznámý typ widgetu nebo jinou chybu v zápisu.

Stejně to funguje v `events`:

```yaml
events:
  - detect: [thrust, thrust_above_rest, force]
  - edges: [pyro_active, pyro_energised]
    optional: true
```

### `label` – statický text

```yaml
- type: label
  text: Statický zážeh
  x: 4%
  y: 6%
  size: 40
  align: left        # left | center | right
  tracking: 5        # rozpal písmen
  uppercase: true
```

### `readout` – velké číslo

```yaml
- type: readout
  channel: thrust
  label: Tah
  unit: N            # výchozí je jednotka z hlavičky
  format: "{:.1f}"
  x: 4%
  y: 22%
  value_size: 92
  peak_hold: true    # drží dosud největší hodnotu
  align: left
```

### `table` – několik hodnot pod sebou

```yaml
- type: table
  x: 66%
  y: 76%
  width: 31%
  size: 26
  rows:
    - { channel: thrust, label: Tah, format: "{:.1f}" }
    - { channel: thrust, label: Maximum, peak_hold: true }
    - { channel: impulse, label: Impuls, format: "{:.0f}" }
```

Řádek s `peak_hold: true` drží dosud největší hodnotu; s `peak_mode: min`
naopak nejmenší. Totéž umí i `readout`.

### `timer` – misijní hodiny

```yaml
- type: timer
  x: 4%
  y: 11%
  size: 54
  style: t+          # t+ | t+mmss | mmss | hhmmss | plain
  decimals: 2
  zero: 1.8          # vlastní nula, jinak platí time.t0
```

### `graph` – živý graf

```yaml
- type: graph
  title: Křivka tahu
  series:
    - channel: thrust
      color: "#ff9f0a"
      width: 3.5
      fill: true
      fill_opacity: 0.22
  x: 4%
  y: 60%
  width: 56%
  height: 33%
  mode: full         # full = celý úsek, window = posuvné okno
  window: 2.5        # šířka okna v sekundách (jen pro window)
  playhead_pos: 1.0  # kde v okně stojí přejezd, 0–1
  y_min: 0
  y_max: 300         # jinak se dopočítá z dat
  show_value: true   # hodnota u bodu na křivce
  ghost: true        # předkreslený zbytek křivky
  glow: false
  grid: true
  events: true
  x_label: "t [s]"
  y_label: "N"
```

Křivka se rastruje **jednou** do širokého pásu a každý snímek je jen jeho výřez
oříznutý přejezdem. Proto je render rychlý i ve 4K.

### `bar` – sloupcový ukazatel

```yaml
- type: bar
  channel: thrust
  x: 70%
  y: 18%
  width: 26%
  orientation: horizontal   # horizontal | vertical
  thickness: 20
  ticks: 4
  peak_marker: true
  min: 0
  max: 300
  zones:
    - { from: 200, to: 270, color: "#ffd60a" }
```

### `gauge` – ručičkový budík

```yaml
- type: gauge
  channel: thrust
  x: 88%
  y: 8%
  anchor: top-center
  width: 260
  height: 260
  min: 0
  max: 300
  start_angle: 135
  sweep: 270
  show_scale: true
  zones:
    - { from: 0, to: 200, color: "#34c759" }
    - { from: 200, to: 300, color: "#ff453a" }
```

### `led` – stavová kontrolka

```yaml
- type: led
  channel: pyro_active
  x: 4%
  y: 47%
  threshold: 0.5
  invert: false
  on_text: Pyro pod proudem
  off_text: Pyro vypnuto
  size: 15
  glow: true
```

### `image` – logo

```yaml
- type: image
  path: logo.png
  x: 92%
  y: 88%
  anchor: bottom-right
  width: 180          # výška se dopočítá podle poměru stran
```

## Přebití z příkazové řádky

Parametry z příkazové řádky mají přednost před souborem:

```bash
telemetry-overlay render data.csv -c overlay.yaml -o overlay.mov \
    -r 4k --fps 60 --speed 0.5 --start 1.0 --end 4.0 \
    --ramp 1.6:2.6:0.2:0.15 --hold 2.3:1.0
```

`--ramp` a `--hold` se k rampám z konfigurace **přidávají**, nenahrazují je.

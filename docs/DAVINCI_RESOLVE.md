# Import do DaVinci Resolve

Platí pro **DaVinci Resolve 18 a 19, včetně bezplatné verze**, na Windows,
macOS i Linuxu.

---

## 1. Co vyrenderovat

```bash
telemetry-overlay render data.csv -c rocket_thrust -o overlay.mov --codec qtrle
```

QuickTime Animation (`qtrle`) je bezztrátový, má alfa kanál a protože je
overlay z většiny průhledný, vyjde několikanásobně menší a rychleji než ProRes.
ProRes 4444 (`-o overlay.mov` bez `--codec`) je konzervativnější volba, kterou
očekává většina střihových pracovišť.

**Na Windows s bezplatnou verzí** může být přehrávání ProRes vrtkavé. Pokud
narazíte, použijte `--codec qtrle`, nebo PNG sekvenci:

```bash
telemetry-overlay render data.csv -c rocket_thrust -o frames/
```

## 2. Import

**Video (.mov / .webm)**

1. `File → Import → Media`, vyberte soubor.
2. Přetáhněte ho na časovou osu na stopu **nad** záběr (Video 2 nad Video 1).

**PNG sekvence**

1. `File → Import → Media`.
2. Označte **první** snímek (`overlay_00000.png`).
3. Resolve podle číslování načte celou sekvenci jako **jeden klip**.

Pokud by se snímky naimportovaly jako stovky samostatných klipů, zapněte
`Preferences → User → Editing → Standard still image duration` a hlavně
`Media Storage → Automatically detect image sequences` (výchozí je zapnuto).

## 3. Průhlednost

Alfa kanál se u těchto formátů použije automaticky – overlay na vyšší stopě
prosvítá na záběr pod sebou.

**Když je pozadí overlaye černé:**

1. V Media Poolu klikněte na klip pravým tlačítkem → `Clip Attributes`.
2. Záložka `Video`, položka **Alpha Mode**.
3. Přepněte na **Premultiplied** (u `Straight` zkuste druhou možnost).

Overlay renderuje s *nepremultiplikovanou* (straight) alfou. Když okraje textu
vypadají tmavě orámované, je nastavený špatný režim – přepněte na druhý.

**Když se overlay vůbec nezobrazí:** ověřte, že je na **vyšší** stopě než
záběr. Resolve skládá stopy zdola nahoru.

## 4. Sladění zpomalení se záběrem

`--speed` a `--ramp` mění tempo **jen u overlaye**. Aby seděl na obraz, musí se
stejně zpomalit i záběr.

### Konstantní zpomalení

Overlay:

```bash
telemetry-overlay render data.csv -c rocket_thrust -o overlay.mov --speed 0.25
```

Záběr v Resolve: pravým tlačítkem na klip → `Retime Controls` → rozbalovací
šipka → **25%**. Overlay i záběr teď mají stejnou délku.

Pro plynulý pohyb u vysokorychlostních záběrů zapněte
`Clip Attributes → Retime Process → Optical Flow`.

### Rampy (zpomalení jen v části)

Overlay:

```bash
telemetry-overlay render data.csv -c rocket_thrust -o overlay.mov \
    --ramp 1.6:2.6:0.2:0.15
```

Záběr v Resolve:

1. `Retime Controls` na klipu se záběrem.
2. Ve značce času přidejte **Retime Ramp** na začátku i konci úseku (u nás
   1,6 s a 2,6 s od začátku dat).
3. Prostřednímu úseku nastavte stejnou rychlost, tedy **20 %**.

`telemetry-overlay render … --dry-run` vypíše, jak dlouhé video vznikne a kde
přesně rampy leží – podle toho se dá retime v Resolve nastavit přesně.

> Jednodušší varianta: záběr si zpomalte v Resolve, jak chcete, a overlay
> vyrenderujte v **reálném čase** (`--speed 1`, bez ramp). Pak na overlay
> aplikujte v Resolve úplně stejný retime jako na záběr. Odpadá dopočítávání.

## 5. Sesazení začátku

Data obvykle začínají dřív než záběr (v ukázce od −0,149 s). Máte tři možnosti:

- **Oříznout data:** `--start 0 --end 4` vyrenderuje jen tento úsek.
- **Posunout klip** na časové ose ručně, až sedne zážeh.
- **Posunout nulu hodin:** `--t0 1.8` udělá z okamžiku 1,8 s „T+0", takže
  hodiny i osa grafu odpočítávají od zážehu.

Nejlepší synchronizační bod je obvykle první viditelný plamen v záběru proti
detekované události `ignition` (vypíše ji `telemetry-overlay inspect`).

## 6. Rozlišení a snímková frekvence

Renderujte ve stejném rozlišení a fps jako je projekt:

```bash
telemetry-overlay render data.csv -c rocket_thrust -o overlay.mov -r 4k --fps 60
```

Rozlišení se zadává jako `1080p`, `1440p`, `4k`, `vertical1080` (na mobil) nebo
přímo `2560x1440`. Rozvržení je v procentech, takže se přizpůsobí samo.

Nesouhlasí-li fps overlaye s projektem, Resolve snímky duplikuje nebo zahazuje
a přejezd v grafu začne poskakovat. `--fps` proto nastavte podle projektu
(`Project Settings → Master Settings → Timeline frame rate`).

## 7. Export hotového videa

Bezplatná verze Resolve exportuje do H.264/H.265 v `.mp4` bez omezení, do
**4K/60 včetně**. Overlay je v té chvíli už vypálený do obrazu, takže alfa
kanál na výstupu nepotřebujete.

## Časté potíže

| Projev | Příčina a řešení |
|---|---|
| Overlay má černé pozadí | `Clip Attributes → Alpha Mode` → přepnout na `Premultiplied` |
| Text má tmavé okraje | Špatný Alpha Mode – přepnout na druhou možnost |
| Overlay není vidět | Je na nižší stopě než záběr, nebo má nulovou opacitu |
| Sekvence jako stovky klipů | Vybrat při importu jen **první** snímek |
| Graf poskakuje | fps overlaye ≠ fps projektu, doplňte `--fps` |
| ProRes se na Windows netrhne/nehraje | `--codec qtrle`, nebo PNG sekvence |
| Overlay je kratší než záběr | Záběr má jiný retime než overlay, viz bod 4 |

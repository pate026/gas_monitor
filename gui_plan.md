# gas_monitor GUI: suunnitelma

Päätökset (24.11.2026):
- Tekniikka: **PySide6 (Qt)** työpöytäsovellus
- TUI **säilyy**, GUI lisätään rinnalle (`python main.py --gui`)

## 1. Lähtötilanne (mitä koodissa jo on)

Arkkitehtuuri on jo valmiiksi GUI-ystävällinen:

| Tiedosto | UI-riippuvuus | GUI:ssa |
| --- | --- | --- |
| `ws_client.py` | ei mitään | käytetään sellaisenaan |
| `metrics.py` | ei mitään | käytetään sellaisenaan (`stats()`, `blocks()`, `recent_logs()`) |
| `decoder.py` | ei mitään | käytetään sellaisenaan (`decode_log`) |
| `config.py` | ei | lisätään `--gui`-lippu |
| `tui.py` | Textual | formatointifunktiot siirretään pois, muuten ennallaan |
| `main.py` | valitsee UI:n | valitsee TUI:n tai GUI:n |

Tärkeä huomio: `tui.py` sisältää sekä Textual-koodia että yleisiä apufunktioita (`format_gwei`, `format_seconds`, `format_rate`, `format_count`, `format_percent`, `CHAIN_NAMES`, `_chain_label`, `_trend_text` logiikka jne.). GUI tarvitsee samat, mutta ei saa tuoda Textualia mukaan. Siksi ensin pieni refaktorointi (vaihe 0).

## 2. Arkkitehtuuri

```
main.py
  ├─ --gui  → gui/app.py   (PySide6 + qasync)
  └─ (oletus) → tui.py     (Textual, ennallaan)

jaettu: config.py, ws_client.py, metrics.py, decoder.py, formatting.py (uusi)
```

Uusi rakenne (tiedostot pienellä alkukirjaimella):

```
gas_monitor/
  formatting.py        # format_gwei, format_seconds, ..., CHAIN_NAMES, chain_label
  gui/
    __init__.py
    app.py             # run_gui(config, metrics, client): QApplication + qasync-silmukka
    main_window.py     # MainWindow: asettelu, QTimer, pause/clear
    status_bar.py      # yhteystila, ketju, endpoint, block #N, "x s ago"
    gas_panel.py       # tilastokortit + base fee -kaavio (pyqtgraph)
    blocks_model.py    # QAbstractTableModel + delegate (gas used -palkki)
    events_view.py     # rajattu event-feed
    theme.py           # värit ja tyylit (vihreä/keltainen/punainen kuten TUI:ssa)
```

### Keskeiset tekniset ratkaisut

1. **asyncio + Qt samassa säikeessä.** `qasync` ajaa asyncio-silmukan Qt:n tapahtumasilmukassa. `client.run()` käynnistetään taskina. Ei säikeitä, ei lukkoja: `Metrics` päivittyy ja GUI lukee sen samassa säikeessä, aivan kuten Textualissa.
2. **Päivitysmalli kuten TUI:ssa.** `QTimer` 250 ms välein lukee `metrics.stats()`. Redraw vain kun `metrics.version` / `blocks_version` / `log_seq` muuttuu, ja raskaammat osat rajoitetaan (vakiot `TABLE_MIN_INTERVAL` jne. siirretään jaettuun paikkaan tai kopioidaan). Datan vastaanottoa ei koskaan rajoiteta.
3. **Blocks-taulukko: `QAbstractTableModel`, ei koko taulun uudelleenrakennusta.** TUI joutuu tekemään `table.clear()` joka päivityksellä, Qt:ssa model voi lähettää `dataChanged`/`layoutChanged`, eikä vierityspositio nollaudu. Siksi TUI:n "scrolled · updates resume at the top" -kikka ei ole tarpeen.
4. **Kaavio: `pyqtgraph`.** Base fee -historia viivana (`stats.base_fee_history`). Myöhemmin mahdollisesti toinen kaavio gas used -prosentille.
5. **Event feed: rajattu ringpuskuri (250 riviä)** kuten `FEED_MAX_LINES`. Uusimmat `FEED_LINES_PER_REFRESH` (10) riviä per päivitys, ja "⋯ N more events not shown" -rivi säilyy. Toteutus: `QListView`/`QTableView` + oma model, tai `QPlainTextEdit` jos riittää.
6. **Pause / clear / quit**: samat toiminnot ja pikanäppäimet (`P`, `C`, `Q`/`Ctrl+Q`) + työkalupalkin napit. Pause-tilassa ikkunan reunus/tila vaihtuu keltaiseksi.
7. **Sulkeminen:** `closeEvent` kutsuu `await client.stop()` ja lopettaa silmukan siististi.
8. **Lazy-importit `main.py`:ssä:** `--gui` ei tarvitse Textualia eikä TUI PySide6:ta. Importit tehdään vasta valinnan jälkeen.

### Ulkoasu (alustava)

```
┌ status: ● connected │ Ethereum · chain 1 │ wss://host        block #… · 0.5s ago ┐
├───────────────────────────┬───────────────────────────────────────────────────┤
│ GAS & NETWORK (kortit)    │ RECENT BLOCKS (taulukko, vieritettävä)            │
│  base fee   trend         │  block time basefee Δ gas used ▓▓▓░ txs events    │
│  min/avg/max  gas used    │                                                   │
│  block time throughput    │                                                   │
│  events    window         │                                                   │
│ ─ base fee -kaavio ─      │                                                   │
├───────────────────────────┴───────────────────────────────────────────────────┤
│ LIVE EVENTS (feed)                                                            │
└───────────────────────────────────────────────────────────────────────────────┘
 toolbar: [Pause] [Clear logs]
```

Kapealla ikkunalla vasen paneeli menee taulukon päälle (QSplitter tekee tästä helpon, ei tarvita breakpointteja).

## 3. Vaiheet

Jokainen vaihe on pieni ja tarkistettavissa erikseen. Claude Code tekee, sinä testaat ja hyväksyt ennen seuraavaa.

### Vaihe 0: valmistelu
- Siirrä formatointifunktiot ja `CHAIN_NAMES` `tui.py`:stä uuteen `formatting.py`:hyn. `tui.py` importtaa ne sieltä.
- **Tarkistus:** `python main.py` toimii täsmälleen kuten ennen.

### Vaihe 1: GUI-runko
- Lisää riippuvuudet `requirements.txt`:ään: `PySide6`, `pyqtgraph`, `qasync`.
- `config.py`: `--gui`-lippu (ja `Config.gui: bool`, ympäristömuuttuja esim. `GUI=true` valinnainen).
- `main.py`: valitsee UI:n lazy-importilla.
- `gui/app.py` + `main_window.py`: tyhjä ikkuna + status bar, joka näyttää yhteystilan, ketjun ja `block #N`.
- **Tarkistus:** `python main.py --gui` avaa ikkunan, status bar päivittyy, ikkunan sulkeminen lopettaa prosessin ilman virheitä. Katkaise netti: näkyy "reconnecting".

### Vaihe 2: gas-paneeli ja kaavio
- Tilastokortit (`base fee`, `trend`, `min/avg/max`, `gas used`, `block time`, `throughput`, `events`, `window`).
- Base fee -viivakaavio.
- **Tarkistus:** luvut täsmäävät TUI:n kanssa kun ajat molemmat samaan aikaan samalla endpointilla.

### Vaihe 3: blocks-taulukko
- `BlocksModel`, värit (Δ punainen/vihreä, gas used -palkki), `…` ja `–` -solut kuten TUI:ssa.
- **Tarkistus:** taulukko päivittyy ilman että vierityskohta hyppää; vanhat rivit voi lukea rauhassa.

### Vaihe 4: events-feed ja ohjaus
- Feed, throttlaus, "more events not shown" -rivi, pause/clear + pikanäppäimet + toolbar.
- Testaa kiireisellä ketjulla (Base: `-u wss://base-rpc.publicnode.com`) ja `-c`-suodattimella.
- **Tarkistus:** ikkuna ei jäädy Basella, CPU-käyttö kohtuullinen.

### Vaihe 5: viimeistely
- Tumma teema, ikkunan koon/sijainnin muistaminen (`QSettings`), mahdollinen ikkunan otsikko `gas monitor · <ketju>`.
- README:hen GUI-osio (asennus, `--gui`, kuvakaappaus).
- Yksikkötestit puhtaille funktioille (`formatting.py`) ja `BlocksModel`:lle.

## 4. Riskit ja avoimet kysymykset

- **PySide6 on iso asennus** (~200–300 MB) ja koneessa on HDD ja 8 GB RAM. Asennus on hidas mutta toimii. Käytä omaa venviä.
- **qasync + PySide6 -versioyhteensopivuus:** tarkistetaan vaiheessa 1, lukitaan toimivat versiot `requirements.txt`:ään.
- **Wayland (Fedora):** PySide6 toimii yleensä suoraan. Jos ikkuna ei aukea, kokeile `QT_QPA_PLATFORM=xcb`.
- **Kiireiset ketjut:** sama throttlaus-periaate kuin TUI:ssa on pakko säilyttää, muuten Qt:n piirto tukkeutuu.
- **Avoin:** halutaanko myöhemmin ketjun/endpointin vaihto GUI:sta ilman uudelleenkäynnistystä? Rajattu pois tästä versiosta (client luodaan käynnistyksessä).

## 5. CLAUDE.md-luonnos (vaihe 2 projektissa)

Tämä lisätään repon `CLAUDE.md`:hen (englanniksi, koska koodisisältö on englanniksi):

```markdown
## GUI (PySide6)

Goal: add a desktop GUI next to the existing Textual TUI. The TUI must keep working unchanged.

### Rules
- Run with `python main.py --gui`. Default (no flag) stays the TUI.
- Do NOT touch `ws_client.py`, `metrics.py`, `decoder.py` behavior. The GUI only reads from `Metrics` and uses `EthWsClient` as is.
- GUI code lives in `gui/`. Shared formatting helpers live in `formatting.py` (no Textual or Qt imports there).
- `main.py` must import the chosen UI lazily: `--gui` must not need Textual, the TUI must not need PySide6.
- One thread only: use `qasync` to run asyncio inside the Qt event loop. No threads, no locks.
- Update via a `QTimer` (250 ms) that reads `metrics.stats()`. Redraw only when `metrics.version`, `blocks_version` or `log_seq` changed; rate-limit heavy widgets. Never throttle data ingestion.
- Blocks table uses a `QAbstractTableModel` (no full rebuild per update). Event feed keeps at most 250 lines.
- Use lowercase file and folder names. Code, comments and docstrings in English, comments in lowercase style like the existing code.

### Workflow
- Work phase by phase (see gui_plan.md). After each phase: stop, summarize what changed, and tell how to test it manually. Do not start the next phase without confirmation.
- Keep dependencies minimal: PySide6, pyqtgraph, qasync.
```

## 6. Seuraavat askeleet

1. Käydään tämä suunnitelma läpi, muutetaan mitä haluat.
2. Päivitetään `CLAUDE.md` luonnoksen pohjalta.
3. Claude Code toteuttaa vaiheen kerrallaan, sinä tarkistat.

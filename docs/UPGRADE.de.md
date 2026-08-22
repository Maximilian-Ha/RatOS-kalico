# RatOS 2.1 auf Kalico umstellen

> **English version: [UPGRADE.md](UPGRADE.md)**

Diese Anleitung ist für Leute geschrieben, die Befehle in ein Terminal kopieren
können, aber nicht verstehen müssen, was jeder einzelne tut. Zu jedem Schritt
steht, was du sehen sollst — und was zu tun ist, wenn etwas anderes kommt.

---

## ⚠️ Diesen Teil bitte lesen. Nicht überspringen.

**Das ist eine frühe Version. Sie lief bisher auf genau einem Drucker.**

Jeder Schritt hier wurde auf einer echten Maschine durchgeführt — einem
V-Core 4.1 IDEX mit Beacon — und jeder Fehler, den diese Maschine hatte, steht in
dieser Anleitung. Aber ein Drucker ist ein Drucker. Dein Board, dein Sensor,
deine Plugins sind Kombinationen, die noch niemand ausprobiert hat.

**Bleib während der ganzen ersten Sitzung in Reichweite des Not-Aus.** Nicht „im
Raum" — in Armlänge, die Hand bereit. Konkret:

- Das erste `G28 Z` nach der Umstellung ist der gefährliche Moment. Kalico fährt
  die Z-Achse beim Homing anders als Klipper, und wenn deine Sensor-Konfiguration
  nicht stimmt, kann die Düse in das Druckbett gefahren werden.
- Starte keinen Druck und lass den Drucker nicht allein, bis du alle drei Achsen
  gehomt und einen vollständigen Messvorgang beobachtet hast.
- Mach das nie an einem Drucker, den du morgen brauchst. Rechne damit, dass du
  einen Abend damit verbringst, alles zurückzubauen.

**Du kommst jederzeit wieder zurück.** [ROLLBACK.de.md](ROLLBACK.de.md) bringt
den Drucker auf das normale, Klipper-basierte RatOS. Lies das *vorher*, damit du
weißt, wie der Ausgang aussieht.

---

## Was sich tatsächlich ändert

RatOS läuft normalerweise auf [Klipper](https://www.klipper3d.org/). Dieser Fork
lässt es stattdessen auf [Kalico](https://github.com/KalicoCrew/kalico) laufen —
einem Klipper-Ableger mit zusätzlichen Funktionen. Alles andere an RatOS bleibt:
derselbe Konfigurator, dieselben Makros, dieselbe Weboberfläche.

Zwei Dinge sind vorab wichtig:

- **Deine `printer.cfg` wird nicht angefasst.** Die Umstellung ändert `~/klipper`
  und `~/ratos-configurator`, nicht deine eigene Konfiguration.
- **Deine Board-Firmware funktioniert zunächst weiter.** Kalico wird anmerken,
  dass es frisch gebaute Firmware bevorzugt. Das ist Schritt 8 und hat keine Eile.

---

## Bevor du anfängst

Du brauchst:

- Einen Drucker mit **RatOS 2.1**, der aktuell funktioniert.
- **SSH-Zugang** dorthin. Unter Windows PowerShell oder PuTTY, unter macOS und
  Linux das Terminal. `ssh pi@RatOS.local` — falls der Name nicht auflöst, nimm
  die IP-Adresse des Druckers.
- **45 Minuten**, Zugang zur Maschine, und es darf nichts drucken.

Alle Befehle unten werden per SSH **auf dem Drucker** eingegeben, außer wenn
ausdrücklich etwas anderes dabeisteht.

---

## Schritt 0 — Sicherung anlegen

```bash
tar czf ~/ratos-backup.tgz ~/printer_data/config ~/klippy-env
git -C ~/klipper rev-parse HEAD
git -C ~/klipper branch --show-current
```

Die beiden letzten Befehle geben so etwas aus wie `2817b348…` und
`ratos/v2.1.x`. **Schreib beides auf.** Das ist dein Rückweg, und er ist damit
deutlich einfacher als ohne.

Dann die Sicherung vom Drucker herunterholen — **auf deinem eigenen Rechner**,
nicht auf dem Drucker:

```bash
scp pi@RatOS.local:~/ratos-backup.tgz .
```

---

## Schritt 1 — Prüfen, ob der Drucker bereit ist

Lade [`scripts/preflight-standalone.sh`](../scripts/preflight-standalone.sh) aus
diesem Repository herunter und führe dann **auf deinem eigenen Rechner** aus:

```bash
scp preflight-standalone.sh pi@RatOS.local:~/
ssh pi@RatOS.local 'bash ~/preflight-standalone.sh'
```

Das Skript ändert nichts. Es liest nur.

**Am Ende soll `==> Ready.` oder `==> Ready, with notes.` stehen.**

Bei `NOT READY` zuerst die `[FAIL]`-Zeilen abarbeiten — jede sagt, was zu tun
ist. Die zwei häufigsten:

| Meldung | Was zu tun ist |
|---|---|
| `beacon does not expose the legacy probe protocol` | `git -C ~/beacon pull`, dann `sudo systemctl restart klipper`, danach Preflight erneut |
| `working tree has modifications` | In `~/klipper` wurden Dateien verändert. `git -C ~/klipper status --short` ansehen und nachfragen, bevor du weitermachst — die Umstellung bricht sonst ab |

---

## Schritt 2 — RatOS auf den Fork umstellen

Das ist der Schritt, der alles Weitere auslöst. Den ganzen Block auf einmal
kopieren:

```bash
bash -c '
cd ~/ratos-configurator &&
git remote set-url origin https://github.com/Maximilian-Ha/RatOS-configurator.git &&
git config --unset-all remote.origin.fetch &&
git config --add remote.origin.fetch "+refs/heads/*:refs/remotes/origin/*" &&
git fetch origin &&
git checkout -B v2.1.x-kalico-deployment origin/v2.1.x-kalico-deployment
' 2>&1 | tee ~/kalico-switch.log
```

> **Warum das `bash -c`:** Wenn etwas schiefgeht, endet nur diese innere Shell.
> Ohne das würde ein Fehler dein SSH-Fenster schließen — samt der Fehlermeldung,
> die du lesen wolltest. Die Ausgabe landet zusätzlich in
> `~/kalico-switch.log`, es geht also nichts verloren.

Jetzt **prüfen, ob es gegriffen hat**. Nicht überspringen:

```bash
git -C ~/ratos-configurator branch --show-current
grep -n 'RATOS_FORK_URL=' ~/printer_data/config/RatOS/scripts/klipper-fork-migration.sh
```

Da muss stehen:

```
v2.1.x-kalico-deployment
178:readonly RATOS_FORK_URL="https://github.com/Maximilian-Ha/kalico.git"
```

**Steht in der zweiten Zeile weiterhin `Rat-OS/klipper`, hier aufhören.** Die
Umstellung hat nicht gegriffen, und der nächste Schritt würde einfach das normale
Klipper neu installieren und dabei Erfolg melden. Den Grund findest du in
`~/kalico-switch.log`.

---

## Schritt 3 — Das RatOS-Update laufen lassen

```bash
sudo systemctl restart ratos-configurator moonraker
sudo ~/printer_data/config/RatOS/scripts/ratos-update.sh
```

Das dauert einige Minuten. Achte am Ende auf `RatOS update completed
successfully`.

Kommt stattdessen `UNSUPPORTED_REPOSITORY_SOURCE` oder
`GIT_CHECKOUT_REMOTE_FAILED`, aufhören und im Abschnitt
[Fehlersuche](#fehlersuche) nachsehen — beides hat eine konkrete Ursache.

---

## Schritt 4 — Klipper richtig neu starten

```bash
sudo systemctl restart klipper
```

**Es muss dieser Dienst-Neustart sein.** `RESTART` und `FIRMWARE_RESTART` in der
Weboberfläche laden nur deine Konfiguration im *laufenden* Programm neu — sie
starten Kalico nicht. Du bekommst dann verwirrende Fehler, die aussehen, als sei
Kalico kaputt, obwohl es nie gestartet wurde.

Der erste Start dauert länger als gewohnt, weil Kalico eine Hilfsbibliothek neu
übersetzt. Gib ihm eine Minute.

---

## Schritt 5 — Bestätigen, dass wirklich Kalico läuft

```bash
grep -n 'App Name:' ~/printer_data/logs/klippy.log | tail -1
```

Nimm die ausgegebene Zeilennummer — sagen wir `12345` — und sieh dir den Block
dort an:

```bash
sed -n '12345,12352p' ~/printer_data/logs/klippy.log
```

Erwartet:

```
App Name: Kalico
Branch: ratos-kalico/v2.1.x
Tracked URL: https://github.com/Maximilian-Ha/kalico.git
```

**Gibt `grep` gar nichts aus, läuft Kalico nicht.** Klipper schreibt diese Zeile
nie. Zurück zu Schritt 4; hilft das nicht, hat Schritt 2 nicht gegriffen.

Danach prüfen, ob der Start sauber war:

```bash
grep -nE 'Unknown config object|is not valid|Traceback' ~/printer_data/logs/klippy.log | tail -5
```

Nichts ist gut. Etwas bedeutet, dass Kalico einen Teil deiner Konfiguration
ablehnt — die bekannten Fälle stehen unter [Fehlersuche](#fehlersuche).

---

## Schritt 6 — Eine Konfigurationszeile, die du eventuell brauchst

**Wenn dein Board TMC2240-Treiber verwendet** — die meisten modernen
Toolhead-Boards tun das, unter anderem LDO Orbitool und BTT SB2240 — verlangt
Kalico eine Angabe, die Klipper stillschweigend selbst gesetzt hat. Du merkst es
daran, dass Klippy nicht startet:

```
Option 'rref' in section 'tmc2240 extruder' must be specified
```

Öffne `printer.cfg` in Mainsail oder Fluidd und ergänze **ganz am Ende**:

```ini
[tmc2240 extruder]
rref: 12000

# nur wenn du einen zweiten Toolhead hast
[tmc2240 extruder1]
rref: 12000
```

`12000` ist kein Rateversuch: es ist der Wert, den Klipper als Voreinstellung
benutzt hat. Am Verhalten deiner Motoren ändert sich dadurch nichts. Speichern,
dann `sudo systemctl restart klipper`.

> Frisch erzeugte Konfigurationen enthalten das bereits. Von Hand nötig ist es
> nur, weil deine bestehende Konfiguration vor der Umstellung geschrieben wurde.

**Beacon-Nutzer:** die entsprechende Z-Homing-Einstellung liefert der Fork
inzwischen mit, du musst nichts ergänzen. Scheitert Schritt 7 mit `Toolhead
stopped below model range`, siehe [Fehlersuche](#fehlersuche).

---

## Schritt 7 — Die erste Bewegung. Hand am Not-Aus.

Hier kostet ein Fehler Hardware. Genau in dieser Reihenfolge, und nicht
improvisieren.

```gcode
M84
G28 X
G28 Y
G28 Z
```

- `M84` darf überhaupt keinen Fehler erzeugen.
- `G28 X` und `G28 Y` sollen genau so aussehen wie immer.
- **`G28 Z` ist das, worauf du achtest.** Erwartet: zügig hinunter bis wenige
  Millimeter über das Bett, kurz anheben, langsamer ein zweites Mal auf denselben
  Punkt, nochmals kurz anheben, dann ein paar Sekunden Pause, während der Sensor
  misst.

**Not-Aus drücken, wenn** die Düse über den Punkt hinaus weiterfährt, an dem sie
zuerst stehengeblieben ist, oder wenn sie das Bett berührt. Beides gehört nicht
zu einem normalen Homing.

X und Y zuerst zu homen ist nicht optional — RatOS löst absichtlich einen Not-Aus
aus, wenn du Z ohne sie homst.

Wenn das läuft, ein `Z_TILT_ADJUST` (oder `QUAD_GANTRY_LEVEL`) und ein Bed Mesh,
weiterhin unter Beobachtung. Erst danach ans Drucken denken.

---

## Schritt 8 — Board-Firmware neu bauen

Kalico schreibt für jedes Board eine Zeile wie diese ins Log:

```
MCU 'mcu' currently has firmware compiled for Klipper (version v0.12.0-…).
  It is recommended to re-flash for best compatiblity with Kalico
```

Dein Drucker funktioniert auch ohne. Mach es, wenn du Zeit hast, **ein Board nach
dem anderen**, beginnend mit dem Mainboard:

```bash
ls -l /dev/RatOS/
sudo ~/printer_data/config/RatOS/scripts/flash-path.sh /dev/RatOS/<board-name> 2>&1 | tee ~/flash.log
```

Die Board-Namen liefert `ls -l /dev/RatOS/`. **Dass Klipper dabei stoppt und neu
startet, ist normal** — das Skript macht das absichtlich, um den USB-Port
freizugeben.

Am Ende soll `Flashing successful.` stehen. Scheitert ein Toolhead-Board
mittendrin, musst du unter Umständen das Gehäuse öffnen und den Bootloader-Taster
drücken. Genau deshalb kommt das Mainboard zuerst: es ist am einfachsten zu
retten.

Das geht auch über die Weboberfläche des RatOS-Konfigurators, sobald der Fork
installiert ist.

---

## Fehlersuche

Fehler, die auf einer echten Maschine tatsächlich aufgetreten sind, mit der
tatsächlichen Ursache.

### `UNSUPPORTED_REPOSITORY_SOURCE` beim Update

Der Konfigurator ist noch der normale. Zurück zu Schritt 2 und die
`RATOS_FORK_URL`-Zeile prüfen.

### `GIT_CHECKOUT_REMOTE_FAILED`, und es passiert bei jedem Update

In `~/klipper/klippy/extras/` liegt ein Symlink über einer Datei, die Kalico
selbst mitbringt, und git weigert sich, ihn zu überschreiben. Aktuelle Versionen
des Forks entfernen solche Links automatisch. Bei einer älteren:

```bash
rm ~/klipper/klippy/extras/gcode_shell_command.py
```

### `Unable to load module 'error_mcu'`

Das alte Klipper-Programm läuft noch und liest bereits die neuen Kalico-Dateien.
Das ist Schritt 4: `sudo systemctl restart klipper`.

### `Option 'rref' in section 'tmc2240 …' must be specified`

Schritt 6.

### `Toolhead stopped below model range` beim Z-Homing

Betrifft Beacon-Nutzer. Die Meldung führt in die Irre — der Toolhead ist zu weit
*über* dem Bett, nicht darunter. Ergänze im Abschnitt `[stepper_z]` deiner
`printer.cfg`:

```ini
homing_retract_dist: 1
```

Die technische Erklärung steht in [RISKS.md](RISKS.md) §3.

### `Section 'belay my_belay' is not a valid config section`

Nur relevant, wenn du das Drittanbieter-Plugin [Belay] für Filament-Buffer
benutzt — es gehört **nicht** zu RatOS, und die meisten Drucker haben es nicht.
Kalico bringt Belay selbst mit; nach der Umstellung kannst du das separate Plugin
deinstallieren, die Konfiguration funktioniert unverändert weiter.

### Der Konfigurator kann die Board-Firmware-Versionen nicht lesen

In aktuellen Versionen des Forks behoben. Beachte: diese Abfrage scheitert
*konstruktionsbedingt*, solange Klipper läuft, weil Klipper den USB-Port
exklusiv belegt — stoppe Klipper, wenn du sie von Hand ausführen willst.

### `klipper_tmc_autotune` nach der Umstellung

Nur relevant, wenn du es selbst installiert hast — es gehört **nicht** zu RatOS.
Es funktioniert unter Kalico, wird aber nicht automatisch wiederhergestellt,
falls `~/klipper` jemals gelöscht und neu geholt wird. Dann
`~/klipper_tmc_autotune/install.sh` erneut ausführen.

### Etwas anderes

Diese drei Ausgaben sammeln und nachfragen:

```bash
tail -60 ~/printer_data/logs/klippy.log
git -C ~/klipper log --oneline -1
git -C ~/ratos-configurator branch --show-current
```

---

## Zurück

[ROLLBACK.de.md](ROLLBACK.de.md). Das ist ein normaler, vorgesehener Weg, kein
Notfallverfahren.

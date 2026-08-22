# Zurück auf das normale RatOS

> **English version: [ROLLBACK.md](ROLLBACK.md)**

Damit kommt ein Drucker von Kalico zurück auf das Klipper-basierte RatOS, mit dem
er ausgeliefert wurde.

Das ist ein vorgesehener Weg, kein Notfallverfahren, und er ist bewusst so
geschrieben, dass du ihm auch auf einem Drucker folgen kannst, der gerade *nicht*
läuft. Nichts hier setzt voraus, dass Klippy startet.

---

## ⚠️ Vorher

- **Bleib auch danach in Reichweite des Not-Aus.** Der Rückweg ist ein
  Firmware-Wechsel wie jeder andere; das erste `G28 Z` danach verdient dieselbe
  Aufmerksamkeit wie in der anderen Richtung.
- **Benutze auf einer umgestellten Maschine nicht die „Recover"- oder „Hard
  Recover"-Schaltflächen in Mainsail.** Hard Recover löscht `~/klipper`
  vollständig und installiert das normale Klipper von der alten Adresse — das
  klingt nach genau dem, was du willst, zerstört dabei aber unbemerkt
  Plugin-Verknüpfungen und macht die Konfigurator-Seite gar nicht rückgängig.
  Nimm stattdessen diese Anleitung.
- **Du musst deine Boards neu flashen**, falls du deren Firmware unter Kalico neu
  gebaut hast. Siehe Schritt 5.

---

## Was du brauchst

Die beiden Werte aus Schritt 0 der Umstellung:

```
Commit   z. B. 2817b348e23c779b68ae5f27f2b9b9af8cfcf0da
Branch   z. B. ratos/v2.1.x
```

Verloren? Sie lassen sich rekonstruieren:

```bash
git -C ~/klipper log --oneline --all | head -30
```

Such den letzten Commit, der *nicht* zu den Kalico-Commits gehört. Kommt dir
nichts bekannt vor, nimm `ratos/v2.1.x` als Branch und lass das Festlegen des
genauen Commits weg — RatOS holt sich in Schritt 3 den passenden selbst.

---

## Schritt 1 — Den Konfigurator zurück auf RatOS zeigen lassen

```bash
bash -c '
cd ~/ratos-configurator &&
git remote set-url origin https://github.com/Rat-OS/RatOS-configurator.git &&
git fetch origin &&
git checkout -B v2.1.x-deployment-2 origin/v2.1.x-deployment-2
' 2>&1 | tee ~/rollback.log
```

Prüfen, ob es gegriffen hat:

```bash
git -C ~/ratos-configurator branch --show-current
grep -n 'RATOS_FORK_URL=' ~/printer_data/config/RatOS/scripts/klipper-fork-migration.sh
```

Erwartet: `v2.1.x-deployment-2` und eine `RATOS_FORK_URL`, die auf
`Rat-OS/klipper` zeigt.

> Scheitert `git fetch`, weil der Branch nicht gefunden wird, ist dein Checkout
> auf einen Branch beschränkt. Einmalig erweitern:
> `git -C ~/ratos-configurator config --unset-all remote.origin.fetch && git -C ~/ratos-configurator config --add remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'`
> danach den Block oben wiederholen.

---

## Schritt 2 — Klipper zurück zeigen lassen

```bash
bash -c '
cd ~/klipper &&
git remote set-url origin https://github.com/Klipper3d/klipper.git &&
git fetch origin &&
git checkout -B ratos/v2.1.x origin/master
' 2>&1 | tee -a ~/rollback.log
```

Wenn du den genauen Commit aus deinen Notizen hast, setz ihn fest:

```bash
git -C ~/klipper reset --hard 2817b348e23c779b68ae5f27f2b9b9af8cfcf0da
```

Bestätigen, dass Kalico weg ist — diese Datei gibt es nur bei Kalico:

```bash
ls ~/klipper/klippy/__init__.py
```

`No such file or directory` ist hier die richtige Antwort.

---

## Schritt 3 — RatOS sich selbst wieder zusammensetzen lassen

```bash
sudo systemctl restart ratos-configurator moonraker
sudo ~/printer_data/config/RatOS/scripts/ratos-update.sh
sudo systemctl restart klipper
```

Das Update legt die RatOS-eigenen Plugin-Verknüpfungen neu an, auch die von
Beacon.

---

## Schritt 4 — Die beiden Konfigurationszeilen zurücknehmen

Falls du eine davon bei der Umstellung ergänzt hast, nimm sie wieder heraus — auf
Klipper sind sie harmlos, aber stehenzulassen ist unsauber, und eine davon ändert
das Homing:

- `homing_retract_dist: 1` in `[stepper_z]` — **entfernen**, oder auf den alten
  Wert zurücksetzen. Sonst bleibt der Z-Rückzug beim Homing kürzer als im
  Original.
- `rref: 12000` in `[tmc2240 …]` — in beide Richtungen harmlos, weil es Klippers
  Voreinstellung entspricht. Entferne es, wenn du eine saubere Datei willst.

Danach `sudo systemctl restart klipper`.

---

## Schritt 5 — Board-Firmware

**Wenn du die Board-Firmware unter Kalico neu gebaut hast, musst du sie jetzt
erneut bauen.** Klipper- und Kalico-Firmware sind nicht austauschbar; eine
Kalico-Firmware auf dem Board bei laufendem Klipper erzeugt Protokollfehler, die
schwer zu lesen sind.

Ein Board nach dem anderen, Mainboard zuerst:

```bash
ls -l /dev/RatOS/
sudo ~/printer_data/config/RatOS/scripts/flash-path.sh /dev/RatOS/<board-name> 2>&1 | tee ~/reflash.log
```

Bist du bei der Umstellung nie bis Schritt 8 gekommen, tragen deine Boards noch
ihre ursprüngliche Firmware und es ist nichts zu tun.

---

## Schritt 6 — Prüfen, dann bewegen

```bash
grep -n 'App Name:' ~/printer_data/logs/klippy.log | tail -1
```

Auf normalem Klipper gibt das **nichts** aus — Klipper schreibt diese Zeile
nicht. Genau das ist das gewünschte Ergebnis.

Dann, Hand in der Nähe des Not-Aus:

```gcode
M84
G28 X
G28 Y
G28 Z
```

---

## Stattdessen aus der Sicherung wiederherstellen

Wenn der Drucker in einem Zustand ist, über den du nicht mehr nachdenken willst,
setzt die Sicherung aus Schritt 0 der Umstellung die Konfiguration komplett
zurück:

```bash
sudo systemctl stop klipper
tar xzf ~/ratos-backup.tgz -C /
sudo systemctl start klipper
```

Das stellt `~/printer_data/config` und die Python-Umgebung wieder her. Es stellt
**nicht** `~/klipper` oder `~/ratos-configurator` wieder her — das tun die
Schritte 1 und 2 oben, und die solltest du zuerst ausführen.

---

## Wenn gar nichts hilft

Die SD-Karte mit einem frischen RatOS-Image neu zu beschreiben und
`~/printer_data/config` aus deiner Sicherung zurückzuspielen, ist eine legitime
Antwort — und bei einem Drucker, den du brauchst, oft die schnellste. Nichts an
diesem Fork fasst den Bootloader oder die Board-Firmware an, solange du Schritt 8
der Umstellung nicht ausgeführt hast.

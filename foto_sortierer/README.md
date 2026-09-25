# Foto- & Video-Sortierer

Sortiert Fotos und Videos vom iPhone nach **Aufnahmedatum** in vorbereitete
Ordner nach **Jahr/Monat**.

## Sicherheit

- Es werden **niemals Dateien gelöscht** (im Code gibt es keinen einzigen Löschbefehl).
- Vorhandene Dateien werden **nie überschrieben**. Gibt es im Ziel schon eine
  Datei mit gleichem Namen, aber anderem Inhalt, heißt die neue z. B. `IMG_1234 (1).HEIC`.
- Liegt die identische Datei schon im Ziel, wird sie übersprungen. Das Tool
  kann also beliebig oft über denselben Ordner laufen, ohne Duplikate anzulegen.
- Standard ist **Kopieren**: Die Originale bleiben im Quellordner.
- **Verschieben** ist optional und passiert nur per Umbenennen auf demselben
  Laufwerk. Auf einem anderen Laufwerk wird stattdessen kopiert.
- Kopien werden erst unter einem temporären Namen geschrieben und nach der
  Größenprüfung umbenannt. So entsteht nie eine halbe Datei unter dem echten Namen.
- Jeder Lauf schreibt ein Protokoll (CSV, lässt sich mit Excel öffnen) nach
  `Zielordner/_Sortierprotokolle/`.

## Installation

Nötig ist nur **Python 3.8 oder neuer**, Zusatzpakete braucht es nicht.

- **Windows:** Python von <https://www.python.org> installieren und dabei
  „Add python.exe to PATH“ anhaken.
- **macOS:** Python von python.org installieren (bringt die Oberfläche mit).

## Benutzung

1. Fotos wie gewohnt vom iPhone auf den Computer holen. Dabei landen sie im
   Bilder-Ordner, etwa `Bilder\202409__`, `Bilder\100APPLE` oder ein Datumsordner
   aus der Windows-Fotos-App.
2. **Windows:** Doppelklick auf `Foto-Sortierer starten.bat`
   **macOS:** Doppelklick auf `Foto-Sortierer starten.command`
   (oder `python3 foto_sortierer.py`)
3. Der **Quellordner** ist schon auf den Bilder-Ordner eingestellt. Alle
   Unterordner werden mit durchsucht.
4. Den **Zielordner** wählen, also den Ordner, in dem die Jahresordner liegen.
5. Zuerst **„Vorschau“** klicken: Das Tool zeigt, was wohin käme, ändert aber nichts.
6. Danach **„Sortieren starten“** klicken.

Die Ordner werden beim nächsten Start wieder vorgeschlagen.

### Kommandozeile

```
python foto_sortierer.py --quelle "C:\Users\Name\Pictures" --ziel "D:\Fotos" --vorschau
python foto_sortierer.py --quelle "C:\Users\Name\Pictures" --ziel "D:\Fotos"
```

Optionen: `--verschieben` und `--ohne-datum-ordner` (siehe unten).

## Welche Zielordner werden erkannt?

Das Tool nutzt die vorhandenen Ordner und erkennt den Monat an Zahl oder Name.
Alle folgenden Varianten funktionieren:

```
D:\Fotos\2024\01              D:\Fotos\2024\01 Januar
D:\Fotos\2024\1               D:\Fotos\2024\01 - Januar
D:\Fotos\2024\Januar          D:\Fotos\2024\2024-01
D:\Fotos\2024-01              (flach, ohne Jahresordner)
```

Fehlt ein Monatsordner, wird er **im selben Stil** wie die vorhandenen angelegt:
Neben `01 Januar` entsteht also `03 März`. Welche Ordner neu angelegt werden,
steht vorher in der Vorschau.

## Woher kommt das Datum?

Die Quellen werden in dieser Reihenfolge geprüft:

1. **Aufnahmedatum aus den Metadaten:** EXIF bei JPG/HEIC/PNG/DNG, bei Videos
   (MOV/MP4) das Apple-Aufnahmedatum in Ortszeit.
2. **Datum im Dateinamen**, z. B. `IMG-20240512-WA0001.jpg` (WhatsApp) oder
   `2024-05-12 14.03.22.jpg`.
3. **Änderungsdatum der Datei.** Diese Dateien werden in der Zusammenfassung
   gezählt. Wer das nicht möchte, setzt das Häkchen „Dateien ohne Aufnahmedatum
   in ‚_Ohne Datum‘ ablegen“ (`--ohne-datum-ordner`).

`.AAE`-Dateien (Bearbeitungsinfos vom iPhone) landen im selben Ordner wie das
zugehörige Foto.

Unterstützt werden: JPG, JPEG, HEIC, HEIF, PNG, GIF, TIF, DNG, WEBP, BMP, MOV,
MP4, M4V, 3GP, AVI, MKV, MTS und AAE.

**Tipp fürs iPhone:** Unter *Einstellungen → Fotos → Auf Mac oder PC übertragen*
steht „Originale beibehalten“. Mit dieser Einstellung bleiben HEIC-Dateien
mitsamt Aufnahmedatum unverändert.

## Tests

```
python -m unittest test_foto_sortierer.py
```

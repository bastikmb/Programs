# Lidl-Bon-Postbote: Einrichtung

Das Programm holt einmal am Tag neue Kassenbons aus deinem Lidl-Plus-Konto. Jeden Bon schickt es als Mail an deine Gmail-Adresse:

- **Betreff:** „Lidl Plus Bon vom 01.10.2026 – 23,45 €“
- **Inhalt:** die Positionsliste als Text
- **Anhang:** eine JSON-Datei mit allen Daten

Deine Einkaufs-App liest diese Mails dann genauso aus wie die REWE-eBons.

Das Programm nutzt das inoffizielle Open-Source-Paket [lidl-plus](https://github.com/Andre0512/lidl-plus). Es kann ausfallen, wenn Lidl seine App ändert.

Du brauchst etwa 20 Minuten.

---

## 1. Ordner anlegen

Lege einen festen Ordner an, zum Beispiel:
- Windows: `C:\Users\<du>\lidl-postbote`
- Mac: `~/lidl-postbote`

Kopiere aus diesem Repo-Ordner `lidl-postbote/` diese Dateien hinein:
- `lidl_postbote.py`
- `test_postbote.py`
- `config.example.ini`
- `ANLEITUNG.md`

## 2. Python und das Lidl-Paket installieren

1. Installiere Python 3.9 oder neuer von [python.org](https://www.python.org/downloads/). Setze unter Windows bei der Installation den Haken bei **„Add python.exe to PATH“**.
2. Öffne ein Terminal: unter Windows die *Eingabeaufforderung*, auf dem Mac das *Terminal*.
3. Installiere das Paket:

```
pip install "lidl-plus[auth]"
```

Auf dem Mac heißt der Befehl eventuell `pip3`. Für die Anmeldung muss Chrome, Edge oder Firefox installiert sein.

## 3. Lidl-Token holen (einmalig)

```
lidl-plus auth
```

Gib nacheinander ein:
- Sprache: `de`
- Land: `DE`
- deine Lidl-Plus-Telefonnummer im Format `+49…`
- dein Passwort
- den Code aus der SMS

Am Ende zeigt das Tool einen langen **Refresh-Token**. Kopiere ihn.

## 4. Gmail-App-Passwort anlegen (einmalig)

Gmail lässt Programme nicht mit deinem normalen Passwort senden. Du brauchst dafür ein **App-Passwort**:

1. Schalte die **Bestätigung in zwei Schritten** ein, falls sie noch aus ist: [myaccount.google.com/security](https://myaccount.google.com/security).
2. Öffne [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) und lege ein App-Passwort an, zum Beispiel mit dem Namen „Lidl-Postbote“.
3. Kopiere das 16-stellige Passwort.

## 5. config.ini ausfüllen

1. Kopiere `config.example.ini` und nenne die Kopie `config.ini`.
2. Trage diese Werte ein:
   - `refresh_token` → den Token aus Schritt 3
   - `adresse` → deine Gmail-Adresse
   - `app_passwort` → das App-Passwort aus Schritt 4

> `config.ini` enthält Zugangsdaten. Gib sie nie weiter und lade sie nirgends hoch. Im Repo ist sie über `.gitignore` ausgeschlossen.

## 6. Testen in Stufen

Teste Schritt für Schritt. So siehst du bei einem Fehler sofort, ob es an Gmail oder an Lidl liegt. Wechsle dazu im Terminal in den Ordner aus Schritt 1, zum Beispiel mit `cd C:\Users\<du>\lidl-postbote`.

**Stufe 1: Selbsttest ohne Zugangsdaten**

```
python test_postbote.py
```

Lidl und Gmail werden dabei nachgebaut. Der Test prüft, ob Python und das Programm auf deinem Rechner laufen. Er kann direkt nach Schritt 2 laufen, ohne Token und ohne App-Passwort. Am Ende muss „Alle Prüfungen bestanden“ stehen.

**Stufe 2: Nur Gmail**

```
python lidl_postbote.py --test-mail
```

Dieser Lauf schickt eine Testmail an dich. Lidl wird dabei nicht angesprochen. In `config.ini` reicht als `refresh_token` vorerst ein Platzhalter wie `x`.

**Stufe 3: Nur Lidl**

```
python lidl_postbote.py --trocken
```

Dafür muss der echte Token eingetragen sein. Das Programm zeigt deine Bons an und schickt nichts.

**Stufe 4: Ein echter Bon**

```
python lidl_postbote.py --max 1
```

Dieser Lauf schickt genau einen Bon, den ältesten. Prüfe in Gmail Betreff, Positionen und den JSON-Anhang.

**Stufe 5: Erster vollständiger Lauf**

```
python lidl_postbote.py --seit 2025-04-30
```

Dieser Lauf sendet höchstens 40 Mails, der Rest kommt an den folgenden Tagen. Mit `--seit` lässt du ältere Bons weg. Ohne `--seit` holt das Programm alle Bons, die Lidl Plus gespeichert hat.

Die Datei `state.json` merkt sich, welche Bons schon verschickt sind. Jeder Bon kommt deshalb nur einmal. Lösche sie nur, wenn du wirklich alle Bons noch einmal bekommen willst.

Nach Schritt 7 kannst du den täglichen Start einmal von Hand auslösen und danach in `postbote.log` nachsehen:
- Windows: Aufgabenplanung, Rechtsklick auf die Aufgabe, *Ausführen*
- Mac: `launchctl start de.lidl-postbote`

## 7. Täglich automatisch starten

### Windows (Aufgabenplanung)

1. Öffne die *Aufgabenplanung* und klicke auf *Einfache Aufgabe erstellen*.
2. Name: `Lidl-Bon-Postbote`.
3. Trigger: *Täglich*, zum Beispiel 20:00 Uhr.
4. Aktion: *Programm starten*.
   - Programm: der Pfad zu `pythonw.exe`. Mit `pythonw` öffnet sich kein Fenster. Den Pfad zeigt `where pythonw`.
   - Argumente: `lidl_postbote.py`
   - Starten in: dein Ordner, z. B. `C:\Users\<du>\lidl-postbote`
5. Öffne danach die Eigenschaften der Aufgabe. Setze im Reiter *Einstellungen* den Haken bei **„Aufgabe so schnell wie möglich nach einem verpassten Start ausführen“**. Dann läuft sie auch nach, wenn der PC um 20 Uhr aus war.

### Mac (launchd)

1. Lege die Datei `~/Library/LaunchAgents/de.lidl-postbote.plist` mit folgendem Inhalt an. Ersetze `DEINNAME` durch deinen Benutzernamen. Den Pfad zu Python zeigt `which python3`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>de.lidl-postbote</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/python3</string>
    <string>/Users/DEINNAME/lidl-postbote/lidl_postbote.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/DEINNAME/lidl-postbote</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>20</integer><key>Minute</key><integer>0</integer></dict>
</dict>
</plist>
```

2. Aktiviere die Aufgabe:

```
launchctl load ~/Library/LaunchAgents/de.lidl-postbote.plist
```

War der Mac um 20 Uhr im Ruhezustand, holt launchd den Lauf nach dem Aufwachen nach.

## 8. Optional: Gmail aufräumen

So landen die Bons nicht im Posteingang:

1. Lege in Gmail einen Filter mit **Betreff enthält: „Lidl Plus Bon“** an.
2. Wähle als Aktionen *Label „Kassenbons“ anwenden* und *Posteingang überspringen*.

Die App findet die Mails trotzdem.

## Wenn etwas nicht klappt

Alle Meldungen stehen in `postbote.log` im Programmordner.

| Meldung | Lösung |
| --- | --- |
| „Abruf bei Lidl fehlgeschlagen“ | Führe `lidl-plus auth` erneut aus und trage den neuen Token in `config.ini` ein. |
| „Gmail-Anmeldung fehlgeschlagen“ | Prüfe das App-Passwort, nicht dein Google-Passwort. Lege notfalls ein neues an. |
| „Paket fehlt“ | Führe `pip install lidl-plus` aus. |

**Hinweis zum Token:** Lidl vergibt bei jeder Anmeldung einen neuen Token. Das Programm trägt ihn selbst in `config.ini` ein. Verwende deshalb die Datei nicht parallel auf zwei Rechnern.

**Zugang widerrufen:**
- Lösche das App-Passwort in deinem Google-Konto.
- Ändere dein Lidl-Plus-Passwort und lösche `config.ini`.

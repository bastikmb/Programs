#!/usr/bin/env python3
"""Lidl-Bon-Postbote

Holt neue Kassenbons aus Lidl Plus (über das inoffizielle Open-Source-Paket
"lidl-plus") und schickt jeden Bon als E-Mail mit JSON-Anhang an die eigene
Gmail-Adresse. Gedacht für den täglichen Start über Aufgabenplanung/launchd/cron.

Aufruf:
    python lidl_postbote.py              # neue Bons senden
    python lidl_postbote.py --trocken    # nur anzeigen, nichts senden
    python lidl_postbote.py --test-mail  # eine Testmail schicken
"""

import argparse
import configparser
import json
import logging
import os
import re
import smtplib
import sys
import tempfile
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

HIER = Path(__file__).resolve().parent
CONFIG_DATEI = HIER / "config.ini"
STATE_DATEI = HIER / "state.json"
LOG_DATEI = HIER / "postbote.log"
BETREFF_KENNUNG = "Lidl Plus Bon"

log = logging.getLogger("postbote")


# ---------------------------------------------------------------- Dateien

def atomar_schreiben(pfad: Path, inhalt: str) -> None:
    """Schreibt erst in eine Temp-Datei und ersetzt dann, damit nie eine halbe Datei entsteht."""
    fd, tmp = tempfile.mkstemp(dir=pfad.parent, prefix=pfad.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(inhalt)
        os.replace(tmp, pfad)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def config_laden(pfad: Path) -> configparser.ConfigParser:
    if not pfad.exists():
        sys.exit(f"Keine Konfiguration gefunden: {pfad}\n"
                 "Kopiere config.example.ini nach config.ini und trage deine Daten ein.")
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(pfad, encoding="utf-8")
    fehlend = [f"{s}.{k}" for s, k in [("lidl", "refresh_token"), ("gmail", "adresse"), ("gmail", "app_passwort")]
               if not cfg.get(s, k, fallback="").strip()]
    if fehlend:
        sys.exit("In config.ini fehlt: " + ", ".join(fehlend))
    return cfg


def token_speichern(cfg: configparser.ConfigParser, pfad: Path, neu: str) -> None:
    """Ersetzt nur die Zeile refresh_token in config.ini, Kommentare bleiben erhalten."""
    text = pfad.read_text(encoding="utf-8")
    text, n = re.subn(r"(?m)^(\s*refresh_token\s*=).*$", lambda m: f"{m.group(1)} {neu}", text, count=1)
    if n != 1:
        raise RuntimeError("refresh_token-Zeile in config.ini nicht gefunden")
    atomar_schreiben(pfad, text)
    cfg.set("lidl", "refresh_token", neu)


def token_sichern(api, cfg: configparser.ConfigParser, pfad: Path) -> None:
    """Lidl gibt bei jeder Anmeldung einen neuen Refresh-Token aus – sofort sichern."""
    neu = getattr(api, "refresh_token", "")
    if neu and neu != cfg.get("lidl", "refresh_token").strip():
        token_speichern(cfg, pfad, neu)
        log.info("Neuer Lidl-Token gespeichert.")


def state_laden(pfad: Path) -> dict:
    if pfad.exists():
        return json.loads(pfad.read_text(encoding="utf-8"))
    return {"gesendet": []}


def state_speichern(state: dict, pfad: Path) -> None:
    atomar_schreiben(pfad, json.dumps(state, indent=1, ensure_ascii=False))


# ---------------------------------------------------------------- Bon-Daten

def zahl(wert) -> float:
    """Lidl liefert Beträge als Text mit Komma ("2,19")."""
    if wert is None or wert == "":
        return 0.0
    if isinstance(wert, (int, float)):
        return float(wert)
    text = str(wert)
    return float(text.replace(".", "").replace(",", ".")) if "," in text else float(text)


def euro(wert) -> str:
    return f"{zahl(wert):.2f}".replace(".", ",") + " €"


def bon_datum(bon: dict, eintrag: dict):
    roh = bon.get("date") or eintrag.get("date") or ""
    try:
        return datetime.fromisoformat(str(roh).replace("Z", "+00:00")[:19])
    except ValueError:
        return None


def bon_summe(bon: dict, eintrag: dict):
    return bon.get("totalAmount", eintrag.get("totalAmount"))


def bon_positionen(bon: dict) -> list:
    return bon.get("itemsLine") or bon.get("items") or []


def bon_markt(bon: dict) -> str:
    store = bon.get("store") or {}
    if isinstance(store, dict):
        teile = [store.get("name"), store.get("address"), store.get("postalCode"), store.get("locality")]
        return " ".join(str(t) for t in teile if t)
    return str(store)


def mail_text(bon: dict, eintrag: dict) -> str:
    d = bon_datum(bon, eintrag)
    zeilen = [BETREFF_KENNUNG, ""]
    if d:
        zeilen.append(f"Datum:  {d:%d.%m.%Y %H:%M}")
    if bon_markt(bon):
        zeilen.append(f"Markt:  {bon_markt(bon)}")
    zeilen.append("")
    rabatt_summe = 0.0
    for p in bon_positionen(bon):
        name = p.get("name", "?")
        menge = p.get("quantity", "1")
        einzel = p.get("currentUnitPrice")
        betrag = p.get("originalAmount", einzel)
        einheit = " kg" if p.get("isWeight") else " x"
        zeilen.append(f"{name:<32} {menge}{einheit} {euro(einzel) if einzel else '':>9}  {euro(betrag):>10}")
        for r in p.get("discounts") or []:
            rabatt_summe += zahl(r.get("amount"))
            zeilen.append(f"   Rabatt: {r.get('description', '')}  -{euro(r.get('amount'))}")
        if p.get("deposit"):
            zeilen.append(f"   Pfand: {json.dumps(p['deposit'], ensure_ascii=False)}")
    zeilen.append("")
    if rabatt_summe:
        zeilen.append(f"Rabatte gesamt:  -{euro(rabatt_summe)}")
    if bon_summe(bon, eintrag) is not None:
        zeilen.append(f"SUMME:           {euro(bon_summe(bon, eintrag))}")
    zeilen += ["", "Die vollständigen Daten stehen im JSON-Anhang.", "Gesendet vom Lidl-Bon-Postboten."]
    return "\n".join(zeilen)


def mail_bauen(bon: dict, eintrag: dict, adresse: str) -> EmailMessage:
    d = bon_datum(bon, eintrag)
    datum_text = f"{d:%d.%m.%Y}" if d else "unbekanntem Datum"
    summe = bon_summe(bon, eintrag)
    betreff = f"{BETREFF_KENNUNG} vom {datum_text}" + (f" – {euro(summe)}" if summe is not None else "")
    msg = EmailMessage()
    msg["Subject"] = betreff
    msg["From"] = adresse
    msg["To"] = adresse
    msg["X-Lidl-Bon-Id"] = str(eintrag["id"])
    msg.set_content(mail_text(bon, eintrag))
    dateiname = f"lidl_bon_{d:%Y-%m-%d}_{eintrag['id']}.json" if d else f"lidl_bon_{eintrag['id']}.json"
    msg.add_attachment(json.dumps(bon, indent=2, ensure_ascii=False).encode("utf-8"),
                       maintype="application", subtype="json", filename=dateiname)
    return msg


# ---------------------------------------------------------------- Verbindungen

def lidl_api(cfg):
    try:
        from lidlplus import LidlPlusApi
    except ImportError:
        sys.exit("Paket fehlt. Bitte installieren: pip install lidl-plus")
    return LidlPlusApi(cfg.get("lidl", "sprache", fallback="de"), cfg.get("lidl", "land", fallback="DE"),
                       refresh_token=cfg.get("lidl", "refresh_token").strip())


def smtp_verbinden(cfg):
    adresse = cfg.get("gmail", "adresse").strip()
    passwort = cfg.get("gmail", "app_passwort").replace(" ", "").strip()
    server = smtplib.SMTP_SSL(cfg.get("gmail", "smtp_server", fallback="smtp.gmail.com"),
                              cfg.getint("gmail", "smtp_port", fallback=465), timeout=30)
    try:
        server.login(adresse, passwort)
    except smtplib.SMTPAuthenticationError:
        server.close()
        sys.exit("Gmail-Anmeldung fehlgeschlagen. Prüfe Adresse und App-Passwort in config.ini "
                 "(normales Google-Passwort funktioniert nicht, siehe ANLEITUNG.md).")
    return server


# ---------------------------------------------------------------- Ablauf

def ausfuehren(args, cfg_pfad=CONFIG_DATEI, state_pfad=STATE_DATEI) -> int:
    cfg = config_laden(cfg_pfad)
    adresse = cfg.get("gmail", "adresse").strip()

    if args.test_mail:
        msg = EmailMessage()
        msg["Subject"] = "Lidl-Bon-Postbote: Testmail"
        msg["From"] = msg["To"] = adresse
        msg.set_content("Der Versand funktioniert.")
        with smtp_verbinden(cfg) as server:
            server.send_message(msg)
        log.info("Testmail an %s gesendet.", adresse)
        return 0

    state = state_laden(state_pfad)
    gesendet = set(state["gesendet"])
    api = lidl_api(cfg)
    try:
        liste = api.tickets()
    except Exception as exc:  # noqa: BLE001 – inoffizielle API, Fehlerbild unbekannt
        log.error("Abruf bei Lidl fehlgeschlagen: %s", exc)
        log.error("Ist der Token abgelaufen? Dann 'python token_holen.py' erneut ausführen.")
        return 2
    finally:
        token_sichern(api, cfg, cfg_pfad)

    seit = datetime.strptime(args.seit, "%Y-%m-%d") if args.seit else None
    neue = []
    for e in liste:
        if str(e["id"]) in gesendet:
            continue
        d = bon_datum({}, e)
        if seit and d and d.replace(tzinfo=None) < seit:
            continue
        neue.append(e)
    neue.sort(key=lambda e: str(e.get("date", "")))
    log.info("%d Bons bei Lidl, davon %d neu.", len(liste), len(neue))
    if not neue:
        return 0

    stapel = neue[: args.max]
    server = None if args.trocken else smtp_verbinden(cfg)
    try:
        for e in stapel:
            bon = api.ticket(e["id"])
            msg = mail_bauen(bon, e, adresse)
            if args.trocken:
                log.info("[trocken] würde senden: %s", msg["Subject"])
                continue
            server.send_message(msg)
            gesendet.add(str(e["id"]))
            state["gesendet"] = sorted(gesendet)
            state_speichern(state, state_pfad)
            log.info("Gesendet: %s", msg["Subject"])
    finally:
        if server:
            server.quit()
        token_sichern(api, cfg, cfg_pfad)
    if len(neue) > len(stapel):
        log.info("%d weitere Bons folgen beim nächsten Lauf (Limit --max %d).", len(neue) - len(stapel), args.max)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Schickt neue Lidl-Plus-Bons per Mail an Gmail.")
    p.add_argument("--max", type=int, default=40, help="höchstens so viele Mails pro Lauf (Standard 40)")
    p.add_argument("--seit", metavar="JJJJ-MM-TT", help="ältere Bons ignorieren")
    p.add_argument("--trocken", action="store_true", help="nichts senden, nur anzeigen")
    p.add_argument("--test-mail", action="store_true", help="nur eine Testmail senden")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_DATEI, encoding="utf-8"), logging.StreamHandler()])
    return ausfuehren(args)


if __name__ == "__main__":
    sys.exit(main())

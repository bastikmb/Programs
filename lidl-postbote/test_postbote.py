#!/usr/bin/env python3
"""Offline-Selbsttest für den Lidl-Bon-Postboten.

Braucht keine Zugangsdaten und kein Internet: Lidl Plus und Gmail werden
nachgebaut. Deine echte config.ini, state.json und postbote.log bleiben
unangetastet, alles läuft in einem temporären Ordner.

Aufruf:
    python test_postbote.py
"""

import argparse
import importlib.util
import logging
import sys
import tempfile
import types
from pathlib import Path

sys.dont_write_bytecode = True
HIER = Path(__file__).resolve().parent

# ---------------------------------------------------------------- Nachbauten

BEISPIEL_BONS = [
    {"id": "A1", "date": "2026-10-01T18:20:00", "totalAmount": "23,45"},
    {"id": "B2", "date": "2026-10-05T10:02:00", "totalAmount": "8,10"},
    {"id": "C3", "date": "2026-09-01T09:00:00", "totalAmount": "4,99"},
]


class FakeLidlPlusApi:
    """Verhält sich wie lidlplus.LidlPlusApi, inklusive rotierendem Token."""

    def __init__(self, language, country, refresh_token=""):
        self.refresh_token = refresh_token

    def tickets(self):
        self.refresh_token += "-neu"
        return [dict(b) for b in BEISPIEL_BONS]

    def ticket(self, ticket_id):
        b = next(x for x in BEISPIEL_BONS if x["id"] == ticket_id)
        return {**b, "store": {"name": "Lidl", "locality": "Teststadt"},
                "itemsLine": [{"name": "Vegane Frikadellen", "quantity": "2", "currentUnitPrice": "2,19",
                               "originalAmount": "4,38", "isWeight": False,
                               "discounts": [{"description": "5€ Coupon", "amount": "0,21"}]},
                              {"name": "Bananen", "quantity": "0,512", "currentUnitPrice": "1,99",
                               "originalAmount": "1,02", "isWeight": True, "discounts": []}]}


GESENDET = []


class FakeSMTP:
    def __init__(self, host, port, timeout=None):
        pass

    def login(self, adresse, passwort):
        if passwort != "testpasswort":
            raise AssertionError("App-Passwort wurde nicht korrekt gelesen")

    def send_message(self, msg):
        GESENDET.append(msg)

    def quit(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


CONFIG = """# Kommentar oben
[lidl]
# Kommentar zum Token
refresh_token = start
land = DE
sprache = de

[gmail]
adresse = test@example.com
app_passwort = test passwort
"""

# ---------------------------------------------------------------- Test


def main() -> int:
    sys.modules["lidlplus"] = types.SimpleNamespace(LidlPlusApi=FakeLidlPlusApi)
    spec = importlib.util.spec_from_file_location("lidl_postbote", HIER / "lidl_postbote.py")
    lp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lp)
    lp.smtplib.SMTP_SSL = FakeSMTP
    logging.basicConfig(level=logging.WARNING)

    fehler = []

    def pruefe(name, bedingung, details=""):
        print(("OK     " if bedingung else "FEHLER ") + name + ("" if bedingung else f"  ({details})"))
        if not bedingung:
            fehler.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "config.ini"
        state = Path(tmp) / "state.json"
        cfg.write_text(CONFIG, encoding="utf-8")

        def lauf(**opt):
            args = argparse.Namespace(**{"max": 40, "seit": None, "trocken": False, "test_mail": False, **opt})
            vorher = len(GESENDET)
            code = lp.ausfuehren(args, cfg_pfad=cfg, state_pfad=state)
            return code, GESENDET[vorher:]

        code, mails = lauf(test_mail=True)
        pruefe("Testmail wird gesendet", code == 0 and len(mails) == 1)

        code, mails = lauf(trocken=True)
        pruefe("Trockenlauf sendet nichts", code == 0 and not mails, f"{len(mails)} Mails")

        code, mails = lauf(max=2)
        pruefe("Limit --max 2 greift", len(mails) == 2, f"{len(mails)} Mails")
        pruefe("Älteste Bons zuerst", [m["X-Lidl-Bon-Id"] for m in mails] == ["C3", "A1"],
               [m["X-Lidl-Bon-Id"] for m in mails])

        code, mails = lauf()
        pruefe("Restlicher Bon folgt im nächsten Lauf", len(mails) == 1, f"{len(mails)} Mails")

        code, mails = lauf()
        pruefe("Kein Bon wird doppelt gesendet", not mails, f"{len(mails)} Mails")

        m = GESENDET[1]
        pruefe("Betreff mit Datum und Summe", m["Subject"] == "Lidl Plus Bon vom 01.09.2026 – 4,99 €", m["Subject"])
        anhang = list(m.iter_attachments())
        pruefe("JSON-Anhang vorhanden",
               len(anhang) == 1 and anhang[0].get_content_type() == "application/json", len(anhang))
        text = m.get_body(("plain",)).get_content()
        pruefe("Positionen und Rabatt im Mailtext", "Vegane Frikadellen" in text and "-0,21 €" in text)

        inhalt = cfg.read_text(encoding="utf-8")
        pruefe("Neuer Lidl-Token gespeichert", "refresh_token = start-neu" in inhalt)
        pruefe("Kommentare in config.ini bleiben erhalten", inhalt.count("#") == 2)

        code, mails = lauf(seit="2026-10-02")
        pruefe("--seit wird akzeptiert", code == 0)

    print()
    if fehler:
        print(f"{len(fehler)} Prüfung(en) fehlgeschlagen.")
        return 1
    print("Alle Prüfungen bestanden. Das Programm läuft auf diesem Rechner.")
    print("Nächster Schritt: config.ini ausfüllen und 'python lidl_postbote.py --test-mail' (siehe ANLEITUNG.md).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

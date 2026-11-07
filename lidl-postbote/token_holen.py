#!/usr/bin/env python3
"""Holt einmalig den Lidl-Plus-Refresh-Token und trägt ihn in config.ini ein.

Ersatz für "lidl-plus auth", das unter neueren Python-Versionen (z. B. 3.14)
abstürzt. Braucht das Paket "lidl-plus[auth]" und Chrome, Edge oder Firefox.

Aufruf:
    python token_holen.py
    python token_holen.py --browser-zeigen   # Browserfenster sichtbar lassen (zur Fehlersuche)
"""

import argparse
import getpass
import re
import sys
from pathlib import Path

HIER = Path(__file__).resolve().parent
CONFIG_DATEI = HIER / "config.ini"


def in_config_eintragen(token: str) -> bool:
    if not CONFIG_DATEI.exists():
        return False
    text = CONFIG_DATEI.read_text(encoding="utf-8")
    text, n = re.subn(r"(?m)^(\s*refresh_token\s*=).*$", lambda m: f"{m.group(1)} {token}", text, count=1)
    if n != 1:
        return False
    CONFIG_DATEI.write_text(text, encoding="utf-8")
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="Lidl-Plus-Token holen")
    p.add_argument("--browser-zeigen", action="store_true", help="Browserfenster nicht verstecken")
    p.add_argument("--land", default="DE")
    p.add_argument("--sprache", default="de")
    args = p.parse_args()

    try:
        from lidlplus import LidlPlusApi
    except ImportError:
        print('Paket fehlt. Bitte installieren: pip install "lidl-plus[auth]" "blinker<1.8" "setuptools<81"')
        return 1

    print("Anmeldung bei Lidl Plus. Deine Eingaben werden nur an Lidl geschickt und nicht gespeichert.\n")
    telefon = input("Lidl-Plus-Telefonnummer (z. B. +4917612345678): ").strip()
    passwort = getpass.getpass("Lidl-Plus-Passwort (Eingabe bleibt unsichtbar): ")

    api = LidlPlusApi(args.sprache, args.land)
    try:
        api.login(telefon, passwort,
                  verify_token_func=lambda: input("Code aus der SMS: ").strip(),
                  headless=not args.browser_zeigen)
    except Exception as exc:  # noqa: BLE001 – Fehlerbilder der inoffiziellen API sind vielfältig
        print(f"\nAnmeldung fehlgeschlagen: {type(exc).__name__}: {exc}")
        print("Prüfe Telefonnummer und Passwort. Zur Fehlersuche: python token_holen.py --browser-zeigen")
        return 1

    token = api.refresh_token
    if not token:
        print("\nKein Token erhalten. Bitte erneut versuchen.")
        return 1
    if in_config_eintragen(token):
        print("\nFertig. Der Token steht jetzt in config.ini.")
    else:
        print("\nToken erhalten, aber config.ini nicht gefunden.")
        print("Lege config.ini aus config.example.ini an und trage diesen Token bei refresh_token ein:\n")
        print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())

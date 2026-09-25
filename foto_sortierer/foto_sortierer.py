#!/usr/bin/env python3
"""
Foto- & Video-Sortierer
=======================

Sortiert Fotos und Videos (z. B. vom iPhone importiert) anhand ihres
Aufnahmedatums in vorbereitete Ordner nach Jahr/Monat.

Sicherheitsregeln – dieses Programm
  * löscht NIEMALS eine Datei,
  * überschreibt NIEMALS eine vorhandene Datei,
  * kopiert standardmäßig (das Original bleibt im Quellordner),
  * verschiebt nur, wenn ausdrücklich gewünscht – und dann nur per
    Umbenennen auf demselben Laufwerk (die Datei wird nie gelöscht und
    neu geschrieben). Auf einem anderen Laufwerk wird stattdessen kopiert.

Start ohne Parameter öffnet die grafische Oberfläche.
Kommandozeile:  python foto_sortierer.py --quelle QUELLE --ziel ZIEL [--vorschau]
Nur Python-Standardbibliothek, keine Zusatzpakete nötig.
"""

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

BILD_ENDUNGEN = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tif",
                 ".tiff", ".dng", ".webp", ".bmp"}
VIDEO_ENDUNGEN = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".mts"}
# Begleitdateien des iPhones (Bearbeitungsinfos) – landen beim zugehörigen Foto
BEGLEIT_ENDUNGEN = {".aae"}
ALLE_ENDUNGEN = BILD_ENDUNGEN | VIDEO_ENDUNGEN | BEGLEIT_ENDUNGEN

OHNE_DATUM_ORDNER = "_Ohne Datum"
PROTOKOLL_ORDNER = "_Sortierprotokolle"
TEMP_ENDUNG = ".kopie-laeuft"
EINSTELLUNGEN_DATEI = Path.home() / ".foto_sortierer.json"

MONATE_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
             "August", "September", "Oktober", "November", "Dezember"]
MONATE_DE_KURZ = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug",
                  "Sep", "Okt", "Nov", "Dez"]
MONATE_EN = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
MONATE_EN_KURZ = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
                  "Sep", "Oct", "Nov", "Dec"]

MONATS_TOKENS = {}
for _i in range(12):
    for _liste in (MONATE_DE, MONATE_DE_KURZ, MONATE_EN, MONATE_EN_KURZ):
        MONATS_TOKENS[_liste[_i].lower()] = _i + 1
MONATS_TOKENS.update({"maerz": 3, "marz": 3, "jänner": 1, "jaenner": 1,
                      "sept": 9, "mrz": 3})


# ---------------------------------------------------------------------------
# Aufnahmedatum ermitteln
# ---------------------------------------------------------------------------

def _plausibel(d):
    return d is not None and 1990 <= d.year <= dt.date.today().year + 1


_ZEIT_RE = re.compile(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def _parse_zeit(text):
    if not text:
        return None
    m = _ZEIT_RE.search(text)
    if not m:
        return None
    try:
        d = dt.datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return None
    return d if _plausibel(d) else None


def _datum_aus_tiff(daten):
    """Liest DateTimeOriginal (bzw. Ersatz-Tags) aus einem EXIF-TIFF-Block."""
    if len(daten) < 8:
        return None
    if daten[:2] == b"II":
        e = "<"
    elif daten[:2] == b"MM":
        e = ">"
    else:
        return None
    if struct.unpack(e + "H", daten[2:4])[0] != 42:
        return None

    def lese_ifd(off):
        tags = {}
        if off + 2 > len(daten):
            return tags
        anzahl = struct.unpack_from(e + "H", daten, off)[0]
        for i in range(min(anzahl, 1000)):
            p = off + 2 + 12 * i
            if p + 12 > len(daten):
                break
            tag, typ, count = struct.unpack_from(e + "HHI", daten, p)
            tags[tag] = (typ, count, daten[p + 8:p + 12])
        return tags

    def als_text(eintrag):
        typ, count, wert = eintrag
        if typ != 2:
            return None
        if count <= 4:
            roh = wert[:count]
        else:
            o = struct.unpack(e + "I", wert)[0]
            roh = daten[o:o + count]
        return roh.split(b"\0")[0].decode("ascii", "ignore")

    ifd0 = lese_ifd(struct.unpack(e + "I", daten[4:8])[0])
    kandidaten = []
    if 0x8769 in ifd0:  # Zeiger auf Exif-IFD
        exif = lese_ifd(struct.unpack(e + "I", ifd0[0x8769][2])[0])
        for tag in (0x9003, 0x9004):  # DateTimeOriginal, DateTimeDigitized
            if tag in exif:
                kandidaten.append(als_text(exif[tag]))
    if 0x0132 in ifd0:  # DateTime
        kandidaten.append(als_text(ifd0[0x0132]))
    for k in kandidaten:
        d = _parse_zeit(k)
        if d:
            return d
    return None


def _jpeg_datum(f):
    f.seek(0)
    if f.read(2) != b"\xff\xd8":
        return None
    while True:
        b = f.read(1)
        if not b:
            return None
        if b != b"\xff":
            return None
        marker = f.read(1)
        while marker == b"\xff":
            marker = f.read(1)
        if not marker:
            return None
        m = marker[0]
        if m in (0xD9, 0xDA):  # Bildende / Bilddaten beginnen
            return None
        if m == 0x01 or 0xD0 <= m <= 0xD7:
            continue
        roh = f.read(2)
        if len(roh) < 2:
            return None
        laenge = struct.unpack(">H", roh)[0]
        inhalt = f.read(max(laenge - 2, 0))
        if m == 0xE1 and inhalt.startswith(b"Exif\0\0"):
            d = _datum_aus_tiff(inhalt[6:])
            if d:
                return d


def _boxen(f, start, ende):
    """Iteriert über ISO-BMFF/QuickTime-Boxen einer Datei: (typ, inhalt_start, box_ende)."""
    pos = start
    while pos + 8 <= ende:
        f.seek(pos)
        kopf = f.read(8)
        if len(kopf) < 8:
            return
        groesse, typ = struct.unpack(">I4s", kopf)
        kopf_len = 8
        if groesse == 1:
            groesse = struct.unpack(">Q", f.read(8))[0]
            kopf_len = 16
        elif groesse == 0:
            groesse = ende - pos
        if groesse < kopf_len:
            return
        yield typ, pos + kopf_len, min(pos + groesse, ende)
        pos += groesse


def _boxen_bytes(daten, start, ende):
    pos = start
    while pos + 8 <= ende:
        groesse, typ = struct.unpack_from(">I4s", daten, pos)
        kopf_len = 8
        if groesse == 1:
            if pos + 16 > ende:
                return
            groesse = struct.unpack_from(">Q", daten, pos + 8)[0]
            kopf_len = 16
        elif groesse == 0:
            groesse = ende - pos
        if groesse < kopf_len:
            return
        yield typ, pos + kopf_len, min(pos + groesse, ende)
        pos += groesse


def _uint(daten, pos, groesse):
    if groesse == 0:
        return 0, pos
    fmt = {2: ">H", 4: ">I", 8: ">Q"}[groesse]
    return struct.unpack_from(fmt, daten, pos)[0], pos + groesse


def _heif_datum(f, dateigroesse):
    """HEIC/HEIF: EXIF-Item über die meta-Box (iinf + iloc) finden."""
    for typ, s, e in _boxen(f, 0, dateigroesse):
        if typ != b"meta":
            continue
        f.seek(s)
        meta = f.read(min(e - s, 16 * 1024 * 1024))
        exif_ids = set()
        orte = {}
        for btyp, bs, be in _boxen_bytes(meta, 4, len(meta)):  # 4 = version/flags
            if btyp == b"iinf":
                version = meta[bs]
                p = bs + 4 + (2 if version == 0 else 4)
                for ityp, is_, ie in _boxen_bytes(meta, p, be):
                    if ityp != b"infe":
                        continue
                    v = meta[is_]
                    if v < 2:
                        continue
                    q = is_ + 4
                    item_id, q = _uint(meta, q, 2 if v == 2 else 4)
                    q += 2  # item_protection_index
                    if meta[q:q + 4] == b"Exif":
                        exif_ids.add(item_id)
            elif btyp == b"iloc":
                version = meta[bs]
                p = bs + 4
                off_size, len_size = meta[p] >> 4, meta[p] & 0x0F
                base_size = meta[p + 1] >> 4
                idx_size = (meta[p + 1] & 0x0F) if version in (1, 2) else 0
                p += 2
                anzahl, p = _uint(meta, p, 2 if version < 2 else 4)
                for _ in range(anzahl):
                    item_id, p = _uint(meta, p, 2 if version < 2 else 4)
                    methode = 0
                    if version in (1, 2):
                        methode = struct.unpack_from(">H", meta, p)[0] & 0x0F
                        p += 2
                    p += 2  # data_reference_index
                    basis, p = _uint(meta, p, base_size)
                    extents, p = _uint(meta, p, 2)
                    liste = []
                    for _ in range(extents):
                        if idx_size:
                            _, p = _uint(meta, p, idx_size)
                        eo, p = _uint(meta, p, off_size)
                        el, p = _uint(meta, p, len_size)
                        liste.append((basis + eo, el))
                    if methode == 0 and liste:
                        orte[item_id] = liste[0]
        for item_id in exif_ids:
            if item_id not in orte:
                continue
            off, laenge = orte[item_id]
            f.seek(off)
            daten = f.read(min(laenge or 1024 * 1024, 1024 * 1024))
            if len(daten) < 4:
                continue
            tiff_off = struct.unpack(">I", daten[:4])[0]
            d = _datum_aus_tiff(daten[4 + tiff_off:])
            if d:
                return d
        return None
    return None


def _png_datum(f):
    f.seek(0)
    if f.read(8) != b"\x89PNG\r\n\x1a\n":
        return None
    while True:
        kopf = f.read(8)
        if len(kopf) < 8:
            return None
        laenge, typ = struct.unpack(">I4s", kopf)
        if typ == b"IDAT" or typ == b"IEND":
            return None
        inhalt = f.read(laenge)
        f.read(4)  # CRC
        if typ == b"eXIf":
            d = _datum_aus_tiff(inhalt)
            if d:
                return d
        elif typ in (b"iTXt", b"tEXt"):
            m = re.search(rb"(?:DateTimeOriginal|DateCreated)[^0-9]{0,40}(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
                          inhalt)
            if m:
                d = _parse_zeit(m.group(1).decode())
                if d:
                    return d


_APPLE_DATUM_RE = re.compile(rb"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:[+-]\d{2}:?\d{2}|Z)?")
_QT_EPOCHE = dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)


def _video_datum(f, dateigroesse):
    """MOV/MP4: Apple-Aufnahmedatum (Ortszeit) bevorzugt, sonst mvhd (UTC -> lokale Zeit)."""
    for typ, s, e in _boxen(f, 0, dateigroesse):
        if typ != b"moov":
            continue
        f.seek(s)
        moov = f.read(min(e - s, 64 * 1024 * 1024))
        schluessel = moov.find(b"com.apple.quicktime.creationdate")
        if schluessel >= 0:
            m = _APPLE_DATUM_RE.search(moov, schluessel)
            if m:
                d = _parse_zeit(m.group(1).decode())
                if d:
                    return d
        for btyp, bs, be in _boxen_bytes(moov, 0, len(moov)):
            if btyp != b"mvhd":
                continue
            version = moov[bs]
            if version == 1:
                sek = struct.unpack_from(">Q", moov, bs + 4)[0]
            else:
                sek = struct.unpack_from(">I", moov, bs + 4)[0]
            if sek == 0:
                return None
            try:
                d = (_QT_EPOCHE + dt.timedelta(seconds=sek)).astimezone().replace(tzinfo=None)
            except (OverflowError, OSError, ValueError):
                return None
            return d if _plausibel(d) else None
        return None
    return None


def _exif_suche(f):
    """Notlösung für alle Bildformate: nach einem EXIF-Block in den ersten 512 KB suchen."""
    f.seek(0)
    daten = f.read(512 * 1024)
    pos = daten.find(b"Exif\0\0")
    while pos >= 0:
        d = _datum_aus_tiff(daten[pos + 6:])
        if d:
            return d
        pos = daten.find(b"Exif\0\0", pos + 1)
    m = re.search(rb"(?:exif:DateTimeOriginal|photoshop:DateCreated|xmp:CreateDate)[=\">\s]{1,3}"
                  rb"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", daten)
    if m:
        return _parse_zeit(m.group(1).decode())
    return None


_DATEINAME_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")


def datum_aus_dateiname(name):
    m = _DATEINAME_RE.search(name)
    if not m:
        return None
    try:
        d = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return d if _plausibel(d) else None


def metadaten_datum(pfad):
    """Aufnahmedatum aus den Datei-Metadaten oder None."""
    endung = pfad.suffix.lower()
    try:
        with open(pfad, "rb") as f:
            groesse = os.fstat(f.fileno()).st_size
            d = None
            try:
                if endung in (".jpg", ".jpeg"):
                    d = _jpeg_datum(f)
                elif endung in (".heic", ".heif"):
                    d = _heif_datum(f, groesse)
                elif endung == ".png":
                    d = _png_datum(f)
                elif endung in (".mov", ".mp4", ".m4v", ".3gp"):
                    d = _video_datum(f, groesse)
                elif endung in (".tif", ".tiff", ".dng"):
                    f.seek(0)
                    d = _datum_aus_tiff(f.read(4 * 1024 * 1024))
            except (struct.error, IndexError, ValueError, KeyError):
                d = None
            if d is None and endung in BILD_ENDUNGEN:
                d = _exif_suche(f)
            return d
    except OSError:
        return None


def ermittle_datum(pfad, mtime_erlaubt=True):
    """Liefert (datum, quelle). quelle beschreibt, woher das Datum stammt."""
    d = metadaten_datum(pfad)
    if d:
        return d, "Aufnahmedatum (Metadaten)"
    d = datum_aus_dateiname(pfad.name)
    if d:
        return d, "Dateiname"
    if mtime_erlaubt:
        try:
            return dt.datetime.fromtimestamp(pfad.stat().st_mtime), "Änderungsdatum der Datei"
        except (OSError, ValueError):
            pass
    return None, "kein Datum gefunden"


# ---------------------------------------------------------------------------
# Vorbereitete Zielordner erkennen
# ---------------------------------------------------------------------------

def monat_aus_ordnername(name, jahr=None):
    """Erkennt den Monat in Ordnernamen wie '01', '1', '01 Januar', 'Januar',
    '2024-01', '2024_01 Urlaub', 'März' … Liefert 1–12 oder None."""
    n = name.strip().lower()
    m = re.match(r"^(\d{4})[-_. ]?(\d{1,2})(?!\d)", n)
    if m:
        if jahr is None or int(m.group(1)) == jahr:
            mon = int(m.group(2))
            if 1 <= mon <= 12:
                return mon
        return None
    m = re.match(r"^(\d{1,2})(?!\d)", n)
    if m:
        mon = int(m.group(1))
        return mon if 1 <= mon <= 12 else None
    for token in re.findall(r"[a-zäöüß]+", n):
        if token in MONATS_TOKENS:
            return MONATS_TOKENS[token]
    return None


def _ersetze_monatsnamen(text, neu):
    def ersatz(m):
        wort = m.group(0)
        klein = wort.lower()
        if klein not in MONATS_TOKENS:
            return wort
        if klein in (x.lower() for x in MONATE_EN) and klein not in (x.lower() for x in MONATE_DE):
            neu_wort = MONATE_EN[neu - 1]
        elif klein in (x.lower() for x in MONATE_EN_KURZ) and klein not in (x.lower() for x in MONATE_DE_KURZ):
            neu_wort = MONATE_EN_KURZ[neu - 1]
        elif len(klein) <= 4 and klein not in ("juni", "juli"):
            neu_wort = MONATE_DE_KURZ[neu - 1]
        else:
            neu_wort = MONATE_DE[neu - 1]
        if wort.isupper() and len(wort) > 1:
            return neu_wort.upper()
        if wort.islower():
            return neu_wort.lower()
        return neu_wort
    return re.sub(r"[A-Za-zÄÖÜäöüß]+", ersatz, text)


def ordnername_nach_vorlage(vorlage, jahr, monat):
    """Baut einen neuen Monatsordnernamen im Stil eines vorhandenen, z. B.
    '01 - Januar' -> '03 - März' oder '2024-01' -> '2024-03'."""
    m = re.match(r"^(\d{4})([-_. ]?)(\d{1,2})(?!\d)(.*)$", vorlage)
    if m:
        return f"{jahr}{m.group(2)}{monat:0{len(m.group(3))}d}" + _ersetze_monatsnamen(m.group(4), monat)
    m = re.match(r"^(\d{1,2})(?!\d)(.*)$", vorlage)
    if m:
        return f"{monat:0{len(m.group(1))}d}" + _ersetze_monatsnamen(m.group(2), monat)
    neu = _ersetze_monatsnamen(vorlage, monat)
    return neu if neu != vorlage else f"{monat:02d}"


def _unterordner(pfad):
    try:
        return sorted((p for p in pfad.iterdir() if p.is_dir() and not p.name.startswith(".")),
                      key=lambda p: p.name.lower())
    except OSError:
        return []


class ZielStruktur:
    """Findet (oder plant) den passenden Jahr/Monat-Ordner im Zielverzeichnis."""

    def __init__(self, ziel):
        self.ziel = Path(ziel)
        self.cache = {}
        self.neue_ordner = []   # Ordner, die (noch) angelegt werden müssen

    def _jahresordner(self, jahr):
        exakt, andere = None, None
        for p in _unterordner(self.ziel):
            if p.name == str(jahr):
                exakt = p
            elif andere is None and re.match(rf"^{jahr}(?![\d])(?![-_. ]?\d)", p.name):
                andere = p  # z. B. "2024 Fotos"
        return exakt or andere

    def _flache_monatsordner(self):
        treffer = []
        for p in _unterordner(self.ziel):
            m = re.match(r"^(\d{4})[-_. ]?(\d{1,2})(?!\d)", p.name)
            if m and 1 <= int(m.group(2)) <= 12:
                treffer.append((int(m.group(1)), int(m.group(2)), p))
        return treffer

    def ordner_fuer(self, datum):
        schluessel = (datum.year, datum.month)
        if schluessel in self.cache:
            return self.cache[schluessel]
        jahr, monat = schluessel
        ergebnis = None

        jahres_ordner = self._jahresordner(jahr)
        if jahres_ordner is not None:
            vorlagen = []
            for p in _unterordner(jahres_ordner):
                mon = monat_aus_ordnername(p.name, jahr)
                if mon == monat:
                    ergebnis = p
                    break
                if mon is not None:
                    vorlagen.append(p.name)
            if ergebnis is None:
                name = ordnername_nach_vorlage(vorlagen[0], jahr, monat) if vorlagen else f"{monat:02d}"
                ergebnis = jahres_ordner / name
                self.neue_ordner.append(ergebnis)
        else:
            flach = self._flache_monatsordner()
            for j, mon, p in flach:
                if (j, mon) == schluessel:
                    ergebnis = p
                    break
            if ergebnis is None and flach:
                ergebnis = self.ziel / ordnername_nach_vorlage(flach[0][2].name, jahr, monat)
                self.neue_ordner.append(ergebnis)
            if ergebnis is None:
                # Stil anderer Jahresordner übernehmen, falls vorhanden
                name = f"{monat:02d}"
                for p in _unterordner(self.ziel):
                    if re.fullmatch(r"\d{4}", p.name):
                        vorlage = next((u.name for u in _unterordner(p)
                                        if monat_aus_ordnername(u.name, int(p.name))), None)
                        if vorlage:
                            name = ordnername_nach_vorlage(vorlage, jahr, monat)
                            break
                ergebnis = self.ziel / str(jahr) / name
                self.neue_ordner.append(ergebnis)

        self.cache[schluessel] = ergebnis
        return ergebnis


# ---------------------------------------------------------------------------
# Sicheres Kopieren / Verschieben (nie löschen, nie überschreiben)
# ---------------------------------------------------------------------------

def _hash(pfad):
    h = hashlib.sha256()
    with open(pfad, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def gleicher_inhalt(a, b):
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        return _hash(a) == _hash(b)
    except OSError:
        return False


def _kopiere_ohne_ueberschreiben(quelle, ziel):
    """Kopiert in eine temporäre Datei und benennt sie erst nach erfolgreicher
    Prüfung um. Vorhandene Dateien werden nie angetastet."""
    temp = ziel.with_name("." + ziel.name + TEMP_ENDUNG)
    with open(quelle, "rb") as fi, open(temp, "xb") as fo:  # "x" = nur neu anlegen
        shutil.copyfileobj(fi, fo, 1024 * 1024)
    shutil.copystat(quelle, temp)
    if os.path.getsize(temp) != os.path.getsize(quelle):
        raise OSError(f"Kopie unvollständig, liegt als {temp.name} im Zielordner")
    if ziel.exists():
        raise FileExistsError(f"{ziel} ist während des Kopierens entstanden; Kopie liegt als {temp.name} vor")
    os.rename(temp, ziel)


def _verschiebe_ohne_ueberschreiben(quelle, ziel):
    """Verschiebt nur per Umbenennen auf demselben Laufwerk. Gibt False zurück,
    wenn das nicht möglich ist (dann wird stattdessen kopiert)."""
    try:
        if os.stat(quelle).st_dev != os.stat(ziel.parent).st_dev:
            return False
    except OSError:
        return False
    if ziel.exists():
        raise FileExistsError(str(ziel))
    try:
        os.rename(quelle, ziel)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Sortier-Ablauf
# ---------------------------------------------------------------------------

def _ist_unterhalb(pfad, basis):
    try:
        pfad.relative_to(basis)
        return True
    except ValueError:
        return False


def finde_dateien(quelle, ziel):
    quelle = Path(quelle).resolve()
    ziel = Path(ziel).resolve()
    # Zielordner nicht erneut durchsuchen, falls er im Quellordner liegt
    ziel_ausschliessen = ziel != quelle and _ist_unterhalb(ziel, quelle)
    dateien = []
    for wurzel, ordner, namen in os.walk(quelle):
        w = Path(wurzel)
        ordner[:] = sorted(o for o in ordner
                           if not o.startswith(".")
                           and not (ziel_ausschliessen and (w / o).resolve() == ziel))
        for n in sorted(namen):
            if n.startswith(".") or n.endswith(TEMP_ENDUNG):
                continue
            if Path(n).suffix.lower() in ALLE_ENDUNGEN:
                dateien.append(w / n)
    return dateien


def _stamm_fuer_begleitdatei(name):
    # iPhone: IMG_1234.HEIC, bearbeitet IMG_E1234.HEIC, Begleitdatei IMG_O1234.AAE
    stamm = Path(name).stem.upper()
    return re.sub(r"^IMG_[EO](\d)", r"IMG_\1", stamm)


class Sortierer:
    def __init__(self, quelle, ziel, verschieben=False, vorschau=False,
                 ohne_datum_separat=False, melde=print, fortschritt=None, abbruch=None):
        self.quelle = Path(quelle)
        self.ziel = Path(ziel)
        self.verschieben = verschieben
        self.vorschau = vorschau
        self.ohne_datum_separat = ohne_datum_separat
        self.melde = melde
        self.fortschritt = fortschritt or (lambda i, n: None)
        self.abbruch = abbruch or (lambda: False)
        self.struktur = ZielStruktur(self.ziel)
        self.reserviert = {}  # geplante Zielpfade -> Quelldatei (für die Vorschau)
        self.protokoll = []
        self.zaehler = {"kopiert": 0, "verschoben": 0, "duplikat": 0, "fehler": 0,
                        "ohne_datum": 0, "datum_geschaetzt": 0}

    def _eindeutiges_ziel(self, ordner, quelle):
        stamm, endung = quelle.stem, quelle.suffix
        i = 0
        while True:
            name = quelle.name if i == 0 else f"{stamm} ({i}){endung}"
            kandidat = ordner / name
            if kandidat in self.reserviert:
                if gleicher_inhalt(quelle, self.reserviert[kandidat]):
                    return None, kandidat
            elif kandidat.exists():
                if gleicher_inhalt(quelle, kandidat):
                    return None, kandidat
            else:
                return kandidat, None
            i += 1

    def _ordner_anlegen(self, ordner):
        if not self.vorschau and not ordner.exists():
            ordner.mkdir(parents=True, exist_ok=True)
            self.melde(f"Ordner angelegt: {ordner}")

    def _protokolliere(self, aktion, quelle, ziel, datum, datumsquelle, hinweis=""):
        self.protokoll.append({
            "Aktion": aktion,
            "Quelle": str(quelle),
            "Ziel": str(ziel) if ziel else "",
            "Datum": datum.strftime("%Y-%m-%d %H:%M:%S") if datum else "",
            "Datumsquelle": datumsquelle,
            "Hinweis": hinweis,
        })

    def ausfuehren(self):
        if not self.quelle.is_dir():
            raise FileNotFoundError(f"Quellordner nicht gefunden: {self.quelle}")
        if not self.ziel.is_dir():
            raise FileNotFoundError(f"Zielordner nicht gefunden: {self.ziel}")

        self.melde(f"Durchsuche {self.quelle} …")
        dateien = finde_dateien(self.quelle, self.ziel)
        medien = [p for p in dateien if p.suffix.lower() not in BEGLEIT_ENDUNGEN]
        begleit = [p for p in dateien if p.suffix.lower() in BEGLEIT_ENDUNGEN]
        self.melde(f"{len(medien)} Fotos/Videos und {len(begleit)} Begleitdateien gefunden.")
        if self.vorschau:
            self.melde("VORSCHAU – es werden keine Dateien kopiert oder verschoben.")

        bekannte_daten = {}
        gesamt = len(dateien)
        for i, pfad in enumerate(medien + begleit, 1):
            if self.abbruch():
                self.melde("Abgebrochen.")
                break
            self.fortschritt(i, gesamt)
            if pfad.suffix.lower() in BEGLEIT_ENDUNGEN:
                datum = bekannte_daten.get((pfad.parent, _stamm_fuer_begleitdatei(pfad.name)))
                datumsquelle = "wie zugehöriges Foto"
                if datum is None:
                    datum, datumsquelle = ermittle_datum(pfad, not self.ohne_datum_separat)
            else:
                datum, datumsquelle = ermittle_datum(pfad, not self.ohne_datum_separat)
                if datum:
                    bekannte_daten[(pfad.parent, _stamm_fuer_begleitdatei(pfad.name))] = datum
            self._verarbeite(pfad, datum, datumsquelle)

        if self.struktur.neue_ordner:
            self.melde("")
            self.melde("Folgende Monatsordner fehlten und " +
                       ("würden angelegt:" if self.vorschau else "wurden angelegt:"))
            for o in self.struktur.neue_ordner:
                self.melde(f"  {o}")

        if not self.vorschau and self.protokoll:
            self._protokoll_schreiben()
        return self.zaehler

    def _verarbeite(self, pfad, datum, datumsquelle):
        try:
            if datum is None:
                ordner = self.ziel / OHNE_DATUM_ORDNER
                self.zaehler["ohne_datum"] += 1
            else:
                ordner = self.struktur.ordner_fuer(datum)
                if datumsquelle == "Änderungsdatum der Datei":
                    self.zaehler["datum_geschaetzt"] += 1

            ziel, duplikat = self._eindeutiges_ziel(ordner, pfad)
            if duplikat is not None:
                self.zaehler["duplikat"] += 1
                self.melde(f"= übersprungen (bereits vorhanden): {pfad.name} -> {duplikat}")
                self._protokolliere("übersprungen (Duplikat)", pfad, duplikat, datum, datumsquelle)
                return

            hinweis = ""
            if ziel.name != pfad.name:
                hinweis = f"umbenannt, da {pfad.name} mit anderem Inhalt schon existiert"

            if self.vorschau:
                self.reserviert[ziel] = pfad
                aktion = "verschieben" if self.verschieben else "kopieren"
            else:
                self._ordner_anlegen(ordner)
                aktion = None
                if self.verschieben:
                    if _verschiebe_ohne_ueberschreiben(pfad, ziel):
                        aktion = "verschoben"
                    else:
                        hinweis = (hinweis + "; " if hinweis else "") + \
                            "anderes Laufwerk – kopiert, Original bleibt erhalten"
                if aktion is None:
                    _kopiere_ohne_ueberschreiben(pfad, ziel)
                    aktion = "kopiert"
                self.zaehler[aktion] += 1

            self.melde(f"→ {aktion}: {pfad.name} -> {ziel.relative_to(self.ziel)}"
                       f"  [{datumsquelle}]" + (f"  ({hinweis})" if hinweis else ""))
            self._protokolliere(aktion, pfad, ziel, datum, datumsquelle, hinweis)
        except Exception as fehler:  # einzelne Fehler dürfen den Lauf nicht stoppen
            self.zaehler["fehler"] += 1
            self.melde(f"! FEHLER bei {pfad}: {fehler}")
            self._protokolliere("Fehler", pfad, None, datum, datumsquelle, str(fehler))

    def _protokoll_schreiben(self):
        ordner = self.ziel / PROTOKOLL_ORDNER
        ordner.mkdir(exist_ok=True)
        name = dt.datetime.now().strftime("Protokoll_%Y-%m-%d_%H-%M-%S")
        pfad = ordner / f"{name}.csv"
        i = 1
        while pfad.exists():
            pfad = ordner / f"{name}_{i}.csv"
            i += 1
        with open(pfad, "x", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(self.protokoll[0].keys()), delimiter=";")
            w.writeheader()
            w.writerows(self.protokoll)
        self.melde(f"Protokoll gespeichert: {pfad}")


def zusammenfassung(z, vorschau, verschieben):
    zeilen = []
    if vorschau:
        zeilen.append("Vorschau abgeschlossen – es wurde nichts verändert.")
    else:
        zeilen.append("Fertig.")
        zeilen.append(f"  Kopiert:     {z['kopiert']}")
        if verschieben:
            zeilen.append(f"  Verschoben:  {z['verschoben']}")
    zeilen.append(f"  Übersprungen (schon im Ziel vorhanden): {z['duplikat']}")
    if z["datum_geschaetzt"]:
        zeilen.append(f"  Davon ohne Aufnahmedatum, nach Änderungsdatum einsortiert: {z['datum_geschaetzt']}")
    if z["ohne_datum"]:
        zeilen.append(f"  Ohne Datum -> Ordner '{OHNE_DATUM_ORDNER}': {z['ohne_datum']}")
    if z["fehler"]:
        zeilen.append(f"  FEHLER: {z['fehler']} (Details im Protokoll/Log)")
    zeilen.append("  Gelöscht: 0 (dieses Programm löscht niemals Dateien)")
    return "\n".join(zeilen)


# ---------------------------------------------------------------------------
# Standardordner & Einstellungen
# ---------------------------------------------------------------------------

def standard_bilder_ordner():
    """Ordner, in den iPhone-Importe standardmäßig landen ('Bilder'/'Pictures')."""
    if os.name == "nt":
        try:
            import ctypes
            import uuid

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                            ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

            guid = GUID.from_buffer_copy(uuid.UUID("{33E28130-4E1E-4676-835A-98395C3BC3BB}").bytes_le)
            ergebnis = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                                          ctypes.byref(ergebnis)) == 0:
                pfad = Path(ergebnis.value)
                ctypes.windll.ole32.CoTaskMemFree(ergebnis)
                if pfad.is_dir():
                    return pfad
        except Exception:
            pass
    for name in ("Pictures", "Bilder"):
        p = Path.home() / name
        if p.is_dir():
            return p
    return Path.home()


def lade_einstellungen():
    try:
        return json.loads(EINSTELLUNGEN_DATEI.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def speichere_einstellungen(daten):
    try:
        EINSTELLUNGEN_DATEI.write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Grafische Oberfläche
# ---------------------------------------------------------------------------

def starte_gui():
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    einstellungen = lade_einstellungen()
    fenster = tk.Tk()
    fenster.title("Foto- & Video-Sortierer")
    fenster.geometry("860x600")
    fenster.minsize(640, 420)

    quelle_var = tk.StringVar(value=einstellungen.get("quelle") or str(standard_bilder_ordner()))
    ziel_var = tk.StringVar(value=einstellungen.get("ziel", ""))
    modus_var = tk.StringVar(value="kopieren")
    ohne_datum_var = tk.BooleanVar(value=einstellungen.get("ohne_datum_separat", False))
    nachrichten = queue.Queue()
    laeuft = {"aktiv": False, "abbruch": False}

    rahmen = ttk.Frame(fenster, padding=12)
    rahmen.pack(fill="both", expand=True)
    rahmen.columnconfigure(1, weight=1)

    def ordnerwahl(var, titel):
        p = filedialog.askdirectory(title=titel, initialdir=var.get() or str(Path.home()))
        if p:
            var.set(p)

    ttk.Label(rahmen, text="Quellordner (iPhone-Import):").grid(row=0, column=0, sticky="w")
    ttk.Entry(rahmen, textvariable=quelle_var).grid(row=0, column=1, sticky="ew", padx=6)
    ttk.Button(rahmen, text="Auswählen …",
               command=lambda: ordnerwahl(quelle_var, "Quellordner wählen")).grid(row=0, column=2)
    ttk.Label(rahmen, text="Zielordner (Jahr/Monat):").grid(row=1, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(rahmen, textvariable=ziel_var).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
    ttk.Button(rahmen, text="Auswählen …",
               command=lambda: ordnerwahl(ziel_var, "Zielordner wählen")).grid(row=1, column=2, pady=(6, 0))

    optionen = ttk.LabelFrame(rahmen, text="Optionen", padding=8)
    optionen.grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)
    ttk.Radiobutton(optionen, text="Kopieren – Originale bleiben im Quellordner (empfohlen)",
                    variable=modus_var, value="kopieren").pack(anchor="w")
    ttk.Radiobutton(optionen, text="Verschieben – nur auf demselben Laufwerk, sonst wird kopiert",
                    variable=modus_var, value="verschieben").pack(anchor="w")
    ttk.Checkbutton(optionen, text=f"Dateien ohne Aufnahmedatum in '{OHNE_DATUM_ORDNER}' ablegen "
                                   "(statt nach Änderungsdatum einzusortieren)",
                    variable=ohne_datum_var).pack(anchor="w", pady=(4, 0))
    ttk.Label(optionen, text="Es werden niemals Dateien gelöscht oder überschrieben.",
              foreground="#1a7f37").pack(anchor="w", pady=(4, 0))

    knoepfe = ttk.Frame(rahmen)
    knoepfe.grid(row=3, column=0, columnspan=3, sticky="ew")
    vorschau_knopf = ttk.Button(knoepfe, text="Vorschau (ändert nichts)")
    start_knopf = ttk.Button(knoepfe, text="Sortieren starten")
    abbruch_knopf = ttk.Button(knoepfe, text="Abbrechen", state="disabled")
    vorschau_knopf.pack(side="left")
    start_knopf.pack(side="left", padx=6)
    abbruch_knopf.pack(side="left")
    balken = ttk.Progressbar(knoepfe, mode="determinate")
    balken.pack(side="left", fill="x", expand=True, padx=(12, 0))

    log = ScrolledText(rahmen, height=18, wrap="none", font=("Consolas", 9))
    log.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(10, 0))
    rahmen.rowconfigure(4, weight=1)

    def schreibe(text):
        log.insert("end", text + "\n")
        log.see("end")

    def verarbeite_queue():
        try:
            while True:
                art, wert = nachrichten.get_nowait()
                if art == "text":
                    schreibe(wert)
                elif art == "fortschritt":
                    i, n = wert
                    balken["maximum"] = max(n, 1)
                    balken["value"] = i
                elif art == "fertig":
                    laeuft["aktiv"] = False
                    for k in (vorschau_knopf, start_knopf):
                        k.configure(state="normal")
                    abbruch_knopf.configure(state="disabled")
                    schreibe("")
                    schreibe(wert)
                    messagebox.showinfo("Foto-Sortierer", wert)
        except queue.Empty:
            pass
        fenster.after(100, verarbeite_queue)

    def starte(vorschau):
        quelle, ziel = quelle_var.get().strip(), ziel_var.get().strip()
        if not quelle or not Path(quelle).is_dir():
            messagebox.showerror("Foto-Sortierer", "Bitte einen gültigen Quellordner wählen.")
            return
        if not ziel or not Path(ziel).is_dir():
            messagebox.showerror("Foto-Sortierer", "Bitte einen gültigen Zielordner wählen.")
            return
        if Path(quelle).resolve() == Path(ziel).resolve():
            messagebox.showerror("Foto-Sortierer", "Quell- und Zielordner dürfen nicht gleich sein.")
            return
        verschieben = modus_var.get() == "verschieben"
        if not vorschau:
            text = ("Dateien werden jetzt " + ("VERSCHOBEN" if verschieben else "KOPIERT") +
                    f".\n\nVon:  {quelle}\nNach: {ziel}\n\nEs wird nichts gelöscht oder überschrieben. Fortfahren?")
            if not messagebox.askyesno("Foto-Sortierer", text):
                return
        speichere_einstellungen({"quelle": quelle, "ziel": ziel,
                                 "ohne_datum_separat": ohne_datum_var.get()})
        log.delete("1.0", "end")
        laeuft.update(aktiv=True, abbruch=False)
        for k in (vorschau_knopf, start_knopf):
            k.configure(state="disabled")
        abbruch_knopf.configure(state="normal")

        def arbeit():
            try:
                s = Sortierer(quelle, ziel, verschieben=verschieben, vorschau=vorschau,
                              ohne_datum_separat=ohne_datum_var.get(),
                              melde=lambda t: nachrichten.put(("text", t)),
                              fortschritt=lambda i, n: nachrichten.put(("fortschritt", (i, n))),
                              abbruch=lambda: laeuft["abbruch"])
                z = s.ausfuehren()
                nachrichten.put(("fertig", zusammenfassung(z, vorschau, verschieben)))
            except Exception as fehler:
                nachrichten.put(("fertig", f"Fehler: {fehler}"))

        threading.Thread(target=arbeit, daemon=True).start()

    vorschau_knopf.configure(command=lambda: starte(True))
    start_knopf.configure(command=lambda: starte(False))
    abbruch_knopf.configure(command=lambda: laeuft.update(abbruch=True))

    schreibe("1. Quellordner prüfen (Standard: Bilder-Ordner, in den iPhone-Importe landen).")
    schreibe("2. Zielordner mit den vorbereiteten Jahr/Monat-Ordnern wählen.")
    schreibe("3. Erst 'Vorschau' klicken, dann 'Sortieren starten'.")
    fenster.after(100, verarbeite_queue)
    fenster.mainloop()


# ---------------------------------------------------------------------------
# Kommandozeile
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Sortiert Fotos/Videos nach Aufnahmedatum in Jahr/Monat-Ordner. "
                    "Löscht und überschreibt niemals Dateien. Ohne Parameter startet die Oberfläche.")
    parser.add_argument("--quelle", help="Quellordner (Standard: Bilder-Ordner des Benutzers)")
    parser.add_argument("--ziel", help="Zielordner mit den Jahr/Monat-Ordnern")
    parser.add_argument("--vorschau", action="store_true", help="nur anzeigen, nichts verändern")
    parser.add_argument("--verschieben", action="store_true",
                        help="verschieben statt kopieren (nur auf demselben Laufwerk)")
    parser.add_argument("--ohne-datum-ordner", action="store_true",
                        help=f"Dateien ohne Aufnahmedatum nach '{OHNE_DATUM_ORDNER}' statt nach Änderungsdatum")
    args = parser.parse_args(argv)

    if args.ziel is None and args.quelle is None:
        starte_gui()
        return 0
    if args.ziel is None:
        parser.error("--ziel fehlt")
    quelle = args.quelle or str(standard_bilder_ordner())
    if Path(quelle).resolve() == Path(args.ziel).resolve():
        parser.error("Quell- und Zielordner dürfen nicht gleich sein")

    s = Sortierer(quelle, args.ziel, verschieben=args.verschieben, vorschau=args.vorschau,
                  ohne_datum_separat=args.ohne_datum_ordner)
    try:
        z = s.ausfuehren()
    except FileNotFoundError as fehler:
        print(fehler, file=sys.stderr)
        return 2
    print()
    print(zusammenfassung(z, args.vorschau, args.verschieben))
    return 1 if z["fehler"] else 0


if __name__ == "__main__":
    sys.exit(main())

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


def _datum_text(datum, datumsquelle):
    """'03.05.2022 10:31 – Aufnahmedatum (Metadaten)' für Log und Vorschau."""
    if datum is None:
        return datumsquelle
    return f"{datum:%d.%m.%Y %H:%M} – {datumsquelle}"


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
                self.melde(f"= übersprungen (bereits vorhanden): {pfad.name} -> {duplikat}"
                           f"  [{_datum_text(datum, datumsquelle)}]")
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
                       f"  [{_datum_text(datum, datumsquelle)}]" + (f"  ({hinweis})" if hinweis else ""))
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

FARBEN = {
    "seite": "#0F172A",        # dunkle Seitenleiste (passt zum silbernen Lando-Logo)
    "seite_text": "#F8FAFC",
    "seite_leise": "#94A3B8",
    "seite_linie": "#1E293B",
    "hintergrund": "#F1F5F9",
    "karte": "#FFFFFF",
    "rand": "#E2E8F0",
    "text": "#0F172A",
    "leise": "#64748B",
    "akzent": "#2563EB",
    "akzent_hover": "#1D4ED8",
    "akzent_hell": "#EFF6FF",
    "neutral_hover": "#E2E8F0",
    "gruen": "#16A34A",
    "gruen_hell": "#DCFCE7",
    "rot": "#DC2626",
    "gelb": "#B45309",
}


def _schriften():
    if sys.platform == "darwin":
        familie, mono = "Helvetica Neue", "Menlo"
    elif os.name == "nt":
        familie, mono = "Segoe UI", "Consolas"
    else:
        familie, mono = "DejaVu Sans", "DejaVu Sans Mono"
    return {
        "titel": (familie, 17, "bold"),
        "ueberschrift": (familie, 12, "bold"),
        "normal": (familie, 10),
        "fett": (familie, 10, "bold"),
        "klein": (familie, 9),
        "zahl": (familie, 18, "bold"),
        "mono": (mono, 9),
    }


def starte_gui():
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    if os.name == "nt":  # scharfe Darstellung auf hochauflösenden Bildschirmen
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    F = FARBEN
    S = _schriften()
    einstellungen = lade_einstellungen()

    fenster = tk.Tk()
    fenster.title("Foto-Sortierer – Lando System")
    fenster.configure(bg=F["hintergrund"])

    # Windows-Skalierung (125 %, 150 % …): Schriften wachsen automatisch mit,
    # feste Pixelmaße müssen wir selbst umrechnen.
    skalierung = max(1.0, float(fenster.tk.call("tk", "scaling")) * 72 / 96)  # 1.0 = 100 %

    def px(wert):
        return int(round(wert * skalierung))

    bildschirm_b, bildschirm_h = fenster.winfo_screenwidth(), fenster.winfo_screenheight()
    fenster.geometry(f"{min(px(1040), int(bildschirm_b * 0.92))}x{min(px(780), int(bildschirm_h * 0.85))}")
    fenster.minsize(min(px(900), int(bildschirm_b * 0.8)), min(px(620), int(bildschirm_h * 0.7)))

    logo_gross = logo_icon = None
    try:
        logo_gross = tk.PhotoImage(data=LANDO_LOGO_GROSS)  # 200 x 313 Pixel
        if skalierung < 1.25:
            logo_gross = logo_gross.subsample(2)
        elif skalierung < 1.75:
            logo_gross = logo_gross.zoom(3).subsample(4)
        logo_icon = tk.PhotoImage(data=LANDO_LOGO_ICON)
        fenster.iconphoto(True, logo_icon)
    except tk.TclError:
        pass  # sehr alte Tk-Versionen ohne PNG-Unterstützung

    stil = ttk.Style(fenster)
    stil.theme_use("clam")
    stil.configure("Lando.Horizontal.TProgressbar", troughcolor=F["rand"], background=F["akzent"],
                   bordercolor=F["rand"], lightcolor=F["akzent"], darkcolor=F["akzent"], thickness=8)
    stil.configure("Lando.Vertical.TScrollbar", background=F["rand"], troughcolor=F["karte"],
                   bordercolor=F["karte"], arrowcolor=F["leise"], lightcolor=F["rand"], darkcolor=F["rand"])

    quelle_var = tk.StringVar(value=einstellungen.get("quelle") or str(standard_bilder_ordner()))
    ziel_var = tk.StringVar(value=einstellungen.get("ziel", ""))
    modus_var = tk.StringVar(value="kopieren")
    ohne_datum_var = tk.BooleanVar(value=einstellungen.get("ohne_datum_separat", False))
    status_var = tk.StringVar(value="Bereit. Prüfe die Ordner und starte mit der Vorschau.")
    nachrichten = queue.Queue()
    laeuft = {"aktiv": False, "abbruch": False}

    # --- kleine Bausteine -------------------------------------------------

    class Knopf(tk.Label):
        """Flacher Knopf, der auf Windows, macOS und Linux gleich aussieht."""
        ARTEN = {
            "primaer": (F["akzent"], F["akzent_hover"], "#FFFFFF"),
            "sekundaer": (F["akzent_hell"], "#DBEAFE", F["akzent"]),
            "neutral": (F["hintergrund"], F["neutral_hover"], F["text"]),
            "leise": (F["karte"], F["hintergrund"], F["leise"]),
        }

        def __init__(self, master, text, befehl, art="primaer", **kw):
            self.farben = self.ARTEN[art]
            super().__init__(master, text=text, bg=self.farben[0], fg=self.farben[2],
                             font=S["fett"], padx=18, pady=9, cursor="hand2", **kw)
            self.befehl = befehl
            self.aktiv = True
            self.bind("<Enter>", lambda e: self.aktiv and self.configure(bg=self.farben[1]))
            self.bind("<Leave>", lambda e: self.configure(bg=self.farben[0]))
            self.bind("<Button-1>", lambda e: self.aktiv and self.befehl())

        def schalten(self, aktiv):
            self.aktiv = aktiv
            self.configure(fg=self.farben[2] if aktiv else "#94A3B8",
                           bg=self.farben[0], cursor="hand2" if aktiv else "arrow")

    def karte(master, titel=None, untertitel=None):
        rahmen = tk.Frame(master, bg=F["karte"], highlightbackground=F["rand"], highlightthickness=1)
        innen = tk.Frame(rahmen, bg=F["karte"])
        innen.pack(fill="both", expand=True, padx=20, pady=16)
        if titel:
            tk.Label(innen, text=titel, font=S["ueberschrift"], bg=F["karte"], fg=F["text"],
                     anchor="w").pack(fill="x")
        if untertitel:
            tk.Label(innen, text=untertitel, font=S["klein"], bg=F["karte"], fg=F["leise"],
                     anchor="w").pack(fill="x", pady=(2, 0))
        return rahmen, innen

    # --- Seitenleiste mit Lando-Logo --------------------------------------

    seite = tk.Frame(fenster, bg=F["seite"])
    seite.pack(side="left", fill="y")
    # Mindestbreite; wird der Inhalt breiter (große Schrift), wächst die Leiste mit
    tk.Frame(seite, bg=F["seite"], width=px(260), height=0).pack()

    if logo_gross is not None:
        tk.Label(seite, image=logo_gross, bg=F["seite"]).pack(pady=(34, 10))
    tk.Label(seite, text="Foto-Sortierer", font=S["titel"], bg=F["seite"],
             fg=F["seite_text"]).pack(pady=(8, 2))
    tk.Label(seite, text="Fotos & Videos nach Jahr/Monat", font=S["klein"], bg=F["seite"],
             fg=F["seite_leise"]).pack()

    tk.Frame(seite, bg=F["seite_linie"], height=1).pack(fill="x", padx=px(28), pady=px(26))

    schritte = tk.Frame(seite, bg=F["seite"])
    schritte.pack(fill="x", padx=px(28))
    for nr, text in enumerate(("Ordner prüfen", "Vorschau ansehen", "Sortieren starten"), 1):
        zeile = tk.Frame(schritte, bg=F["seite"])
        zeile.pack(fill="x", pady=5)
        d = px(24)
        kreis = tk.Canvas(zeile, width=d, height=d, bg=F["seite"], highlightthickness=0)
        kreis.create_oval(1, 1, d - 1, d - 1, outline=F["seite_leise"], width=1)
        kreis.create_text(d / 2, d / 2, text=str(nr), fill=F["seite_text"], font=S["klein"])
        kreis.pack(side="left")
        tk.Label(zeile, text=text, font=S["normal"], bg=F["seite"], fg=F["seite_text"]).pack(side="left", padx=10)

    hinweis = tk.Frame(seite, bg=F["seite"])
    hinweis.pack(side="bottom", fill="x", padx=px(28), pady=px(28))
    tk.Label(hinweis, text="✓ Löscht niemals Dateien", font=S["fett"], bg=F["seite"],
             fg="#4ADE80", anchor="w").pack(fill="x")
    tk.Label(hinweis, text="Nichts wird überschrieben.\nJeder Lauf wird protokolliert.",
             font=S["klein"], bg=F["seite"], fg=F["seite_leise"], justify="left",
             anchor="w").pack(fill="x", pady=(4, 0))

    # --- Inhalt -----------------------------------------------------------

    inhalt = tk.Frame(fenster, bg=F["hintergrund"])
    inhalt.pack(side="left", fill="both", expand=True, padx=px(28), pady=px(24))

    # Ordner
    ordner_karte, ordner_innen = karte(inhalt, "Ordner")
    ordner_karte.pack(fill="x")

    def ordnerwahl(var, titel):
        p = filedialog.askdirectory(title=titel, initialdir=var.get() or str(Path.home()))
        if p:
            var.set(str(Path(p)))

    def ordnerzeile(beschriftung, hilfe, var, titel):
        zeile = tk.Frame(ordner_innen, bg=F["karte"])
        zeile.pack(fill="x", pady=(12, 0))
        kopf = tk.Frame(zeile, bg=F["karte"])
        kopf.pack(fill="x")
        tk.Label(kopf, text=beschriftung, font=S["fett"], bg=F["karte"], fg=F["text"]).pack(side="left")
        tk.Label(kopf, text=hilfe, font=S["klein"], bg=F["karte"], fg=F["leise"]).pack(side="left", padx=8)
        feld = tk.Frame(zeile, bg=F["karte"])
        feld.pack(fill="x", pady=(5, 0))
        eingabe_rahmen = tk.Frame(feld, bg=F["hintergrund"], highlightbackground=F["rand"],
                                  highlightthickness=1)
        eingabe_rahmen.pack(side="left", fill="x", expand=True)
        tk.Entry(eingabe_rahmen, textvariable=var, font=S["normal"], relief="flat", bg=F["hintergrund"],
                 fg=F["text"], insertbackground=F["text"]).pack(fill="x", padx=10, pady=8)
        Knopf(feld, "Ändern …", lambda: ordnerwahl(var, titel), art="neutral").pack(side="left", padx=(10, 0))

    ordnerzeile("Quelle", "iPhone-Import, Unterordner werden mit durchsucht", quelle_var, "Quellordner wählen")
    ordnerzeile("Ziel", "Ordner mit den vorbereiteten Jahr/Monat-Ordnern", ziel_var, "Zielordner wählen")

    # Optionen
    optionen_karte, optionen_innen = karte(inhalt, "Vorgehen")
    optionen_karte.pack(fill="x", pady=(16, 0))

    umschalter = tk.Frame(optionen_innen, bg=F["hintergrund"], highlightbackground=F["rand"],
                          highlightthickness=1)
    umschalter.pack(anchor="w", pady=(12, 0))
    modus_knoepfe = {}
    modus_texte = {
        "kopieren": "Originale bleiben im Quellordner (empfohlen).",
        "verschieben": "Dateien werden verschoben – nur auf demselben Laufwerk, sonst wird kopiert.",
    }
    modus_hilfe = tk.Label(optionen_innen, font=S["klein"], bg=F["karte"], fg=F["leise"], anchor="w")

    def modus_setzen(modus):
        if laeuft["aktiv"]:
            return
        modus_var.set(modus)
        for name, k in modus_knoepfe.items():
            gewaehlt = name == modus
            k.configure(bg=F["karte"] if gewaehlt else F["hintergrund"],
                        fg=F["text"] if gewaehlt else F["leise"])
        modus_hilfe.configure(text=modus_texte[modus])

    for name, text in (("kopieren", "Kopieren"), ("verschieben", "Verschieben")):
        k = tk.Label(umschalter, text=text, font=S["fett"], padx=22, pady=7, cursor="hand2")
        k.pack(side="left", padx=3, pady=3)
        k.bind("<Button-1>", lambda e, n=name: modus_setzen(n))
        modus_knoepfe[name] = k
    modus_hilfe.pack(fill="x", pady=(6, 0))
    modus_setzen("kopieren")

    # eigenes Kästchen: das Standard-Häkchen von Tk wächst unter Windows nicht mit der Skalierung
    haken_zeile = tk.Frame(optionen_innen, bg=F["karte"], cursor="hand2")
    haken_zeile.pack(fill="x", pady=(14, 0))
    k = px(18)
    haken = tk.Canvas(haken_zeile, width=k, height=k, bg=F["karte"], highlightthickness=0, cursor="hand2")
    haken.pack(side="left")
    haken_text = tk.Label(haken_zeile, text="Dateien ohne Aufnahmedatum separat ablegen", font=S["normal"],
                          bg=F["karte"], fg=F["text"], cursor="hand2")
    haken_text.pack(side="left", padx=(px(10), 0))

    def haken_zeichnen():
        haken.delete("all")
        if ohne_datum_var.get():
            haken.create_rectangle(1, 1, k - 1, k - 1, fill=F["akzent"], outline=F["akzent"])
            haken.create_line(k * 0.25, k * 0.52, k * 0.43, k * 0.70, k * 0.76, k * 0.32,
                              fill="#FFFFFF", width=max(2, px(2)), capstyle="round", joinstyle="round")
        else:
            haken.create_rectangle(1, 1, k - 1, k - 1, fill=F["karte"], outline="#94A3B8")

    def haken_umschalten(_=None):
        if not laeuft["aktiv"]:
            ohne_datum_var.set(not ohne_datum_var.get())
            haken_zeichnen()

    for w in (haken_zeile, haken, haken_text):
        w.bind("<Button-1>", haken_umschalten)
    haken_zeichnen()
    tk.Label(optionen_innen, text=f"Sie landen dann in „{OHNE_DATUM_ORDNER}“, statt nach dem Änderungsdatum "
                                  "der Datei einsortiert zu werden.",
             font=S["klein"], bg=F["karte"], fg=F["leise"], anchor="w").pack(fill="x", padx=(k + px(10), 0))

    # Aktionen
    aktionen = tk.Frame(inhalt, bg=F["hintergrund"])
    aktionen.pack(fill="x", pady=(18, 0))
    start_knopf = Knopf(aktionen, "Sortieren starten", lambda: starte(False), art="primaer")
    vorschau_knopf = Knopf(aktionen, "Vorschau", lambda: starte(True), art="sekundaer")
    abbruch_knopf = Knopf(aktionen, "Abbrechen", lambda: laeuft.update(abbruch=True), art="neutral")
    start_knopf.pack(side="right")
    vorschau_knopf.pack(side="right", padx=(0, 10))
    tk.Label(aktionen, textvariable=status_var, font=S["normal"], bg=F["hintergrund"], fg=F["leise"],
             anchor="w").pack(side="left", fill="x", expand=True)

    # Fortschritt + Protokoll
    verlauf_karte, verlauf_innen = karte(inhalt)
    verlauf_karte.pack(fill="both", expand=True, pady=(18, 0))

    kennzahlen = tk.Frame(verlauf_innen, bg=F["karte"])
    kennzahlen.pack(fill="x")
    zahl_labels = {}
    for schluessel, text, farbe in (("bearbeitet", "Dateien", F["text"]),
                                    ("abgelegt", "Abgelegt", F["gruen"]),
                                    ("duplikat", "Schon vorhanden", F["leise"]),
                                    ("fehler", "Fehler", F["rot"])):
        box = tk.Frame(kennzahlen, bg=F["karte"])
        box.pack(side="left", padx=(0, 36))
        zahl_labels[schluessel] = tk.Label(box, text="–", font=S["zahl"], bg=F["karte"], fg=farbe)
        zahl_labels[schluessel].pack(anchor="w")
        tk.Label(box, text=text, font=S["klein"], bg=F["karte"], fg=F["leise"]).pack(anchor="w")

    balken = ttk.Progressbar(verlauf_innen, mode="determinate", style="Lando.Horizontal.TProgressbar")
    balken.pack(fill="x", pady=(14, 12))

    log_rahmen = tk.Frame(verlauf_innen, bg=F["karte"])
    log_rahmen.pack(fill="both", expand=True)
    log = tk.Text(log_rahmen, height=6, wrap="none", font=S["mono"], relief="flat", bg=F["karte"],
                  fg=F["text"], padx=0, pady=0, highlightthickness=0, spacing1=2, spacing3=2)
    scroll = ttk.Scrollbar(log_rahmen, orient="vertical", command=log.yview, style="Lando.Vertical.TScrollbar")
    log.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    log.pack(side="left", fill="both", expand=True)
    log.tag_configure("ok", foreground=F["gruen"])
    log.tag_configure("leise", foreground=F["leise"])
    log.tag_configure("fehler", foreground=F["rot"])
    log.tag_configure("info", foreground=F["akzent"])

    def schreibe(text):
        if text.startswith("→"):
            tag = "ok"
        elif text.startswith("="):
            tag = "leise"
        elif text.startswith("!"):
            tag = "fehler"
        elif text.startswith(("VORSCHAU", "Ordner angelegt", "Folgende", "Protokoll")):
            tag = "info"
        else:
            tag = None
        log.configure(state="normal")
        log.insert("end", text + "\n", tag)
        log.see("end")
        log.configure(state="disabled")

    def zahlen_setzen(z, i):
        zahl_labels["bearbeitet"].configure(text=str(i))
        zahl_labels["abgelegt"].configure(text=str(z.get("kopiert", 0) + z.get("verschoben", 0)
                                                   + z.get("geplant", 0)))
        zahl_labels["duplikat"].configure(text=str(z.get("duplikat", 0)))
        zahl_labels["fehler"].configure(text=str(z.get("fehler", 0)))

    def bedienung_sperren(sperren):
        laeuft["aktiv"] = sperren
        start_knopf.schalten(not sperren)
        vorschau_knopf.schalten(not sperren)
        if sperren:  # Abbrechen nur zeigen, solange etwas läuft
            abbruch_knopf.pack(side="right", padx=(0, 10), before=vorschau_knopf)
        else:
            abbruch_knopf.pack_forget()

    def verarbeite_queue():
        try:
            while True:
                art, wert = nachrichten.get_nowait()
                if art == "text":
                    schreibe(wert)
                elif art == "fortschritt":
                    i, n, z = wert
                    balken["maximum"] = max(n, 1)
                    balken["value"] = i
                    zahlen_setzen(z, i)
                    status_var.set(f"Bearbeite Datei {i} von {n} …")
                elif art == "fertig":
                    titel, text = wert
                    bedienung_sperren(False)
                    status_var.set(titel)
                    schreibe("")
                    for zeile in text.splitlines():
                        schreibe(zeile)
                    messagebox.showinfo("Foto-Sortierer", text)
        except queue.Empty:
            pass
        fenster.after(80, verarbeite_queue)

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
        log.configure(state="normal")
        log.delete("1.0", "end")
        balken["value"] = 0
        zahlen_setzen({}, 0)
        laeuft["abbruch"] = False
        bedienung_sperren(True)
        status_var.set("Vorschau läuft …" if vorschau else "Sortiere …")

        def arbeit():
            try:
                s = Sortierer(quelle, ziel, verschieben=verschieben, vorschau=vorschau,
                              ohne_datum_separat=ohne_datum_var.get(),
                              melde=lambda t: nachrichten.put(("text", t)),
                              abbruch=lambda: laeuft["abbruch"])

                def fortschritt(i, n):
                    z = dict(s.zaehler)
                    z["geplant"] = len(s.reserviert)
                    nachrichten.put(("fortschritt", (i, n, z)))

                s.fortschritt = fortschritt
                z = s.ausfuehren()
                fortschritt(len(s.protokoll), max(len(s.protokoll), 1))
                titel = "Vorschau fertig – nichts wurde verändert." if vorschau else "Fertig sortiert."
                nachrichten.put(("fertig", (titel, zusammenfassung(z, vorschau, verschieben))))
            except Exception as fehler:
                nachrichten.put(("fertig", ("Fehler", f"Fehler: {fehler}")))

        threading.Thread(target=arbeit, daemon=True).start()

    schreibe("Hier erscheint, welche Datei wohin abgelegt wird.")
    schreibe("Tipp: Erst „Vorschau“ – dabei wird nichts verändert.")
    log.tag_add("leise", "1.0", "end")
    fenster.after(80, verarbeite_queue)
    fenster.mainloop()


# ---------------------------------------------------------------------------
# Kommandozeile
# ---------------------------------------------------------------------------

def main(argv=None):
    # Als Windows-Programm ohne Konsolenfenster gibt es keine Standardausgabe
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
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


# ---------------------------------------------------------------------------
# Lando-System-Logo (eingebettet, damit das Programm eine einzige Datei bleibt)
# ---------------------------------------------------------------------------

LANDO_LOGO_GROSS = (
    "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"
    "RmNfIxDCGzhokD5r5qx7fvCDa67qzkACT6Zy1z30qqoqK5VKkcgcJRHHdLSTvLnqL558HR/85ly2+PjNAheGzElk5iLq4BTyaf30"
    "pzfdFwQyxRj1dMOkjIWh82w26x04dqy+pG7xu0uWLF5imoa2/+gx55WVl+obN24UlFIepfDQ9mw7ragYWn7LLbfddc01V30v3J/o"
    "ohBjxHfsuq589913/3jmmWf9PJtt9yilplIKuq4jm83ypqYmedjhh12cyWR+kMvlum0/K9EgXeADCCFw8TcumXr3XXfNdD0/mtBx"
    "i7Uo4VIBXOPhA4hC0EGUhxZ02NuhlBb3hCilCIQAifLIZBAgJILW4LoOlCSwCwVkcznJNUZjtkfD1J2+ffuY026ddsJ99937arz3"
    "MWDgwF633Trtp8eOH//jNWvWiLKyMp7P55FKpVDI5719991XP/74CZVLlixp7so9knhhmDBx4sg///nhxatWrfJ0Xdfj96OUIqd/"
    "/wH65d+8dPQbb8xb3F1aJNEgO/lBCyFw+eXfnvqb3/525vLlyz2lFHVcF5ZpFoVASlmk3QQAXdfh+35EXxpnGIhiEmhkHhVLAOLv"
    "xqHaQEoQgDq2K1OpNEjI9Rsmbba1iiFDB5u/uPnmE++77745HW34hvr6lssvv+wnjz722ICDDhp3YUtLi6fruu46DqSU0nFcefzx"
    "J5yxZMmSB7vSMY7vw/vz5y9bv27dMsuy9rVtW5qmSTUtXBzyBRuUEnrchAkT33hj3uLESd9No0cXXXTx1N/dccfMRYs+Lmiaxjnn"
    "3DQMLqXkSimeyWR4KpXimqZxSimnlHJfeJwywi3L5IbBOSGKV1T24oDihJAiIyOllJeVlXFKCc+kU5xzwpWSHEpyQkBLy0p4IH3u"
    "uk4sjKL/gIG85sMP5993332vxI56x54lAPCDa3/wLaVkvW7oNAgCSUNB557v0UMOOeTCUGC7brWOsypyuZxcsOD9T3uVl1ECiHDR"
    "8EAIg67penNzC4499rgf6rrebQTXiYDsRLPq0ksvm3r33b+fuXTpcqekpCRFCKXhak2K/QR9PzS5bNuGYRghQ6MfIBABPM+D74eR"
    "o2x7Ful0JmrO48G27WLxlGVaoenh+ZCBBIn+kzJMIE2nQzZVIYRIp1J45plnbgbClIKOdntc397YuMmZ+9qr6yt69QoJ65QE4xz5"
    "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"
)

LANDO_LOGO_ICON = (
    "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"
    "mfV/iONXhNCg1E0AAAAASUVORK5CYII="
)


if __name__ == "__main__":
    sys.exit(main())

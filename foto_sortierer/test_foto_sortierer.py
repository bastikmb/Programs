"""Tests für den Foto-Sortierer:  python -m unittest test_foto_sortierer.py"""

import datetime as dt
import os
import struct
import tempfile
import unittest
from pathlib import Path

import foto_sortierer as fs


def exif_tiff(datum):
    """Minimaler Big-Endian-TIFF-Block mit DateTimeOriginal."""
    text = datum.strftime("%Y:%m:%d %H:%M:%S").encode() + b"\0"   # 20 Bytes
    ifd0 = struct.pack(">H", 1) + struct.pack(">HHII", 0x8769, 4, 1, 26) + struct.pack(">I", 0)
    exif = struct.pack(">H", 1) + struct.pack(">HHII", 0x9003, 2, 20, 44) + struct.pack(">I", 0)
    return b"MM\0*" + struct.pack(">I", 8) + ifd0 + exif + text


def jpeg_mit_exif(datum, extra=b""):
    app1 = b"Exif\0\0" + exif_tiff(datum)
    return (b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", len(app1) + 2) + app1 +
            b"\xff\xda\x00\x02" + extra + b"\xff\xd9")


def box(typ, inhalt):
    return struct.pack(">I4s", 8 + len(inhalt), typ) + inhalt


def mov_mit_datum(datum_utc):
    sek = int((datum_utc - dt.datetime(1904, 1, 1, tzinfo=dt.timezone.utc)).total_seconds())
    mvhd = box(b"mvhd", b"\0\0\0\0" + struct.pack(">II", sek, sek) + b"\0" * 88)
    return box(b"ftyp", b"qt  \0\0\0\0") + box(b"mdat", b"x" * 100) + box(b"moov", mvhd)


def mov_mit_apple_datum(text):
    meta = b"com.apple.quicktime.creationdate" + b"\0" * 8 + text.encode()
    return box(b"ftyp", b"qt  \0\0\0\0") + box(b"moov", box(b"meta", meta))


def heic_mit_exif(datum):
    tiff = exif_tiff(datum)
    exif_item = struct.pack(">I", 0) + tiff
    infe = box(b"infe", bytes([2, 0, 0, 0]) + struct.pack(">HH", 1, 0) + b"Exif" + b"\0")
    iinf = box(b"iinf", b"\0\0\0\0" + struct.pack(">H", 1) + infe)

    def iloc(offset):
        return box(b"iloc", b"\0\0\0\0" + bytes([0x44, 0x00]) + struct.pack(">H", 1) +
                   struct.pack(">HHH", 1, 0, 1) + struct.pack(">II", offset, len(exif_item)))

    ftyp = box(b"ftyp", b"heic\0\0\0\0mif1heic")
    meta_len = len(box(b"meta", b"\0\0\0\0" + iinf + iloc(0)))
    offset = len(ftyp) + meta_len + 8
    meta = box(b"meta", b"\0\0\0\0" + iinf + iloc(offset))
    return ftyp + meta + box(b"mdat", exif_item)


class DatumsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def schreibe(self, name, daten):
        p = self.dir / name
        p.write_bytes(daten)
        return p

    def test_jpeg(self):
        p = self.schreibe("IMG_0001.JPG", jpeg_mit_exif(dt.datetime(2023, 7, 14, 10, 5, 0)))
        self.assertEqual(fs.ermittle_datum(p), (dt.datetime(2023, 7, 14, 10, 5, 0), "Aufnahmedatum (Metadaten)"))

    def test_heic(self):
        p = self.schreibe("IMG_0002.HEIC", heic_mit_exif(dt.datetime(2022, 12, 31, 23, 59, 0)))
        self.assertEqual(fs.ermittle_datum(p)[0], dt.datetime(2022, 12, 31, 23, 59, 0))

    def test_mov_mvhd(self):
        utc = dt.datetime(2021, 3, 5, 12, 0, 0, tzinfo=dt.timezone.utc)
        p = self.schreibe("IMG_0003.MOV", mov_mit_datum(utc))
        self.assertEqual(fs.ermittle_datum(p)[0], utc.astimezone().replace(tzinfo=None))

    def test_mov_apple_ortszeit(self):
        p = self.schreibe("IMG_0004.MOV", mov_mit_apple_datum("2020-08-01T00:30:00+0200"))
        self.assertEqual(fs.ermittle_datum(p)[0], dt.datetime(2020, 8, 1, 0, 30, 0))

    def test_dateiname(self):
        p = self.schreibe("IMG-20190204-WA0001.jpg", b"kein exif")
        self.assertEqual(fs.ermittle_datum(p), (dt.datetime(2019, 2, 4), "Dateiname"))

    def test_aenderungsdatum_und_ohne(self):
        p = self.schreibe("IMG_0005.PNG", b"nix")
        ts = dt.datetime(2018, 6, 1, 12, 0).timestamp()
        os.utime(p, (ts, ts))
        self.assertEqual(fs.ermittle_datum(p)[0].month, 6)
        self.assertIsNone(fs.ermittle_datum(p, mtime_erlaubt=False)[0])


class OrdnerTests(unittest.TestCase):
    def test_monatsnamen(self):
        for name, erwartet in [("01", 1), ("1", 1), ("03 März", 3), ("Maerz", 3), ("2024-11", 11),
                               ("12_Dezember", 12), ("Juli", 7), ("Urlaub", None), ("2024", None)]:
            self.assertEqual(fs.monat_aus_ordnername(name, 2024), erwartet, name)

    def test_vorlage(self):
        self.assertEqual(fs.ordnername_nach_vorlage("01 - Januar", 2024, 3), "03 - März")
        self.assertEqual(fs.ordnername_nach_vorlage("2023-01", 2024, 5), "2024-05")
        self.assertEqual(fs.ordnername_nach_vorlage("1", 2024, 10), "10")
        self.assertEqual(fs.ordnername_nach_vorlage("Jan", 2024, 3), "Mär")

    def test_struktur(self):
        with tempfile.TemporaryDirectory() as t:
            ziel = Path(t)
            (ziel / "2024" / "01 Januar").mkdir(parents=True)
            (ziel / "2024" / "07 Juli").mkdir(parents=True)
            s = fs.ZielStruktur(ziel)
            self.assertEqual(s.ordner_fuer(dt.datetime(2024, 7, 3)), ziel / "2024" / "07 Juli")
            self.assertEqual(s.ordner_fuer(dt.datetime(2024, 2, 3)), ziel / "2024" / "02 Februar")
            self.assertEqual(s.ordner_fuer(dt.datetime(2025, 2, 3)), ziel / "2025" / "02 Februar")
            self.assertFalse((ziel / "2025").exists())  # nur geplant, nicht angelegt


class SortierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        basis = Path(self.tmp.name)
        self.quelle = basis / "Bilder"
        self.ziel = basis / "Archiv"
        (self.quelle / "100APPLE").mkdir(parents=True)
        for m in range(1, 13):
            (self.ziel / "2023" / f"{m:02d}").mkdir(parents=True)
        self.a = self.quelle / "100APPLE" / "IMG_0001.JPG"
        self.a.write_bytes(jpeg_mit_exif(dt.datetime(2023, 7, 14, 10, 0)))
        self.aae = self.quelle / "100APPLE" / "IMG_O0001.AAE"
        self.aae.write_bytes(b"<plist/>")
        self.b = self.quelle / "100APPLE" / "IMG_0002.HEIC"
        self.b.write_bytes(heic_mit_exif(dt.datetime(2023, 12, 24, 18, 0)))

    def tearDown(self):
        self.tmp.cleanup()

    def alle_quelldateien(self):
        return sorted(p for p in self.quelle.rglob("*") if p.is_file())

    def lauf(self, **kw):
        return fs.Sortierer(self.quelle, self.ziel, melde=lambda t: None, **kw).ausfuehren()

    def test_vorschau_aendert_nichts(self):
        vorher = self.alle_quelldateien()
        z = self.lauf(vorschau=True)
        self.assertEqual(self.alle_quelldateien(), vorher)
        self.assertEqual(list((self.ziel / "2023" / "07").iterdir()), [])
        self.assertEqual(z["fehler"], 0)

    def test_kopieren(self):
        vorher = {p: p.read_bytes() for p in self.alle_quelldateien()}
        z = self.lauf()
        self.assertEqual(z["kopiert"], 3)
        self.assertTrue((self.ziel / "2023" / "07" / "IMG_0001.JPG").exists())
        self.assertTrue((self.ziel / "2023" / "07" / "IMG_O0001.AAE").exists())
        self.assertTrue((self.ziel / "2023" / "12" / "IMG_0002.HEIC").exists())
        self.assertEqual({p: p.read_bytes() for p in self.alle_quelldateien()}, vorher)
        self.assertEqual(len(list((self.ziel / fs.PROTOKOLL_ORDNER).iterdir())), 1)

        # zweiter Lauf: alles schon vorhanden -> nur übersprungen
        z2 = self.lauf()
        self.assertEqual((z2["kopiert"], z2["duplikat"]), (0, 3))

    def test_nie_ueberschreiben(self):
        vorhanden = self.ziel / "2023" / "07" / "IMG_0001.JPG"
        vorhanden.write_bytes(b"anderes Foto mit gleichem Namen")
        self.lauf()
        self.assertEqual(vorhanden.read_bytes(), b"anderes Foto mit gleichem Namen")
        self.assertEqual((self.ziel / "2023" / "07" / "IMG_0001 (1).JPG").read_bytes(), self.a.read_bytes())

    def test_verschieben(self):
        inhalt = self.a.read_bytes()
        z = self.lauf(verschieben=True)
        self.assertEqual(z["verschoben"], 3)
        self.assertFalse(self.a.exists())
        self.assertEqual((self.ziel / "2023" / "07" / "IMG_0001.JPG").read_bytes(), inhalt)

    def test_ziel_im_quellordner_wird_nicht_durchsucht(self):
        ziel = self.quelle / "Sortiert"
        (ziel / "2023" / "07").mkdir(parents=True)
        z = fs.Sortierer(self.quelle, ziel, melde=lambda t: None).ausfuehren()
        self.assertEqual(z["kopiert"], 3)
        z2 = fs.Sortierer(self.quelle, ziel, melde=lambda t: None).ausfuehren()
        self.assertEqual((z2["kopiert"], z2["duplikat"]), (0, 3))

    def test_iphone_monatsordner_mit_gemischten_daten(self):
        # iPhone-Ordner "202205__" mit Fotos aus verschiedenen Monaten/Jahren,
        # Ziel im Stil "2022/05_Mai" -> jedes Foto in seinen eigenen Monat
        quelle = self.quelle / "202205__"
        quelle.mkdir()
        ziel = Path(self.tmp.name) / "02_Bilder"
        for jahr in (2021, 2022, 2023):
            for m, name in enumerate(fs.MONATE_DE, 1):
                (ziel / str(jahr) / f"{m:02d}_{name}").mkdir(parents=True)
        daten = [dt.datetime(2022, 5, 3), dt.datetime(2022, 8, 28), dt.datetime(2021, 12, 24),
                 dt.datetime(2023, 1, 2)]
        for i, d in enumerate(daten):
            p = quelle / f"IMG_{3484 + i}.JPG"
            p.write_bytes(jpeg_mit_exif(d, bytes([i])))
            ts = dt.datetime(2022, 8, 28, 3, 37).timestamp()
            os.utime(p, (ts, ts))  # gleiches Änderungsdatum wie nach dem Kopieren vom iPhone
        fs.Sortierer(quelle, ziel, melde=lambda t: None).ausfuehren()
        for i, erwartet in enumerate(["2022/05_Mai", "2022/08_August", "2021/12_Dezember", "2023/01_Januar"]):
            self.assertTrue((ziel / erwartet / f"IMG_{3484 + i}.JPG").exists(), erwartet)

    def test_leere_ordner_entfernen(self):
        # 100APPLE wird komplett geleert -> entfernt; 101APPLE enthält noch eine
        # fremde Datei -> bleibt samt Datei; der Quellordner selbst bleibt immer
        (self.quelle / "101APPLE").mkdir()
        c = self.quelle / "101APPLE" / "IMG_0003.JPG"
        c.write_bytes(jpeg_mit_exif(dt.datetime(2023, 3, 1), b"c"))
        fremd = self.quelle / "101APPLE" / "notiz.txt"
        fremd.write_text("bleibt")
        (self.quelle / "leer_von_anfang_an").mkdir()

        vorschau = self.lauf(verschieben=True, vorschau=True, leere_ordner_entfernen=True)
        self.assertEqual(vorschau["ordner_entfernt"], 1)
        self.assertTrue((self.quelle / "100APPLE").is_dir())  # Vorschau ändert nichts

        z = self.lauf(verschieben=True, leere_ordner_entfernen=True)
        self.assertEqual(z["ordner_entfernt"], 1)
        self.assertFalse((self.quelle / "100APPLE").exists())
        self.assertEqual(fremd.read_text(), "bleibt")
        self.assertTrue((self.quelle / "leer_von_anfang_an").is_dir())  # nicht von uns geleert
        self.assertTrue(self.quelle.is_dir())
        self.assertTrue((self.ziel / "2023" / "03" / "IMG_0003.JPG").exists())

    def test_leere_ordner_nur_beim_verschieben(self):
        z = self.lauf(leere_ordner_entfernen=True)  # Kopieren
        self.assertEqual(z["ordner_entfernt"], 0)
        self.assertTrue((self.quelle / "100APPLE").is_dir())

    def test_duplikat_haelt_ordner_am_leben(self):
        # identische Datei liegt schon im Ziel -> bleibt in der Quelle -> Ordner bleibt
        (self.ziel / "2023" / "07" / "IMG_0001.JPG").write_bytes(self.a.read_bytes())
        self.lauf(verschieben=True, leere_ordner_entfernen=True)
        self.assertTrue(self.a.exists())
        self.assertTrue((self.quelle / "100APPLE").is_dir())


if __name__ == "__main__":
    unittest.main()

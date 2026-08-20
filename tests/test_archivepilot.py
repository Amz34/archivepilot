"""Tests: Arabic normalization, FTS search, importers."""

import tempfile
import unittest
from pathlib import Path

from archivepilot.arabic import has_arabic, normalize
from archivepilot.db import Archive
from archivepilot.importers import import_csv, import_text, import_whatsapp


class ArabicTests(unittest.TestCase):
    def test_diacritics_removed(self):
        self.assertEqual(normalize("مُحَمَّد"), "محمد")

    def test_alef_variants(self):
        self.assertEqual(normalize("أحمد إبراهيم آدم"), "احمد ابراهيم ادم")

    def test_hamza_and_tail(self):
        self.assertEqual(normalize("مسؤولة مبنى"), "مسووله مبني")
        self.assertEqual(normalize("مؤمن"), "مومن")

    def test_teh_marbuta(self):
        self.assertEqual(normalize("شركة"), "شركه")

    def test_definite_article_stripped(self):
        self.assertEqual(normalize("الموقع"), "موقع")

    def test_definite_article_keeps_allah(self):
        self.assertEqual(normalize("الله"), "الله")

    def test_detect_arabic(self):
        self.assertTrue(has_arabic("مرحبا بالعالم"))
        self.assertFalse(has_arabic("hello world"))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.archive = Archive(str(Path(self.tmp) / "test.db"))

    def tearDown(self):
        self.archive.close()

    def test_add_and_search_english(self):
        self.archive.add("test", "The quick brown fox jumps over the lazy dog")
        self.archive.add("test", "Python packaging is delightful")
        hits = self.archive.search("quick fox")
        self.assertEqual(len(hits), 1)
        self.assertIn("brown fox", hits[0]["raw"])

    def test_arabic_search_normalized(self):
        self.archive.add("chat", "سعر صمام التهوية ٤٥٠ ريال")
        hits = self.archive.search("صمام")
        self.assertEqual(len(hits), 1)

    def test_arabic_search_with_diacritics(self):
        self.archive.add("chat", "الموقع الجديد جاهز للانطلاق")
        hits = self.archive.search("الموقع")
        self.assertEqual(len(hits), 1)

    def test_stats(self):
        self.archive.add("a", "one")
        self.archive.add("b", "two")
        self.archive.add("b", "three")
        s = self.archive.stats()
        self.assertEqual(s["items"], 3)
        self.assertEqual(s["sources"]["b"], 2)


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.archive = Archive(str(self.tmp / "test.db"))

    def tearDown(self):
        self.archive.close()

    def test_whatsapp_import(self):
        chat = self.tmp / "chat.txt"
        chat.write_text(
            "[20/08/2026, 10:15:22] Ali: Bhaijaan meeting 5 baje\n"
            "[20/08/2026, 10:16:01] Aamir: Ok, link bhej dena\n"
            "media omitted\n"
            "[21/08/2026, 9:00:00] Ali: Kal ka agenda yaad rahega\n",
            encoding="utf-8",
        )
        n = import_whatsapp(chat, self.archive)
        self.assertEqual(n, 3)  # media omitted line skipped
        hits = self.archive.search("meeting")
        self.assertEqual(len(hits), 1)

    def test_csv_import(self):
        csvf = self.tmp / "leads.csv"
        csvf.write_text(
            "name,company,interest\n"
            "Ahmed,AVVS,air valves\n"
            "Sara,Alfa,security audit\n",
            encoding="utf-8",
        )
        n = import_csv(csvf, self.archive)
        self.assertEqual(n, 2)
        hits = self.archive.search("security audit")
        self.assertEqual(len(hits), 1)
        self.assertIn("Sara", hits[0]["raw"])

    def test_text_import(self):
        tf = self.tmp / "notes.md"
        tf.write_text("# Day one\n\nBought a server.\n\nDeployed the site.\n",
                      encoding="utf-8")
        n = import_text(tf, self.archive)
        self.assertEqual(n, 2)
        hits = self.archive.search("server")
        self.assertEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main()

"""Tests for Hacker News job discovery sanitization.

Security-relevant: ensures the parser never stores email addresses as
application URLs, correctly rejects non-http(s) strings, deobfuscates
common HN contact patterns, and generates stable synthetic URLs for
contact-only posts.
"""

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE jobs ("
        "  url TEXT PRIMARY KEY, title TEXT, salary TEXT, description TEXT,"
        "  location TEXT, site TEXT, strategy TEXT, discovered_at TEXT,"
        "  full_description TEXT, detail_scraped_at TEXT,"
        "  state TEXT DEFAULT 'discovered'"
        ")"
    )
    conn.execute(
        "CREATE TABLE job_state_transitions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  job_url TEXT NOT NULL REFERENCES jobs(url) ON DELETE CASCADE,"
        "  from_state TEXT,"
        "  to_state TEXT NOT NULL,"
        "  at TEXT NOT NULL,"
        "  reason TEXT,"
        "  metadata TEXT"
        ")"
    )
    conn.commit()
    return conn


class TestDeobfuscateEmail(unittest.TestCase):
    """_deobfuscate_email handles common HN obfuscation patterns."""

    def _run(self, text: str) -> str:
        from applypilot.discovery.hackernews import _deobfuscate_email
        return _deobfuscate_email(text)

    def test_bracket_at(self):
        self.assertEqual(self._run("user [at] company.com"), "user@company.com")

    def test_paren_at(self):
        self.assertEqual(self._run("user(at)company.com"), "user@company.com")

    def test_bracket_dot(self):
        self.assertEqual(self._run("user [at] company [dot] com"), "user@company.com")

    def test_plain_at(self):
        self.assertEqual(self._run("user at company.com"), "user@company.com")

    def test_already_normal(self):
        self.assertEqual(self._run("user@company.com"), "user@company.com")

    def test_non_email_unchanged(self):
        result = self._run("https://company.com/careers")
        self.assertIn("company.com", result)
        self.assertNotIn("@", result)


class TestIsEmail(unittest.TestCase):
    """_is_email correctly identifies email addresses (including obfuscated)."""

    def _run(self, text: str) -> bool:
        from applypilot.discovery.hackernews import _is_email
        return _is_email(text)

    def test_plain_email(self):
        self.assertTrue(self._run("hiring@company.com"))

    def test_obfuscated_email(self):
        self.assertTrue(self._run("hiring [at] company.com"))

    def test_http_url_not_email(self):
        self.assertFalse(self._run("https://company.com/jobs"))

    def test_bare_domain_not_email(self):
        self.assertFalse(self._run("company.com/careers"))

    def test_empty_not_email(self):
        self.assertFalse(self._run(""))


class TestStoreHnJob(unittest.TestCase):
    """_store_hn_job URL handling: http URLs stored as-is, emails/non-http get synthetic URL."""

    def setUp(self):
        self.conn = _make_db()

    def _store(self, job: dict) -> bool:
        from applypilot.discovery.hackernews import _store_hn_job
        return _store_hn_job(self.conn, job, "Who is Hiring? (March 2026)")

    def _get_url(self, title: str) -> str | None:
        row = self.conn.execute(
            "SELECT url FROM jobs WHERE title = ?", (title,)
        ).fetchone()
        return row[0] if row else None

    def test_http_url_stored_directly(self):
        self._store({"url": "https://company.com/jobs/swe", "title": "SWE", "company": "Acme"})
        url = self._get_url("SWE")
        self.assertEqual(url, "https://company.com/jobs/swe")

    def test_email_url_never_stored_as_url(self):
        """An email address in the url field must NOT become the stored URL."""
        self._store({"url": "hiring@company.com", "title": "Backend Dev", "company": "Acme"})
        url = self._get_url("Backend Dev")
        self.assertIsNotNone(url)
        self.assertNotIn("@", url, "Email address must not be used as the application URL")
        self.assertTrue(url.startswith("https://"), f"Expected https:// synthetic URL, got: {url}")

    def test_obfuscated_email_url_never_stored_as_url(self):
        """Obfuscated emails in the url field also must not become the stored URL."""
        self._store({"url": "hiring [at] company.com", "title": "DevOps Eng", "company": "Acme"})
        url = self._get_url("DevOps Eng")
        self.assertIsNotNone(url)
        self.assertNotIn("@", url)
        self.assertTrue(url.startswith("https://"))

    def test_bare_domain_gets_https_prefix(self):
        self._store({"url": "company.com/careers", "title": "Staff Eng", "company": "Acme"})
        url = self._get_url("Staff Eng")
        self.assertEqual(url, "https://company.com/careers")

    def test_no_url_gets_synthetic_hn_url(self):
        """Contact-only posts without any URL get a stable synthetic HN URL."""
        self._store({"url": None, "title": "Data Eng", "company": "Acme",
                     "contact": "hiring@acme.com"})
        url = self._get_url("Data Eng")
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("https://news.ycombinator.com/item?id="),
                        f"Expected synthetic HN URL, got: {url}")

    def test_duplicate_url_returns_false(self):
        job = {"url": "https://company.com/job/42", "title": "PM", "company": "Acme"}
        first = self._store(job)
        second = self._store(job)
        self.assertTrue(first)
        self.assertFalse(second)

    def test_contact_email_appended_to_description(self):
        """The deobfuscated contact email is stored in the description for the apply agent."""
        self._store({
            "url": "https://company.com/jobs/infra",
            "title": "Infra Eng",
            "company": "Acme",
            "contact": "infra [at] company.com",
            "description": "Great role.",
        })
        row = self.conn.execute(
            "SELECT description FROM jobs WHERE title = ?", ("Infra Eng",)
        ).fetchone()
        self.assertIn("infra@company.com", row[0])

    def test_non_http_non_domain_url_gets_synthetic(self):
        """Strings that aren't http and aren't bare domains fall back to synthetic URL."""
        self._store({"url": "apply via our careers page", "title": "ML Eng", "company": "Acme"})
        url = self._get_url("ML Eng")
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("https://"), f"Expected synthetic URL, got: {url}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

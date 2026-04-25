"""Tests for format-aware filtering in database.get_qa / get_all_qa.

Historical qa_knowledge rows like Q='Resume' A='Resume.pdf' were re-injected
into every apply prompt as KNOWN_ANSWERS, even after DOCX became the default
format. The fix adds an optional doc_format parameter that suppresses answers
referencing the OTHER format's file extension.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _seed_qa(conn: sqlite3.Connection, question: str, answer: str,
             outcome: str = "accepted") -> None:
    """Insert a qa_knowledge row directly."""
    from applypilot.database import question_key
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO qa_knowledge "
        "(question_text, question_key, answer_text, answer_source, "
        " outcome, created_at, updated_at) "
        "VALUES (?, ?, ?, 'human', ?, ?, ?)",
        (question, question_key(question), answer, outcome, now, now),
    )
    conn.commit()


class TestGetQaFormatFilter:
    """`get_qa` should suppress answers referencing the opposing format."""

    def test_no_filter_returns_any_match(self, tmp_db):
        conn = tmp_db()
        _seed_qa(conn, "Resume", "Resume.pdf")

        from applypilot.database import get_qa
        # No doc_format -> any match is fine
        assert get_qa("Resume", conn=conn) == "Resume.pdf"

    def test_docx_mode_skips_pdf_answer(self, tmp_db):
        conn = tmp_db()
        # Only a pdf-suggesting answer exists; docx mode must not return it.
        _seed_qa(conn, "Resume", "Resume.pdf")

        from applypilot.database import get_qa
        assert get_qa("Resume", doc_format="docx", conn=conn) is None

    def test_docx_mode_returns_docx_answer(self, tmp_db):
        conn = tmp_db()
        # Two candidate answers; docx mode returns the .docx one.
        _seed_qa(conn, "Resume", "Resume.pdf", outcome="unknown")
        _seed_qa(conn, "Resume", "Resume.docx", outcome="accepted")

        from applypilot.database import get_qa
        assert get_qa("Resume", doc_format="docx", conn=conn) == "Resume.docx"

    def test_pdf_mode_skips_docx_answer(self, tmp_db):
        conn = tmp_db()
        _seed_qa(conn, "Resume", "Resume.docx")

        from applypilot.database import get_qa
        assert get_qa("Resume", doc_format="pdf", conn=conn) is None

    def test_pdf_mode_returns_pdf_answer(self, tmp_db):
        conn = tmp_db()
        _seed_qa(conn, "Resume", "Resume.pdf", outcome="accepted")
        _seed_qa(conn, "Resume", "Resume.docx", outcome="unknown")

        from applypilot.database import get_qa
        assert get_qa("Resume", doc_format="pdf", conn=conn) == "Resume.pdf"

    def test_no_filter_returns_both_via_get_all_qa(self, tmp_db):
        """get_all_qa also supports doc_format filtering for the
        KNOWN_ANSWERS prompt injection path."""
        conn = tmp_db()
        _seed_qa(conn, "Resume", "Resume.pdf")
        _seed_qa(conn, "Resume", "Resume.docx")

        from applypilot.database import get_all_qa
        results = get_all_qa(conn=conn)
        answers = {r["answer_text"] for r in results}
        assert "Resume.pdf" in answers
        assert "Resume.docx" in answers

    def test_get_all_qa_docx_filter(self, tmp_db):
        conn = tmp_db()
        _seed_qa(conn, "Resume", "Resume.pdf")
        _seed_qa(conn, "Resume", "Resume.docx")
        _seed_qa(conn, "How many years?", "10")  # unrelated answer, no file ref

        from applypilot.database import get_all_qa
        results = get_all_qa(doc_format="docx", conn=conn)
        answers = {r["answer_text"] for r in results}
        assert "Resume.pdf" not in answers
        assert "Resume.docx" in answers
        assert "10" in answers  # unrelated answers pass through untouched

    def test_get_all_qa_pdf_filter(self, tmp_db):
        conn = tmp_db()
        _seed_qa(conn, "Resume", "Resume.pdf")
        _seed_qa(conn, "Resume", "Resume.docx")

        from applypilot.database import get_all_qa
        results = get_all_qa(doc_format="pdf", conn=conn)
        answers = {r["answer_text"] for r in results}
        assert "Resume.docx" not in answers
        assert "Resume.pdf" in answers

"""Tests that build_prompt renders doc-format-aware instructions.

Historically the prompt hard-coded "PDF" in the "Upload resume" and "Upload
cover letter" step descriptions even after ApplyPilot flipped to DOCX as the
default. These tests lock in the fix: both instructions must reference the
dynamic `{doc_format.upper()}` label so they read "DOCX path" / "PDF path"
depending on configuration.

Also verifies the internal variable rename (pdf_path -> resume_doc_path) via
the rendered prompt (the file path appears where we expect it).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


_MINIMAL_PROFILE = {
    "personal": {
        "full_name": "Test User",
        "preferred_name": "Test",
        "email": "test@example.com",
        "password": "hunter2",
        "phone": "555-000-0000",
        "address": "1 Main St",
        "city": "Seattle",
        "province_state": "WA",
        "country": "USA",
        "postal_code": "98101",
    },
    "work_authorization": {
        "legally_authorized_to_work": "Yes",
        "require_sponsorship": "No",
    },
    "availability": {
        "earliest_start_date": "Immediately",
    },
    "compensation": {
        "salary_expectation": "150000",
        "salary_currency": "USD",
    },
    "experience": {
        "years_of_experience_total": "10",
        "current_job_title": "Senior Software Engineer",
    },
    "eeo_voluntary": {},
    "skills_boundary": {},
    "resume_facts": {},
    "site_credentials": {},
    "files": {},
}


_MINIMAL_SEARCH = {
    "location": {
        "primary": "Seattle",
        "accept_patterns": ["Seattle", "Remote"],
        "linkedin_type_chars": 3,
    },
    "queries": [{"query": "software engineer", "tier": 1}],
}


def _setup_paths(tmp_path, monkeypatch):
    """Point config paths at a tmp dir, write a profile + search config."""
    from applypilot import config

    app_dir = tmp_path / "applypilot_home"
    app_dir.mkdir()
    apply_worker_dir = app_dir / "apply-workers"
    apply_worker_dir.mkdir()

    profile_path = app_dir / "profile.json"
    profile_path.write_text(json.dumps(_MINIMAL_PROFILE), encoding="utf-8")

    search_path = app_dir / "searches.yaml"
    import yaml
    search_path.write_text(yaml.safe_dump(_MINIMAL_SEARCH), encoding="utf-8")

    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "SEARCH_CONFIG_PATH", search_path)
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", apply_worker_dir)

    return app_dir


def _make_resume(tmp_path, ext: str) -> Path:
    """Create a fake resume .txt + counterpart file with the given extension."""
    resume_dir = tmp_path / "tailored"
    resume_dir.mkdir()
    txt = resume_dir / "acme_senior_engineer_abc123.txt"
    txt.write_text("Test User\nSenior Software Engineer\n", encoding="utf-8")
    doc = txt.with_suffix(f".{ext}")
    doc.write_bytes(b"fake-doc-content")
    return txt


def _build_job(resume_txt: Path) -> dict:
    return {
        "url": "https://example.com/job/1",
        "title": "Senior Software Engineer",
        "site": "acme",
        "application_url": "https://boards.greenhouse.io/acme/jobs/1",
        "fit_score": 9,
        "tailored_resume_path": str(resume_txt),
        "cover_letter_path": None,
    }


def _mock_db_calls(monkeypatch):
    """Stub out DB-dependent helpers so the test does not need a live DB."""
    from applypilot.apply import prompt as prompt_module

    monkeypatch.setattr(prompt_module, "get_all_qa", lambda: [])

    # get_accounts_for_prompt is imported lazily inside build_prompt; stub the
    # module-level symbol so both pre- and post-import lookups succeed.
    from applypilot import database
    monkeypatch.setattr(database, "get_accounts_for_prompt", lambda: {})


class TestPromptDocFormat:
    def test_docx_prompt_uses_docx_labels(self, tmp_path, monkeypatch):
        _setup_paths(tmp_path, monkeypatch)
        resume_txt = _make_resume(tmp_path, "docx")
        _mock_db_calls(monkeypatch)

        from applypilot.apply.prompt import build_prompt
        job = _build_job(resume_txt)
        result = build_prompt(job, tailored_resume="Resume text", doc_format="docx")

        # The two previously-hardcoded "PDF" lines must now say DOCX.
        assert "browser_file_upload with the DOCX path above" in result
        assert "use the cover letter DOCX path" in result
        # And there should be no stray "PDF path above" / "cover letter PDF path"
        # that contradicts the active format.
        assert "PDF path above" not in result
        assert "cover letter PDF path" not in result

    def test_pdf_prompt_uses_pdf_labels(self, tmp_path, monkeypatch):
        _setup_paths(tmp_path, monkeypatch)
        resume_txt = _make_resume(tmp_path, "pdf")
        _mock_db_calls(monkeypatch)

        from applypilot.apply.prompt import build_prompt
        job = _build_job(resume_txt)
        result = build_prompt(job, tailored_resume="Resume text", doc_format="pdf")

        assert "browser_file_upload with the PDF path above" in result
        assert "use the cover letter PDF path" in result
        assert "DOCX path above" not in result
        assert "cover letter DOCX path" not in result

    def test_no_hardcoded_pdf_token_in_source(self):
        """The source file should not contain a bare 'PDF' word token outside
        of the dynamic {doc_format.upper()} interpolations. Guards against
        someone re-introducing the hard-code."""
        import re
        src = Path(__file__).parent.parent / "src" / "applypilot" / "apply" / "prompt.py"
        text = src.read_text(encoding="utf-8")
        # Strip out all lines that contain `doc_format.upper()` — those are
        # the intentional dynamic references.
        lines = [ln for ln in text.splitlines() if "doc_format.upper()" not in ln]
        joined = "\n".join(lines)
        # A bare \bPDF\b token now should be a bug. Allow PDF in comments
        # like 'PDF rendering' only if they don't appear in f-string instructions.
        hits = [
            ln for ln in lines
            if re.search(r"\bPDF\b", ln)
            and "path above" in ln.lower() or "cover letter pdf" in ln.lower()
        ]
        assert not hits, f"Hard-coded PDF instructions resurfaced: {hits}"

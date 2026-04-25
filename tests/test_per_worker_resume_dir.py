"""Per-worker resume copy isolation (audit #9 fix).

Today, prompt.build_prompt() copies the resume to APPLY_WORKER_DIR/'current/',
which is shared across concurrent workers — worker A's resume can land on
worker B's clean filename between B's "build prompt" and "spawn claude" steps.

After the fix: each worker writes to APPLY_WORKER_DIR/f'worker-{wid}/'.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

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
    "availability": {"earliest_start_date": "Immediately"},
    "compensation": {"salary_expectation": "150000", "salary_currency": "USD"},
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
    """Point config paths at a tmp dir, write a profile + search config.

    Returns the apply-workers dir so tests can assert its contents.
    """
    from applypilot import config

    app_dir = tmp_path / "applypilot_home"
    app_dir.mkdir()
    apply_worker_dir = app_dir / "apply-workers"
    apply_worker_dir.mkdir()

    profile_path = app_dir / "profile.json"
    profile_path.write_text(json.dumps(_MINIMAL_PROFILE), encoding="utf-8")

    search_path = app_dir / "searches.yaml"
    search_path.write_text(yaml.safe_dump(_MINIMAL_SEARCH), encoding="utf-8")

    monkeypatch.setattr(config, "APP_DIR", app_dir)
    monkeypatch.setattr(config, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(config, "SEARCH_CONFIG_PATH", search_path)
    monkeypatch.setattr(config, "APPLY_WORKER_DIR", apply_worker_dir)

    return apply_worker_dir


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
    """Stub out DB-dependent helpers so tests don't need a live DB."""
    from applypilot.apply import prompt as prompt_module
    monkeypatch.setattr(prompt_module, "get_all_qa", lambda **_kw: [])
    from applypilot import database
    monkeypatch.setattr(database, "get_accounts_for_prompt", lambda: {})


class TestPerWorkerResumeDir:
    def test_resume_lands_in_per_worker_dir(self, tmp_path, monkeypatch):
        """build_prompt(worker_id=N) must write to apply-workers/worker-N/."""
        apply_worker_dir = _setup_paths(tmp_path, monkeypatch)
        resume_txt = _make_resume(tmp_path, "pdf")
        _mock_db_calls(monkeypatch)

        from applypilot.apply.prompt import build_prompt
        job = _build_job(resume_txt)
        build_prompt(job, tailored_resume="Resume text", worker_id=3, doc_format="pdf")

        worker_dir = apply_worker_dir / "worker-3"
        assert worker_dir.exists(), \
            f"worker-3 dir not created. apply-workers contents: " \
            f"{sorted(p.name for p in apply_worker_dir.iterdir())}"
        upload = worker_dir / "Test_User_Resume.pdf"
        assert upload.exists(), \
            f"resume not in worker-3/. dir contents: " \
            f"{sorted(p.name for p in worker_dir.iterdir())}"
        # The shared 'current/' dir from the buggy behavior must NOT exist.
        assert not (apply_worker_dir / "current").exists(), \
            "shared 'current/' dir was created — fix did not take effect"

    def test_two_workers_do_not_collide(self, tmp_path, monkeypatch):
        """Workers 0 and 1 must write to separate dirs and not overwrite each other."""
        apply_worker_dir = _setup_paths(tmp_path, monkeypatch)
        resume_txt = _make_resume(tmp_path, "pdf")
        _mock_db_calls(monkeypatch)

        from applypilot.apply.prompt import build_prompt
        job = _build_job(resume_txt)
        build_prompt(job, tailored_resume="x", worker_id=0, doc_format="pdf")
        build_prompt(job, tailored_resume="x", worker_id=1, doc_format="pdf")

        w0 = apply_worker_dir / "worker-0" / "Test_User_Resume.pdf"
        w1 = apply_worker_dir / "worker-1" / "Test_User_Resume.pdf"
        assert w0.exists() and w1.exists()
        # Verify the files are independent (rewriting one does not touch the other).
        w0.write_bytes(b"WORKER0_VERSION")
        assert w1.read_bytes() != b"WORKER0_VERSION"

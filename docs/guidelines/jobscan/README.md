# Jobscan Methodology — Reference Guidelines

Condensed, attributed summaries of Jobscan's published guidance on resumes, cover letters, interviews, and ATS optimization. Intended as (a) LLM prompt context for the tailoring/cover-letter stages of ApplyPilot, and (b) a human checklist for the operator.

All claims are sourced directly to `jobscan.co` URLs. When Jobscan hasn't published a number, these docs say so rather than invent one.

## Files

| File | Description |
|---|---|
| [`resume.md`](./resume.md) | ATS-beating resume rules: 80% match-rate target, PDF-default/DOCX-fallback file format, single-column formatting, standard section headers, keyword placement, soft vs hard skills, date format, length, font. |
| [`cover-letter.md`](./cover-letter.md) | Cover letter structure (4-paragraph Problem → Solution → Evidence → CTA), 250-400 words, one page, single-spaced, tone rules, 3.4x interview lift stat, greeting + sign-off conventions. |
| [`interview.md`](./interview.md) | STAR method, 10 behavioral categories, common question frameworks, virtual/phone/in-person tactics, salary negotiation (4 strategies), thank-you + follow-up cadence (3 attempts max post-interview). |
| [`ats-optimization.md`](./ats-optimization.md) | How ATS parsers actually work, platform-specific quirks (Workday, Greenhouse, Lever, iCIMS, Taleo, SuccessFactors), market share, keyword matching mechanics, formatting elements that break parsing. |

## How to Use

**For LLM prompts:** the tl;dr blocks at the top of each file are ~3 bullets each and work as system-prompt preambles. Full file bodies are 400-700 lines and fit cleanly into a 10K-token prompt budget.

**For the human operator:** the `Final Checklist` at the end of each file is the scannable version. Use before shipping any resume / cover letter / submitted application / interview.

## Key Numeric Thresholds (cross-file summary)

| Threshold | Source doc |
|---|---|
| **80% Jobscan match rate** (75% floor, 85% ceiling) | `resume.md` §1 |
| **Resume length: 1-2 pages**, body 10-12pt, 1" margins | `resume.md` §5 |
| **Cover letter: 250-400 words**, 1 page, 3-4 paragraphs | `cover-letter.md` §1-§2 |
| **Cover letter = 3.4x interview rate lift** | `cover-letter.md` §9 |
| **Job title verbatim = 10.6x interview rate lift** | `resume.md` §7, `ats-optimization.md` §2 |
| **76.4% of recruiters filter by skill** | `ats-optimization.md` §2 |
| **Interview answers: 60-90 seconds** | `interview.md` §6 |
| **Thank-you email: within 24 hours** | `interview.md` §11 |
| **Follow-up cap: 3 post-interview, 2 post-application** | `interview.md` §12-§13 |
| **ATS market share 2025 (Fortune 500):** Workday 39%, SuccessFactors 13.2% | `ats-optimization.md` §6 |
| **ATS market share 2025 (general):** Greenhouse 19.3%, Lever 16.6%, Workday 15.9%, iCIMS 15.3% | `ats-optimization.md` §6 |

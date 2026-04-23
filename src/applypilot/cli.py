"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__
from applypilot import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: int = typer.Option(
        config.DEFAULTS["min_score"], "--min-score",
        help=f"Minimum fit score for tailor/cover stages (default: {config.DEFAULTS['min_score']}).",
    ),
    max_age_days: int = typer.Option(
        config.DEFAULTS["max_job_age_days"], "--max-age-days",
        help=(
            "Skip jobs whose discovered_at is older than this many days. "
            "0 = no age filter. "
            f"Default: {config.DEFAULTS['max_job_age_days']}."
        ),
    ),
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max jobs per stage (tailor/cover). Default: 20."),
    workers: int = typer.Option(
        1, "--workers", "-w",
        help="Parallel threads for Workday/smart-extract stages. (JobSpy runs sequentially regardless.)",
    ),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    doc_format: str = typer.Option("pdf", "--doc-format", help="Document format for resumes/cover letters: pdf (default) or docx."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    source: Optional[list[str]] = typer.Option(
        None, "--source", "-s",
        help="Discovery source(s) to run. Repeatable: --source hn --source jobspy. "
             "Aliases: hn=hackernews, smart=smartextract. Only affects the discover stage.",
    ),
    list_sources: bool = typer.Option(
        False, "--list-sources",
        help="List available discovery sources and exit.",
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, tailor, cover, pdf."""
    # Handle --list-sources before bootstrap (no DB/env needed)
    if list_sources:
        from applypilot.pipeline import DISCOVERY_SOURCES, _SOURCE_ALIASES
        console.print("\n[bold]Available discovery sources:[/bold]\n")
        for name, desc in DISCOVERY_SOURCES.items():
            aliases = [a for a, canon in _SOURCE_ALIASES.items() if canon == name]
            alias_str = f"  (alias: {', '.join(aliases)})" if aliases else ""
            console.print(f"  [cyan]{name:<14s}[/cyan] {desc}{alias_str}")
        console.print("\nUsage: applypilot run discover --source hn --source jobspy")
        raise typer.Exit()

    _bootstrap()

    from applypilot.pipeline import run_pipeline, resolve_source_names

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Resolve --source aliases
    resolved_sources: list[str] | None = None
    if source:
        try:
            resolved_sources = resolve_source_names(source)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=1)

    # Validate --doc-format
    from applypilot.scoring.pdf import VALID_DOC_FORMATS
    if doc_format not in VALID_DOC_FORMATS:
        console.print(f"[red]Invalid --doc-format:[/red] '{doc_format}'. Must be one of: {', '.join(VALID_DOC_FORMATS)}")
        raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        max_age_days=max_age_days,
        limit=limit,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        sources=resolved_sources,
        doc_format=doc_format,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(5, "--workers", "-w", help="Number of parallel browser workers."),
    min_score: int = typer.Option(
        config.DEFAULTS["min_score"], "--min-score",
        help=f"Minimum fit score for job selection (default: {config.DEFAULTS['min_score']}).",
    ),
    max_age_days: int = typer.Option(
        config.DEFAULTS["max_job_age_days"], "--max-age-days",
        help=(
            "Skip jobs whose discovered_at is older than this many days. "
            "0 = no age filter. "
            f"Default: {config.DEFAULTS['max_job_age_days']}."
        ),
    ),
    max_score: Optional[int] = typer.Option(None, "--max-score", help="Maximum fit score for job selection (useful for testing on lower-score jobs)."),
    model: str = typer.Option("haiku", "--model", "-m", help="Claude model name."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    doc_format: str = typer.Option("pdf", "--doc-format", help="Document format for resumes/cover letters: pdf (default) or docx."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    fresh_sessions: bool = typer.Option(False, "--fresh-sessions", help="Refresh Chrome session cookies from your real profile before launching."),
    no_hitl: bool = typer.Option(False, "--no-hitl", help="Skip HITL waits: park needs_human jobs and move on. Use for overnight runs."),
    no_focus: bool = typer.Option(False, "--no-focus", help="Prevent Chrome windows from stealing keyboard focus (Linux/GNOME only). Windows stay visible but won't interrupt your active app."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
    reset_category: Optional[str] = typer.Option(None, "--reset-category", help="Reset all jobs in a category for retry (e.g., blocked_technical)."),
    sessions: bool = typer.Option(False, "--sessions", help="List saved ATS sessions."),
    clear_session: Optional[str] = typer.Option(None, "--clear-session", help="Clear a saved ATS session (e.g., workday)."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    if reset_category:
        from applypilot.database import reset_by_category
        count = reset_by_category(reset_category)
        console.print(f"[green]Reset {count} job(s) in category '{reset_category}' for retry.[/green]")
        return

    if sessions:
        from applypilot.apply.chrome import list_ats_sessions
        ats_sessions = list_ats_sessions()
        if not ats_sessions:
            console.print("[dim]No saved ATS sessions.[/dim]")
            return
        from rich.table import Table
        t = Table(title="Saved ATS Sessions", show_header=True, header_style="bold cyan")
        t.add_column("ATS")
        t.add_column("Cookies")
        t.add_column("Age")
        for s in ats_sessions:
            age_str = f"{s['age_hours']:.1f}h" if s["age_hours"] is not None else "n/a"
            t.add_row(s["slug"], "yes" if s["has_cookies"] else "no", age_str)
        console.print(t)
        return

    if clear_session:
        from applypilot.apply.chrome import clear_ats_session
        if clear_ats_session(clear_session):
            console.print(f"[green]Cleared ATS session: {clear_session}[/green]")
        else:
            console.print(f"[yellow]No session found for: {clear_session}[/yellow]")
        return

    # --- Full apply mode ---

    # Validate --doc-format
    from applypilot.scoring.pdf import VALID_DOC_FORMATS as _valid_fmts
    if doc_format not in _valid_fmts:
        console.print(f"[red]Invalid --doc-format:[/red] '{doc_format}'. Must be one of: {', '.join(_valid_fmts)}")
        raise typer.Exit(code=1)

    # Set doc format for apply workers
    from applypilot.apply.launcher import set_doc_format
    set_doc_format(doc_format)

    # Check 1: Tier 3 required (Claude Code CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: Tailored resumes exist (skip for --gen with --url)
    if not (gen and url):
        conn = get_connection()
        ready = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL"
        ).fetchone()[0]
        if ready == 0:
            console.print(
                "[red]No tailored resumes ready.[/red]\n"
                "Run [bold]applypilot run score tailor[/bold] first to prepare applications."
            )
            raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import gen_prompt
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, max_score=max_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print("\n[bold]Run manually:[/bold]")
        console.print(
            f"  claude --model {model} -p "
            f"--mcp-config {mcp_path} "
            f"--permission-mode bypassPermissions < {prompt_file}"
        )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else 0

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Model:    {model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if fresh_sessions:
        console.print("  Sessions: [yellow]refreshing from real Chrome profile[/yellow]")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        max_age_days=max_age_days,
        max_score=max_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        fresh_sessions=fresh_sessions,
        no_hitl=no_hitl,
        no_focus=no_focus,
    )


@app.command()
def status() -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))
    if stats.get("needs_human", 0) > 0:
        summary.add_row("Needs human review", str(stats["needs_human"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # Score funnel: per-score breakdown of pipeline stages
    funnel = stats.get("score_funnel", [])
    if funnel:
        funnel_table = Table(
            title="\nPipeline Funnel by Score",
            show_header=True,
            header_style="bold magenta",
        )
        funnel_table.add_column("Score",        justify="center", style="bold")
        funnel_table.add_column("Cover Ready",  justify="right", style="green")
        funnel_table.add_column("Tailored",     justify="right", style="cyan")
        funnel_table.add_column("Needs Tailor", justify="right", style="yellow")
        funnel_table.add_column("Applied",      justify="right", style="dim green")
        funnel_table.add_column("Errors",       justify="right", style="red")

        for row in funnel:
            score = row["score"]
            score_str = f"[bold {'green' if score >= 9 else 'yellow' if score >= 7 else 'white'}]{score}[/]"
            funnel_table.add_row(
                score_str,
                str(row["cover_ready"])  if row["cover_ready"]  else "[dim]—[/]",
                str(row["tailored"])     if row["tailored"]     else "[dim]—[/]",
                str(row["needs_tailor"]) if row["needs_tailor"] else "[dim]—[/]",
                str(row["applied"])      if row["applied"]      else "[dim]—[/]",
                str(row["errors"])       if row["errors"]       else "[dim]—[/]",
            )

        console.print(funnel_table)

    # Apply categories with per-score breakdown
    by_cat = stats.get("by_category", {})
    if by_cat:
        cat_table = Table(title="\nApply Categories", show_header=True, header_style="bold blue")
        cat_table.add_column("Category", style="bold")
        cat_table.add_column("Total", justify="right")
        cat_table.add_column("10",  justify="right", style="bold green")
        cat_table.add_column("9",   justify="right", style="green")
        cat_table.add_column("8",   justify="right", style="yellow")
        cat_table.add_column("7",   justify="right", style="yellow")
        cat_table.add_column("6",   justify="right", style="dim")
        cat_table.add_column("<6",  justify="right", style="dim")
        cat_table.add_column("Action")

        cat_display = {
            "applied":             ("green",   "Done"),
            "pending":             ("white",   "In queue"),
            "in_progress":         ("cyan",    "Running now"),
            "needs_human":         ("magenta", "applypilot human-review"),
            "blocked_auth":        ("yellow",  "Needs persistent sessions / HITL"),
            "blocked_technical":   ("yellow",  "Retryable: applypilot reset-category blocked_technical"),
            "archived_ineligible": ("dim",     "Location/salary/type mismatch"),
            "archived_expired":    ("dim",     "Job no longer available"),
            "archived_platform":   ("red",     "Unsupported platform"),
            "archived_no_url":     ("dim",     "No application URL"),
            "manual_only":         ("dim",     "Manual ATS (no automation)"),
        }

        def _score_cell(d: dict, key: str) -> str:
            """Format a score count cell — blank if zero."""
            v = d.get(key, 0) if isinstance(d, dict) else 0
            return str(v) if v else "[dim]—[/dim]"

        order = ["applied", "pending", "in_progress", "needs_human",
                 "blocked_auth", "blocked_technical",
                 "archived_ineligible", "archived_expired",
                 "archived_platform", "archived_no_url", "manual_only"]
        for cat in order:
            d = by_cat.get(cat)
            if not d:
                continue
            total = d["total"] if isinstance(d, dict) else d
            color, action = cat_display.get(cat, ("white", ""))
            cat_table.add_row(
                f"[{color}]{cat}[/{color}]",
                str(total),
                _score_cell(d, "10"),
                _score_cell(d, "9"),
                _score_cell(d, "8"),
                _score_cell(d, "7"),
                _score_cell(d, "6"),
                _score_cell(d, "<6"),
                action,
            )

        # Any unknown categories
        for cat, d in sorted(by_cat.items(), key=lambda x: -(x[1]["total"] if isinstance(x[1], dict) else x[1])):
            if cat not in cat_display:
                total = d["total"] if isinstance(d, dict) else d
                if total > 0:
                    cat_table.add_row(
                        cat, str(total),
                        _score_cell(d, "10"), _score_cell(d, "9"),
                        _score_cell(d, "8"),  _score_cell(d, "7"),
                        _score_cell(d, "6"),  _score_cell(d, "<6"),
                        "",
                    )

        console.print(cat_table)

        # Retry hint for high-score retryable jobs
        retryable_tech = by_cat.get("blocked_technical", {})
        if isinstance(retryable_tech, dict):
            high_score_retryable = retryable_tech.get("10", 0) + retryable_tech.get("9", 0)
            if high_score_retryable > 0:
                console.print(
                    f"[bold yellow]  ↳ {high_score_retryable} score 9-10 jobs in blocked_technical are retryable.[/bold yellow]"
                    f" Run: [bold]applypilot reset-category blocked_technical[/bold]"
                )

    # By site (group all HN: * sites under "HackerNews")
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        grouped: dict[str, int] = {}
        for site, count in stats["by_site"]:
            key = "HackerNews" if (site or "").startswith("HN:") else (site or "Unknown")
            grouped[key] = grouped.get(key, 0) + count
        for site, count in sorted(grouped.items(), key=lambda x: -x[1]):
            site_table.add_row(site, str(count))

        console.print(site_table)

    # ── Funnel diagnostics ────────────────────────────────────────
    if stats.get("skipped_stale"):
        max_age = config.DEFAULTS["max_job_age_days"]
        console.print(
            f"\n[dim]Skipped as stale (>{max_age}d old): "
            f"{stats['skipped_stale']} ready-to-apply jobs[/dim]"
        )

    bbc = stats.get("blocked_by_cap") or {}
    if bbc.get("count"):
        console.print(
            f"\n[yellow]Blocked by company cap:[/yellow] "
            f"{bbc['count']} company/companies"
        )
        if bbc.get("companies"):
            preview = ", ".join(bbc["companies"][:10])
            more = f" (+{bbc['count'] - 10} more)" if bbc["count"] > 10 else ""
            console.print(f"  [dim]{preview}{more}[/dim]")

    console.print()


@app.command()
def track(
    days: int = typer.Option(14, "--days", "-d", help="Email look-back period in days."),
    setup: bool = typer.Option(False, "--setup", help="Verify Gmail MCP connectivity."),
    actions: bool = typer.Option(False, "--actions", "-a", help="Show pending action items."),
    ghosted_days: int = typer.Option(7, "--ghosted-days", help="Days before marking as ghosted."),
    limit: int = typer.Option(100, "--limit", "-l", help="Max emails to fetch."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch + classify but don't write DB/files."),
    relabel: bool = typer.Option(False, "--relabel", help="Apply 'ap-track' label to all emails already in the DB (backfill)."),
    remap_stubs: bool = typer.Option(False, "--remap-stubs", help="Re-match emails under multi-company stubs to correct per-company jobs."),
) -> None:
    """Track application responses from Gmail."""
    _bootstrap()

    from applypilot.config import check_tier
    check_tier(2, "application tracking")

    if setup:
        import asyncio
        from applypilot.tracking.gmail_client import check_gmail_setup, verify_connection

        ok, msg = check_gmail_setup()
        if not ok:
            console.print(f"[red]{msg}[/red]")
            raise typer.Exit(code=1)

        console.print("[dim]Testing Gmail MCP connection...[/dim]")
        connected = asyncio.run(verify_connection())
        if connected:
            console.print("[green]Gmail MCP connected successfully.[/green]")
        else:
            console.print("[red]Gmail MCP connection failed.[/red]")
            console.print("[dim]Check that gcp-oauth.keys.json is valid and OAuth is authorized.[/dim]")
            raise typer.Exit(code=1)
        return

    if actions:
        from applypilot.tracking import show_action_items
        show_action_items()
        return

    if relabel:
        from applypilot.tracking import relabel_all_tracked
        relabel_all_tracked()
        return

    if remap_stubs:
        from applypilot.tracking import remap_stubs as _remap_stubs
        _remap_stubs()
        return

    from applypilot.tracking import run_tracking

    result = run_tracking(
        days=days,
        ghosted_days=ghosted_days,
        limit=limit,
        dry_run=dry_run,
    )

    if result.get("errors", 0) > 0:
        raise typer.Exit(code=1)


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command("human-review")
def human_review(
    port: int = typer.Option(7373, "--port", help="Port for the review UI server."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't auto-open the browser."),
) -> None:
    """Launch Human-in-the-Loop review UI for parked jobs."""
    _bootstrap()

    from applypilot.database import get_stats
    from applypilot.apply.human_review import serve

    stats = get_stats()
    count = stats.get("needs_human", 0)

    if count == 0:
        console.print("[green]No jobs in the human review queue.[/green]")
        console.print("[dim]Jobs are parked here when the apply agent hits a login wall it can't solve.[/dim]")
        raise typer.Exit()

    console.print(f"\n[bold purple]Human Review Queue:[/bold purple] {count} job(s) need attention\n")
    serve(port=port, open_browser=not no_browser)


# ---------------------------------------------------------------------------
# Q&A knowledge base commands
# ---------------------------------------------------------------------------

qa_app = typer.Typer(name="qa", help="Manage the Q&A knowledge base for screening questions.")
app.add_typer(qa_app)


@qa_app.command("list")
def qa_list(
    limit: int = typer.Option(50, "--limit", "-l", help="Max Q&A pairs to show."),
) -> None:
    """Show stored Q&A pairs from past applications."""
    _bootstrap()

    from applypilot.database import get_all_qa

    pairs = get_all_qa()
    if not pairs:
        console.print("[dim]No Q&A pairs stored yet. Apply to jobs to build the knowledge base.[/dim]")
        return

    t = Table(title=f"Q&A Knowledge Base ({len(pairs)} total)", show_header=True, header_style="bold cyan")
    t.add_column("Question", max_width=50)
    t.add_column("Answer", max_width=30)
    t.add_column("Source")
    t.add_column("Outcome")
    t.add_column("Type")

    for qa in pairs[:limit]:
        outcome_color = {"accepted": "green", "rejected": "red"}.get(qa["outcome"], "dim")
        t.add_row(
            qa["question_text"][:50],
            qa["answer_text"][:30],
            qa["answer_source"],
            f"[{outcome_color}]{qa['outcome']}[/{outcome_color}]",
            qa.get("field_type") or "",
        )

    console.print(t)


@qa_app.command("stats")
def qa_stats() -> None:
    """Show Q&A knowledge base statistics."""
    _bootstrap()

    from applypilot.database import get_qa_stats

    stats = get_qa_stats()
    if stats["total"] == 0:
        console.print("[dim]No Q&A pairs stored yet.[/dim]")
        return

    console.print("\n[bold]Q&A Knowledge Base Stats[/bold]\n")
    console.print(f"  Total pairs:    {stats['total']}")
    console.print(f"  Unique Qs:      {stats['unique_questions']}")

    if stats["by_source"]:
        console.print("\n  [bold]By source:[/bold]")
        for src, cnt in stats["by_source"].items():
            console.print(f"    {src}: {cnt}")

    if stats["by_outcome"]:
        console.print("\n  [bold]By outcome:[/bold]")
        for out, cnt in stats["by_outcome"].items():
            color = {"accepted": "green", "rejected": "red"}.get(out, "dim")
            console.print(f"    [{color}]{out}[/{color}]: {cnt}")

    if stats.get("by_ats"):
        console.print("\n  [bold]By ATS:[/bold]")
        for ats, cnt in stats["by_ats"].items():
            console.print(f"    {ats or 'unknown'}: {cnt}")

    console.print()


@qa_app.command("export")
def qa_export(
    output: str = typer.Option("qa_export.yaml", "--output", "-o", help="Output YAML file path."),
) -> None:
    """Export Q&A pairs to YAML for review and editing."""
    _bootstrap()

    from applypilot.database import export_qa_yaml

    content = export_qa_yaml()
    if not content:
        console.print("[dim]No Q&A pairs to export.[/dim]")
        return

    from pathlib import Path
    out_path = Path(output)
    out_path.write_text(content, encoding="utf-8")
    console.print(f"[green]Exported Q&A to:[/green] {out_path.resolve()}")


@qa_app.command("import")
def qa_import(
    file: str = typer.Argument(..., help="YAML file with Q&A pairs to import."),
) -> None:
    """Import Q&A pairs from a YAML file."""
    _bootstrap()

    from pathlib import Path
    import yaml
    from applypilot.database import store_qa

    path = Path(file)
    if not path.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(code=1)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        console.print("[red]Expected a YAML list of Q&A objects.[/red]")
        raise typer.Exit(code=1)

    count = 0
    for item in data:
        q = item.get("question", "").strip()
        a = item.get("answer", "").strip()
        if q and a:
            store_qa(
                q, a,
                source=item.get("source", "human"),
                field_type=item.get("field_type"),
                ats_slug=item.get("ats"),
            )
            count += 1

    console.print(f"[green]Imported {count} Q&A pair(s).[/green]")


# ---------------------------------------------------------------------------
# creds — site credential CRUD
# ---------------------------------------------------------------------------

creds_app = typer.Typer(name="creds", help="Manage saved site credentials (usernames / passwords).")
app.add_typer(creds_app)


@creds_app.command("list")
def creds_list(
    show: bool = typer.Option(False, "--show", "-s", help="Show passwords in plaintext."),
) -> None:
    """List all saved site credentials."""
    _bootstrap()

    from applypilot.database import get_all_accounts

    rows = get_all_accounts()
    if not rows:
        console.print("[dim]No credentials saved yet. Use [bold]applypilot creds add[/bold] to add one.[/dim]")
        return

    t = Table(title=f"Site Credentials ({len(rows)} entries)", show_header=True, header_style="bold cyan")
    t.add_column("Domain",   min_width=28)
    t.add_column("Site",     min_width=12)
    t.add_column("Email",    min_width=24)
    t.add_column("Password", min_width=16)
    t.add_column("Notes",    max_width=30)
    t.add_column("Saved",    min_width=10)

    for row in rows:
        pwd = row["password"] or ""
        pwd_display = pwd if show else (("*" * min(len(pwd), 8)) if pwd else "[dim]—[/dim]")
        saved_date = (row["created_at"] or "")[:10]
        t.add_row(
            row["domain"],
            row["site"] or "",
            row["email"],
            pwd_display,
            row["notes"] or "",
            saved_date,
        )

    console.print(t)
    if not show:
        console.print("[dim]Passwords are masked. Use [bold]--show[/bold] to reveal them.[/dim]")


@creds_app.command("show")
def creds_show(
    domain: str = typer.Argument(..., help="Domain to show credentials for (e.g. linkedin.com)."),
) -> None:
    """Show full (unmasked) credentials for a single domain."""
    _bootstrap()

    from applypilot.database import get_all_accounts

    rows = [r for r in get_all_accounts() if r["domain"] == domain]
    if not rows:
        console.print(f"[red]No credentials found for domain:[/red] {domain}")
        raise typer.Exit(code=1)

    row = rows[0]
    console.print(f"\n  [bold]Domain:[/bold]   {row['domain']}")
    console.print(f"  [bold]Site:[/bold]     {row['site'] or ''}")
    console.print(f"  [bold]Email:[/bold]    {row['email']}")
    console.print(f"  [bold]Password:[/bold] {row['password'] or '[dim](none)[/dim]'}")
    if row["notes"]:
        console.print(f"  [bold]Notes:[/bold]    {row['notes']}")
    if row["job_url"]:
        console.print(f"  [bold]Job URL:[/bold]  {row['job_url']}")
    console.print(f"  [bold]Saved:[/bold]    {(row['created_at'] or '')[:19]}\n")


@creds_app.command("add")
def creds_add(
    domain:   str = typer.Argument(..., help="Domain key (e.g. linkedin.com, myworkdayjobs.com)."),
    email:    str = typer.Option(...,  "--email",    "-e", help="Login email / username."),
    password: str = typer.Option(None, "--password", "-p", help="Password (prompted if omitted)."),
    site:     str = typer.Option(None, "--site",     "-s", help="Human-readable site name."),
    notes:    str = typer.Option(None, "--notes",    "-n", help="Optional notes."),
) -> None:
    """Add or update credentials for a site."""
    _bootstrap()

    from applypilot.database import upsert_account

    if password is None:
        password = typer.prompt(f"Password for {domain}", hide_input=True, confirmation_prompt=True)

    action = upsert_account(domain, email, password, site=site, notes=notes)
    verb = "[green]Created[/green]" if action == "created" else "[yellow]Updated[/yellow]"
    console.print(f"{verb} credentials for [bold]{domain}[/bold] ({email})")


@creds_app.command("set")
def creds_set(
    domain:   str = typer.Argument(..., help="Domain to update."),
    email:    str = typer.Option(None, "--email",    "-e", help="New email / username."),
    password: str = typer.Option(None, "--password", "-p", help="New password (prompted if --email not given either)."),
    notes:    str = typer.Option(None, "--notes",    "-n", help="Update notes."),
) -> None:
    """Update one or more fields for an existing credential entry."""
    _bootstrap()

    from applypilot.database import get_all_accounts, upsert_account

    existing = next((r for r in get_all_accounts() if r["domain"] == domain), None)
    if not existing:
        console.print(f"[red]No credentials found for:[/red] {domain}  (use [bold]add[/bold] to create one)")
        raise typer.Exit(code=1)

    new_email    = email    or existing["email"]
    new_password = password or existing["password"]
    new_notes    = notes    if notes is not None else existing["notes"]

    if not email and not password and notes is None:
        # Nothing specified — prompt for password at minimum
        new_password = typer.prompt(f"New password for {domain}", hide_input=True, confirmation_prompt=True)

    upsert_account(domain, new_email, new_password, site=existing["site"], notes=new_notes)
    console.print(f"[green]Updated[/green] credentials for [bold]{domain}[/bold]")


@creds_app.command("import-logs")
def creds_import_logs(
    log_dir: str = typer.Option(None, "--log-dir", help="Directory of apply logs. Defaults to ~/.applypilot/logs/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be imported without writing."),
    yes:     bool = typer.Option(False, "--yes", "-y", help="Skip per-entry confirmation for free-form entries."),
) -> None:
    """Scan apply logs for account credentials and import into the DB.

    Handles both structured ACCOUNT_CREATED: lines (current format) and
    older free-form log entries where the agent wrote the password as prose.
    Already-known domains are skipped unless --yes is passed.
    """
    _bootstrap()

    import os
    from applypilot.database import mine_accounts_from_logs, upsert_account, get_all_accounts

    if log_dir is None:
        log_dir = os.path.expanduser("~/.applypilot/logs")

    console.print(f"[dim]Scanning logs in {log_dir}…[/dim]")
    found = mine_accounts_from_logs(log_dir)

    if not found:
        console.print("[dim]No credential hints found in logs.[/dim]")
        return

    existing_domains = {r["domain"] for r in get_all_accounts()}

    imported = skipped = already = 0
    for entry in found:
        domain   = entry["domain"]
        email    = entry["email"]
        password = entry.get("password", "")
        site     = entry.get("site", "")
        source   = entry.get("source", "")
        src_file = entry.get("source_file", "")
        is_new   = domain not in existing_domains

        status_tag = "[green]NEW[/green]" if is_new else "[dim]EXISTS[/dim]"
        src_tag    = "[cyan]structured[/cyan]" if source == "structured" else "[yellow]free-form[/yellow]"
        console.print(
            f"  {status_tag} {src_tag}  {domain}  {email or '[dim](no email)[/dim]'}"
            f"  pw={'***' if password else '[dim]none[/dim]'}  ({src_file})"
        )

        if not is_new:
            already += 1
            continue

        if dry_run:
            imported += 1
            continue

        # Free-form entries ask for confirmation (may be less reliable)
        if source == "free-form" and not yes:
            if not email or not password:
                console.print(f"    [dim]Skipping — missing email or password. Add manually with: "
                              f"applypilot creds add {domain}[/dim]")
                skipped += 1
                continue
            confirmed = typer.confirm(f"    Import {domain} ({email} / {password[:4]}***)?")
            if not confirmed:
                skipped += 1
                continue

        if email and password:
            upsert_account(domain, email, password, site=site or None,
                           notes=f"auto-imported from log: {src_file}")
            existing_domains.add(domain)
            imported += 1
        else:
            console.print(f"    [dim]Skipping — missing {'email' if not email else 'password'}. "
                          f"Add manually: applypilot creds add {domain}[/dim]")
            skipped += 1

    suffix = " [dim](dry run — nothing written)[/dim]" if dry_run else ""
    console.print(
        f"\n[green]Imported {imported}[/green]  "
        f"[dim]already-known {already}  skipped {skipped}[/dim]{suffix}"
    )


@creds_app.command("delete")
def creds_delete(
    domain: str = typer.Argument(..., help="Domain whose credentials to delete."),
    yes:    bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Delete all saved credentials for a domain."""
    _bootstrap()

    from applypilot.database import delete_account, get_all_accounts

    existing = [r for r in get_all_accounts() if r["domain"] == domain]
    if not existing:
        console.print(f"[red]No credentials found for:[/red] {domain}")
        raise typer.Exit(code=1)

    row = existing[0]
    if not yes:
        confirmed = typer.confirm(f"Delete credentials for {domain} ({row['email']})?")
        if not confirmed:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit()

    deleted = delete_account(domain)
    console.print(f"[green]Deleted {deleted} credential row(s) for[/green] [bold]{domain}[/bold]")


if __name__ == "__main__":
    app()

"""Command-line interface for PR Cop.

Subcommands:
    prcop review        Review a diff (file, stdin, or local repo refs).
    prcop review-pr     Review a GitHub PR by owner/repo/number.
    prcop provider      Show the active LLM provider snapshot.
    prcop serve         Run the FastAPI server.

The CLI is intentionally thin — all real logic lives in ``prcop.orchestrator``
so the same code path is exercised by tests, the HTTP API, and direct CLI use.
"""
from __future__ import annotations
import asyncio
import json
import sys

import click
from rich.console import Console
from rich.panel import Panel

from prcop import __version__
from prcop.llm import provider_info
from prcop.orchestrator import (
    render_markdown_report,
    render_text_report,
    review_diff,
)
from prcop.sources import (
    diff_from_file,
    diff_from_github_pr,
    diff_from_local_repo,
    diff_from_text,
    post_github_pr_comment,
)
from prcop.specialists import SQUAD_NAMES


def _parse_specialists(value: str | None) -> list[str] | None:
    """Split a comma-separated --specialists value into a clean list."""
    if not value:
        return None
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    return parts or None


console = Console()


def _print_result_text(result_dict: dict, *, json_out: bool) -> None:
    if json_out:
        click.echo(json.dumps(result_dict, indent=2, ensure_ascii=False))
        return
    console.print(Panel.fit(result_dict["verdict"], title="PR Cop verdict"))
    if not result_dict["findings"]:
        return
    console.print()
    console.print(f"[bold]Findings ({len(result_dict['findings'])})[/bold]")
    for f in result_dict["findings"]:
        loc = f["file"] + (f":{f['line']}" if f.get("line") else "")
        sev = f["severity"].upper()
        console.print(
            f"  [bold]{sev:<8}[/bold] [{f['specialist']}] {f['title']}\n"
            f"    [dim]{loc} ({f['category']})[/dim]\n"
            f"    {f['rationale']}"
        )
        if f.get("suggestion"):
            console.print(f"    [italic]suggest:[/italic] {f['suggestion']}")
        console.print()


@click.group()
@click.version_option(__version__, prog_name="prcop")
def main() -> None:
    """PR Cop — multi-agent code review squad."""


@main.command("review")
@click.option(
    "--diff", "diff_path",
    help="Path to a unified diff file. Use '-' for stdin.",
)
@click.option("--repo", "repo_path", type=click.Path(file_okay=False, exists=True), help="Local git repo path.")
@click.option("--base", default=None, help="Base ref (used with --repo).")
@click.option("--head", default="HEAD", help="Head ref (used with --repo).")
@click.option("--title", default=None, help="Optional PR title for context.")
@click.option("--description", default=None, help="Optional PR description for context.")
@click.option(
    "--specialists",
    "specialists_csv",
    default=None,
    help=f"Comma-separated subset of specialists to run. Choices: {','.join(SQUAD_NAMES)}.",
)
@click.option("--json", "json_out", is_flag=True, help="Emit JSON instead of pretty text.")
@click.option("--markdown", "markdown_out", is_flag=True, help="Emit GitHub-flavoured markdown.")
def review_cmd(
    diff_path: str | None,
    repo_path: str | None,
    base: str | None,
    head: str,
    title: str | None,
    description: str | None,
    specialists_csv: str | None,
    json_out: bool,
    markdown_out: bool,
) -> None:
    """Review a diff from a file, stdin, or a local repo's ref range."""
    if diff_path == "-":
        diff_text = sys.stdin.read()
        src = diff_from_text(diff_text, repo_meta={"source": "stdin"})
    elif diff_path:
        src = diff_from_file(diff_path)
    elif repo_path:
        if not base:
            raise click.UsageError("--base is required when using --repo")
        src = diff_from_local_repo(repo_path, base, head)
    else:
        raise click.UsageError("provide one of --diff, --diff -, or --repo + --base")

    if title:
        src.repo_meta["title"] = title
    if description:
        src.repo_meta["description"] = description

    specialists = _parse_specialists(specialists_csv)
    try:
        result = asyncio.run(review_diff(
            diff_text=src.diff_text,
            repo_meta=src.repo_meta,
            specialists=specialists,
        ))
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    if markdown_out:
        click.echo(render_markdown_report(result))
        return
    if json_out:
        click.echo(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return
    click.echo(render_text_report(result))


@main.command("review-pr")
@click.argument("slug")
@click.argument("pr_number", type=int)
@click.option("--post-comment", is_flag=True, help="Post the rendered review back to the PR.")
@click.option("--markdown", "markdown_out", is_flag=True, help="Emit GitHub-flavoured markdown.")
@click.option("--json", "json_out", is_flag=True, help="Emit JSON instead of pretty text.")
@click.option(
    "--specialists",
    "specialists_csv",
    default=None,
    help=f"Comma-separated subset of specialists to run. Choices: {','.join(SQUAD_NAMES)}.",
)
def review_pr_cmd(
    slug: str,
    pr_number: int,
    post_comment: bool,
    markdown_out: bool,
    json_out: bool,
    specialists_csv: str | None,
) -> None:
    """Review a GitHub PR. SLUG is owner/repo, e.g. 'octocat/hello-world'."""
    if "/" not in slug:
        raise click.UsageError("slug must be 'owner/repo'")
    owner, repo = slug.split("/", 1)

    specialists = _parse_specialists(specialists_csv)

    async def _run():
        src = await diff_from_github_pr(owner, repo, pr_number)
        result = await review_diff(
            diff_text=src.diff_text,
            repo_meta=src.repo_meta,
            specialists=specialists,
        )
        comment_url = None
        if post_comment:
            body = render_markdown_report(result)
            try:
                comment = await post_github_pr_comment(owner, repo, pr_number, body)
                comment_url = comment.get("html_url")
            except Exception as e:  # surface but don't crash the command
                click.echo(f"warning: failed to post comment: {e}", err=True)
        return result, comment_url

    try:
        result, comment_url = asyncio.run(_run())
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    if markdown_out:
        click.echo(render_markdown_report(result))
    elif json_out:
        payload = result.to_dict()
        if comment_url:
            payload["github_comment_url"] = comment_url
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        click.echo(render_text_report(result))
        if comment_url:
            click.echo(f"\nPosted comment: {comment_url}")


@main.command("provider")
def provider_cmd() -> None:
    """Show the active LLM provider configuration."""
    info = provider_info()
    click.echo(json.dumps(info, indent=2))


@main.command("serve")
@click.option("--host", default=None, help="Override PRCOP_HOST. Default 127.0.0.1; use 0.0.0.0 only behind auth.")
@click.option("--port", default=None, type=int, help="Override PRCOP_PORT.")
def serve_cmd(host: str | None, port: int | None) -> None:
    """Run the FastAPI server (uvicorn)."""
    import os
    import uvicorn
    h = host or os.environ.get("PRCOP_HOST", "127.0.0.1")
    p = port or int(os.environ.get("PRCOP_PORT", "8080"))
    uvicorn.run("prcop.server:app", host=h, port=p, reload=False)


if __name__ == "__main__":
    main()

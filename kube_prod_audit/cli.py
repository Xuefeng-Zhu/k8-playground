#!/usr/bin/env python3
"""CLI entry point for kube-prod-audit."""
import argparse
import os
import sys

from . import run_audit, render_html, render_terminal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kube-prod-audit",
        description="Production-readiness audit for Kubernetes clusters.",
    )
    parser.add_argument(
        "--mode", choices=("auto", "live", "demo"), default="auto",
        help="Where to source cluster data from (default: auto-detect)",
    )
    parser.add_argument(
        "--kubeconfig", default=os.environ.get("KUBECONFIG"),
        help="Path to kubeconfig (default: $KUBECONFIG or ~/.kube/config)",
    )
    parser.add_argument(
        "--context", default=None,
        help="kubeconfig context to use",
    )
    parser.add_argument(
        "--output", "-o", default="audit-report.html",
        help="HTML report output path (default: audit-report.html)",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI colour in terminal output",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Skip terminal output (HTML only)",
    )
    parser.add_argument(
        "--exit-on-fail", action="store_true",
        help="Exit with code 2 if any check fails (useful in CI)",
    )
    args = parser.parse_args(argv)

    try:
        result = run_audit(
            mode=args.mode,
            kubeconfig=args.kubeconfig,
            context=args.context,
        )
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 3

    color = not args.no_color and sys.stdout.isatty()
    if not args.quiet:
        print(render_terminal(result, color=color))

    html = render_html(result)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    if not args.quiet:
        print(f"HTML report written to: {args.output}")

    if args.exit_on_fail:
        from .runner import Severity
        return 2 if result.by_severity(Severity.FAIL) else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())

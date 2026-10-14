#!/usr/bin/env python
"""Parse pasted Registration Cart "Choose an envelope" text into envelope JSON.

Reads the verbatim pasted text (one title line per program, blank lines
between programs) and writes ``data/envelopes/<term>/<slug>.json`` for each
program, ready to be committed and loaded at startup.

    python scripts/import_cart_text.py data/envelopes/raw/20271.txt --term 20271

The parser is deterministic and pure (no network, no DB): it anchors on
``Section NNN`` lines and records instructor, course code, mode, seats
snapshot, credits, part of term, and one-or-more meeting triples per section.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from backend.envelopes import (  # noqa: E402
    envelope_to_dict,
    json_dir_for_term,
    parse_cart_text,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("raw_file", help="path to the pasted text (e.g. data/envelopes/raw/20271.txt)")
    parser.add_argument("--term", required=True, help="BYU year-term code (e.g. 20271)")
    parser.add_argument("--captured-at", default=date.today().isoformat(),
                        help="ISO capture date (default: today)")
    args = parser.parse_args()

    text = open(args.raw_file, encoding="utf-8").read()
    envelopes = parse_cart_text(text, args.term, args.captured_at)

    if not envelopes:
        print("No envelopes parsed — is the file the right shape?", file=sys.stderr)
        return 1

    outdir = json_dir_for_term(args.term)
    outdir.mkdir(parents=True, exist_ok=True)

    for env in envelopes:
        path = outdir / f"{env.slug}.json"
        path.write_text(json.dumps(envelope_to_dict(env), indent=2) + "\n",
                        encoding="utf-8")
        for w in env.warnings:
            print(f"warning [{env.slug}]: {w}", file=sys.stderr)

    # Summary table.
    print(f"\nTerm {args.term} — parsed {len(envelopes)} program(s), "
          f"{sum(len(e.bundles) for e in envelopes)} bundle(s)\n")
    width = max(len(e.display_title) for e in envelopes)
    print(f"{'program':<{width}}  {'bundles':>7}  courses")
    print(f"{'-' * width}  {'-' * 7}  -------")
    for env in envelopes:
        courses = sorted({s.course_code for b in env.bundles for s in b.sections})
        print(f"{env.display_title:<{width}}  {len(env.bundles):>7}  {', '.join(courses)}")

    print(f"\nWrote JSON to {outdir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

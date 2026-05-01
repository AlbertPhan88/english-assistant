import argparse
import sys
from pathlib import Path


def cmd_init(args) -> None:
    from . import config, db
    db.init(config.DB_PATH)
    print(f"Database initialised at {config.DB_PATH}")


def cmd_ingest(args) -> None:
    from anthropic import Anthropic
    from . import config, db
    from .extractor import ingest_pdf

    db.init(config.DB_PATH)
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    for path_str in args.pdfs:
        p = Path(path_str)
        if not p.exists():
            print(f"File not found: {p}", file=sys.stderr)
            continue
        added, total = ingest_pdf(p, config.DB_PATH, client)
        print(f"{p.name}: found {total} idioms, added {added} new.")


def cmd_fill_examples(args) -> None:
    from anthropic import Anthropic
    from . import config
    from .examples import fill_missing_examples

    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    filled = fill_missing_examples(config.DB_PATH, client)
    print(f"Generated examples for {filled} idiom(s).")


def cmd_stats(args) -> None:
    from . import config, db

    with db.connect(config.DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM idioms").fetchone()[0]
        reviewed = conn.execute("SELECT COUNT(*) FROM reviews WHERE repetitions > 0").fetchone()[0]
        row = conn.execute("SELECT SUM(correct) as c, SUM(wrong) as w FROM reviews").fetchone()
        c, w = row["c"] or 0, row["w"] or 0
        accuracy = round(c / (c + w) * 100) if (c + w) else 0
    print(f"Idioms: {total} | Reviewed: {reviewed} | Accuracy: {accuracy}% ({c}/{c+w})")


def cmd_run(args) -> None:
    from . import config, db
    from .bot import run

    db.init(config.DB_PATH)
    run(config.DB_PATH)


def main() -> None:
    parser = argparse.ArgumentParser(prog="english-assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create the database")

    p_ingest = sub.add_parser("ingest", help="Ingest a PDF file")
    p_ingest.add_argument("pdfs", nargs="+", metavar="PDF")

    sub.add_parser("fill-examples", help="Generate funny examples for idioms missing one")
    sub.add_parser("stats", help="Print DB stats")
    sub.add_parser("run", help="Start the Telegram bot")

    args = parser.parse_args()
    {
        "init": cmd_init,
        "ingest": cmd_ingest,
        "fill-examples": cmd_fill_examples,
        "stats": cmd_stats,
        "run": cmd_run,
    }[args.command](args)


if __name__ == "__main__":
    main()

"""Command-line entry point: ingest / analyze / export / run."""

from __future__ import annotations

import argparse
import sys

from .analyze import analyze
from .config import Config
from .export import export
from .ingest import ingest
from .store import Store


def _open(args):
    cfg = Config.load(args.config)
    store = Store(cfg.path("db_path"))
    return cfg, store


def cmd_ingest(args):
    cfg, store = _open(args)
    with store:
        print(f"Ingesting from {args.source} ...")
        res = ingest(args.source, cfg, store)
        print(f"Done: {res['added']} added, {res['skipped']} already known.")


def cmd_analyze(args):
    cfg, store = _open(args)
    with store:
        print("Analyzing ...")
        res = analyze(cfg, store)
        print(f"Done: {res['recordings']} recordings, {res['events']} events.")


def cmd_export(args):
    cfg, store = _open(args)
    with store:
        print("Exporting ...")
        export(cfg, store)


def cmd_run(args):
    cfg, store = _open(args)
    with store:
        print(f"[1/3] Ingesting from {args.source} ...")
        res = ingest(args.source, cfg, store)
        print(f"      {res['added']} added, {res['skipped']} already known.")
        print("[2/3] Analyzing ...")
        ares = analyze(cfg, store)
        print(f"      {ares['recordings']} recordings, {ares['events']} events.")
        print("[3/3] Exporting ...")
        export(cfg, store)
        print("All done.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="barkdetect",
                                description="Analyze Zoom H6 MP3s for dog barking.")
    p.add_argument("-c", "--config", default="config.yml", help="path to config.yml")
    sub = p.add_subparsers(dest="command", required=True)

    pi = sub.add_parser("ingest", help="copy + register new MP3s from the SD card")
    pi.add_argument("--source", required=True, help="SD card path or folder, e.g. E:\\")
    pi.set_defaults(func=cmd_ingest)

    pa = sub.add_parser("analyze", help="detect barks in unprocessed recordings")
    pa.set_defaults(func=cmd_analyze)

    pe = sub.add_parser("export", help="regenerate results.json for the frontend")
    pe.set_defaults(func=cmd_export)

    pr = sub.add_parser("run", help="ingest + analyze + export in one go")
    pr.add_argument("--source", required=True, help="SD card path or folder, e.g. E:\\")
    pr.set_defaults(func=cmd_run)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())

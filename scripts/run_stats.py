"""Counts for a saved run: what each chunk produced, without reading any of it.

For every chunk: did it finish, how many items, how long the id lists are, whether the
cited post numbers exist in the chunk, how much of the chunk the brief covers, and
whether the reply repeats itself. Numbers only, so the output is safe to paste.

    python scripts\\run_stats.py                      # the newest run in digests/.runs
    python scripts\\run_stats.py --run qwen-qwen3.5-9b__c0.30__20260920-134443
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, installed or not

from xmd.chunk import parse_export  # noqa: E402
from xmd.runner import compression_label  # noqa: E402
from xmd.verify import chunk_stats, support  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Print counts for a saved run.")
    ap.add_argument("--run", default="", help="run label (default: the newest in DIGESTS/.runs)")
    ap.add_argument("--digests-dir", default="digests")
    ap.add_argument("--show-unsupported", action="store_true",
                    help="also list the items with a figure no cited post contains: prints post-derived text, for your eyes only")
    args = ap.parse_args(argv)

    runs = Path(args.digests_dir) / ".runs"
    if args.run:
        run = runs / args.run
    else:
        found = sorted((p for p in runs.glob("*") if (p / "run.json").exists()), key=lambda p: p.stat().st_mtime)
        if not found:
            print(f"no runs in {runs}")
            return 1
        run = found[-1]
    report(run, show_unsupported=args.show_unsupported)
    return 0


def _post_text(export_dir: Path, name: str) -> dict[int, str]:
    """{post number: the line the model saw} from the frozen export the run used; {} if it isn't there."""
    export = export_dir / name
    mapping = export.with_suffix(".map.json")
    if not export.exists() or not mapping.exists():
        return {}
    posts, _ = parse_export(export.read_text(encoding="utf-8"), json.loads(mapping.read_text(encoding="utf-8")))
    return {p.n: p.text for p in posts}


def _own(record: dict, post_text: dict[int, str]) -> dict[int, str]:
    """Only the posts of this chunk: citing a number from another chunk is citing nothing."""
    return {n: post_text[n] for n in record["ids"] if n in post_text}


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "-"


def _cells(record: dict, post_text: dict[int, str], totals: list[int]) -> str:
    """The facts and names cells of one chunk's row, adding its counts to `totals`."""
    if not post_text or record.get("reply") is None:
        return f"{'-':>8}{'-':>9}"
    found = support(record["reply"]["items"], _own(record, post_text))
    for k, value in enumerate((found.items, found.uncited_items, found.facts, found.unsupported_facts,
                               found.names, found.unsupported_names, found.items_with_unsupported_fact)):
        totals[k] += value
    return f"{found.unsupported_facts}/{found.facts}".rjust(8) + f"{found.unsupported_names}/{found.names}".rjust(9)


def report(run: Path, with_summary: bool = True, show_unsupported: bool = False) -> None:
    """Print the counts for the saved run in `run`."""
    manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
    print(f"{run.name}: {manifest['engine']['model']}, {compression_label(manifest)}, "
          f"prompt {manifest['prompt']['sha256'][:8]}"
          + (", items bounded in the schema" if manifest["engine"].get("bound_items") else "") + "\n")

    print(f"{'chunk':<16}{'ok':>3}{'tries':>6}{'finish':>8}{'out':>6}{'cap':>6} |{'items':>6}{'limits':>7}{'ids/it':>7}"
          f"{'longest':>8}{'real ids':>9}{'covered':>8}{'dup':>5}{'detail w':>9}{'facts':>8}{'names':>9}")
    covered_by_tier: dict[str, list[int]] = {}
    post_text = _post_text(run.parent.parent / ".export", manifest["export"]["file"])
    totals = [0] * 7  # items, uncited, facts, bad facts, names, bad names, items with a bad fact
    for row in manifest["chunks"]:
        record = json.loads((run / f"{row['chunk']}.json").read_text(encoding="utf-8"))
        stats = chunk_stats(record)
        done = record["completion"] or {}
        print(f"{record['chunk']:<16}{'Y' if record['ok'] else 'N':>3}{record['attempts']:>6}{(done.get('finish') or '-'):>8}"
              f"{done.get('completion_tokens') or 0:>6}{record['max_tokens']:>6} |{stats.items:>6}"
              f"{str(record.get('min_items', 0)) + '-' + str(record.get('max_items', 0)):>7}"
              f"{stats.ids_per_item:>7.1f}{stats.max_ids:>8}{stats.in_chunk:>9.0%}{stats.covered:>8.0%}"
              f"{stats.duplicate_headlines:>5}{stats.detail_words:>9.1f}{_cells(record, post_text, totals)}")
        covered_by_tier.setdefault(record["tier"], [0, 0])
        covered_by_tier[record["tier"]][0] += round(stats.covered * len(record["ids"]))
        covered_by_tier[record["tier"]][1] += len(record["ids"])

    print("\nposts cited at least once, by tier (a cut-off reply counts as far as it got):")
    for tier, (cited, total) in covered_by_tier.items():
        print(f"  {tier:<8} {cited:>4} of {total:<4} ({cited / total:.0%})")
    if post_text and totals[0]:
        items, uncited, facts, bad_facts, names, bad_names, bad_items = totals
        print("\nfaithfulness, checked against the posts each item cites (a rough net for invented figures):")
        print(f"  numbers, @handles and $tickers: {bad_facts} of {facts} not in a cited post ({_pct(bad_facts, facts)}); "
              f"{bad_items} of {items} items have at least one")
        print(f"  acronyms and capitalized names: {bad_names} of {names} not in a cited post ({_pct(bad_names, names)})")
        print(f"  items that cite no post of their chunk at all: {uncited}")
    if show_unsupported and post_text:
        print("\nitems with a figure no cited post contains (text below comes from the posts: do not paste it):")
        for row in manifest["chunks"]:
            record = json.loads((run / f"{row['chunk']}.json").read_text(encoding="utf-8"))
            if record.get("reply") is None:
                continue
            for index, tokens in support(record["reply"]["items"], _own(record, post_text)).flagged:
                item = record["reply"]["items"][index]
                print(f"  {record['chunk']} item {index}: {', '.join(tokens)} not found | {item['headline']} | {item['detail']} | cites {item['ids']}")
    if with_summary:
        summary = manifest["summary"]
        print(f"\n{summary['ok']}/{summary['chunks']} chunks ok, {summary['seconds'] / 60:.1f} min of model time, "
              f"reply {summary['reply_words']:,} words against a target of {summary['target_words']:,}")


if __name__ == "__main__":
    sys.exit(main())

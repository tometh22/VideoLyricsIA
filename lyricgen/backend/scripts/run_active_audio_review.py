"""Run a private listening pilot, or preflight it without provider calls."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from active_audio_review import review, validate
from reviewer_shadow_audio import file_sha, private_write, probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--providers", nargs="+", choices=["google", "openai"], default=["google", "openai"])
    parser.add_argument("--max-calls", type=int, default=48)
    parser.add_argument("--max-focus-lines", type=int, default=6)
    parser.add_argument("--acoustic-timing", action="store_true", help="Investigate timing after independent text support")
    parser.add_argument("--timing-only", action="store_true", help="Conditional CTC/pitch probe, no recognition provider calls")
    parser.add_argument("--line-indices", nargs="+", type=int, help="Zero-based lines for a conditional timing probe (max 6)")
    parser.add_argument("--execute", action="store_true", help="Make bounded provider calls; default is preflight only")
    args = parser.parse_args()
    if args.timing_only and (not args.line_indices or len(args.line_indices) > 6
                            or len(set(args.line_indices)) != len(args.line_indices)):
        parser.error("--timing-only requires 1 to 6 unique --line-indices")
    snapshot = json.loads(args.snapshot.read_text())
    duration = probe(args.audio)
    validate(snapshot, duration)
    if file_sha(args.audio) != snapshot["audio_sha256"]:
        raise ValueError("audio_identity_changed")
    credentials = {"google": bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
                                  and Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]).is_file()),
                   "openai": bool(os.getenv("OPENAI_API_KEY"))}
    missing = [] if args.timing_only else [p for p in args.providers if not credentials[p]]
    if not args.execute:
        print(json.dumps({"status": "PREFLIGHT", "duration": duration,
                          "lines": len(snapshot["segments"]), "missing_provider_credentials": missing,
                          "max_calls": args.max_calls, "automatic_apply_allowed": False}))
        return
    if missing:
        raise ValueError("missing_provider_credentials:" + ",".join(missing))
    # Exclusive output prevents overwriting a prior run; cache is resumable.
    if (args.output / "report.json").exists():
        raise ValueError("report_exists_choose_new_output_directory")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.timing_only:
        from active_timing_review import investigate
        from full_audio_review import binding
        report = {"schema_version": "active-timing-probe-v1", "binding": binding(snapshot),
                  "status": "CONDITIONAL_PROBE", "calls": 0, "automatic_apply_allowed": False,
                  "text_independently_verified": False,
                  "lines": [investigate(args.audio, snapshot["segments"], i, duration, conditional=True)
                            for i in args.line_indices]}
        private_write(args.output / "report.json", report)
        print(json.dumps({"status": report["status"], "calls": 0,
                          "results": [{"line_index": r["line_index"], "status": r["status"],
                                       "reason": r["reason"]} for r in report["lines"]],
                          "report": str(args.output / "report.json")}))
        return
    report = review(snapshot, args.audio, args.output / "cache", providers=tuple(args.providers),
                    max_calls=args.max_calls, max_focus_lines=args.max_focus_lines,
                    acoustic_timing=args.acoustic_timing)
    private_write(args.output / "report.json", report)
    print(json.dumps({"status": report["status"], "calls": report["calls"],
                      "coverage": report["coverage"], "focus_candidates": report["focus_candidates"],
                      "proposals": sum(r["text"]["status"] == "TEXT_PROPOSAL" for r in report["lines"]),
                      "report": str(args.output / "report.json"), "automatic_apply_allowed": False}))


if __name__ == "__main__":
    main()

"""
test_pipeline.py — Run the reevaluate-preconditions pipeline.

Usage:
    python3 test_pipeline.py --predicted <final_state.json> --manifest <manifest.json>
    python3 test_pipeline.py <input_directory>  (looks for *output*.json + *manifest*.json)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from agent import agent


def _load_predicted(path: Path) -> str:
    """Load predicted-conditions output, handling final_state format."""
    raw = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
        # If it's a final_state, the tool will extract final_output internally
        return raw
    except json.JSONDecodeError:
        return raw


def _find_file(directory: Path, keywords: list[str]) -> Path | None:
    for f in sorted(directory.iterdir()):
        if f.suffix == ".json" and any(kw in f.name.lower() for kw in keywords):
            return f
    return None


def run(predicted_path: Path, manifest_path: Path, output_dir: Path | None = None) -> dict:
    predicted_raw = predicted_path.read_text(encoding="utf-8")
    manifest_raw = manifest_path.read_text(encoding="utf-8")
    source_label = predicted_path.stem

    initial_state = {
        "predicted_conditions_json": predicted_raw,
        "updated_manifest_json": manifest_raw,
        "env": "Test",
        "current_step": "STEP_00",
    }

    provider = os.environ.get("LLM_PROVIDER", "openai").lower()
    if provider == "openai":
        model_name = os.environ.get("OPENAI_MODEL", "gpt-4o")
    else:
        model_name = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    print("=" * 70)
    print("  Reevaluate Preconditions Pipeline")
    print("=" * 70)
    print(f"  Provider: {provider}")
    print(f"  Model: {model_name}")
    print(f"  Predicted: {predicted_path.name}")
    print(f"  Manifest: {manifest_path.name}")
    print("=" * 70)
    sys.stdout.flush()

    config = {"recursion_limit": 50}
    tool_call_count = 0
    start_time = time.time()
    accumulated_state: dict = {}

    for event in agent.stream(initial_state, config=config, stream_mode="values"):
        accumulated_state = event
        msgs = event.get("messages", [])
        if msgs:
            last_msg = msgs[-1]
            if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
                for tc in last_msg.tool_calls:
                    tool_call_count += 1
                    args_preview = json.dumps(tc.get("args", {}), default=str)
                    if len(args_preview) > 200:
                        args_preview = args_preview[:200] + "..."
                    print(f"\n  [{tool_call_count}] {tc['name']}({args_preview})")
            elif hasattr(last_msg, "content") and last_msg.content and not hasattr(last_msg, "tool_call_id"):
                content = last_msg.content if isinstance(last_msg.content, str) else str(last_msg.content)
                if content.strip() and len(content) > 0:
                    preview = content[:200] + "..." if len(content) > 200 else content
                    print(f"\n  LLM: {preview}")
        sys.stdout.flush()

    elapsed = time.time() - start_time
    print(f"\n{'=' * 70}")
    print(f"  Done! ({elapsed:.1f}s, {tool_call_count} tool calls)")
    print("=" * 70)

    final_output = accumulated_state.get("final_output")
    document_requests = (final_output or {}).get("document_requests", []) or accumulated_state.get("document_requests", [])

    # Print results
    total_satisfied = sum(len(dr.get("display", {}).get("satisfied_requirements", [])) for dr in document_requests)
    total_remaining = sum(len(dr.get("display", {}).get("documentation_requirements", [])) for dr in document_requests)
    print(f"\n  Results: {len(document_requests)} document requests")
    print(f"  Satisfied requirements: {total_satisfied}")
    print(f"  Remaining requirements: {total_remaining}")
    print()
    for dr in document_requests:
        display = dr.get("display", {})
        sat = len(display.get("satisfied_requirements", []))
        rem = len(display.get("documentation_requirements", []))
        status = dr.get("status", "?")
        print(f"    {dr.get('document_type', '?')} [{status}] — {sat} satisfied, {rem} remaining")

    # Save output
    result = final_output or {
        "document_requests": document_requests,
        "stats": {"total_satisfied": total_satisfied, "total_remaining": total_remaining},
    }

    dest = output_dir or Path(".")
    dest.mkdir(parents=True, exist_ok=True)
    output_file = dest / f"{source_label}_reevaluated.json"
    with open(output_file, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\n  Saved to {output_file}")

    return result


def main():
    args = sys.argv[1:]
    predicted_path = None
    manifest_path = None
    output_dir = None

    if "--predicted" in args:
        idx = args.index("--predicted")
        predicted_path = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if "--manifest" in args:
        idx = args.index("--manifest")
        manifest_path = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if "--output-dir" in args:
        idx = args.index("--output-dir")
        output_dir = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if not predicted_path and args:
        directory = Path(args[0])
        predicted_path = _find_file(directory, ["output", "predicted", "final_state"])
        manifest_path = manifest_path or _find_file(directory, ["manifest"])

    if not predicted_path or not manifest_path:
        print("Usage:")
        print("  python3 test_pipeline.py --predicted <file.json> --manifest <manifest.json>")
        print("  python3 test_pipeline.py <directory>")
        sys.exit(1)

    run(predicted_path, manifest_path, output_dir)


if __name__ == "__main__":
    main()

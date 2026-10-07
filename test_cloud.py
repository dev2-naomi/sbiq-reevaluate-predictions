"""
test_cloud.py — Test the LangGraph Cloud deployment of reevaluate-preconditions.

Usage:
    python3 test_cloud.py --predicted <file.json> --manifest <manifest.json>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

LANGGRAPH_URL = os.environ.get(
    "REEVALUATE_LANGGRAPH_URL",
    "https://sbiq-reevaluate-predictions-8af10a300ebf5186bf271ec255a3f2cb.us.langgraph.app"
)
LANGSMITH_API_KEY = os.environ.get("LANGCHAIN_API_KEY", "")


def run_cloud_test(predicted_path: Path, manifest_path: Path):
    from langgraph_sdk import get_client

    client = get_client(
        url=LANGGRAPH_URL,
        api_key=LANGSMITH_API_KEY,
    )

    predicted_raw = predicted_path.read_text(encoding="utf-8")
    manifest_raw = manifest_path.read_text(encoding="utf-8")

    print("=" * 70)
    print("  Reevaluate Preconditions — Cloud Test")
    print("=" * 70)
    print(f"  URL: {LANGGRAPH_URL}")
    print(f"  Predicted: {predicted_path.name}")
    print(f"  Manifest: {manifest_path.name}")
    print("=" * 70)
    sys.stdout.flush()

    # Create thread
    import asyncio

    async def _run():
        thread = await client.threads.create()
        print(f"\n  Thread ID: {thread['thread_id']}")

        # Start run
        run = await client.runs.create(
            thread_id=thread["thread_id"],
            assistant_id="reevaluate-preconditions",
            input={
                "predicted_conditions_json": predicted_raw,
                "updated_manifest_json": manifest_raw,
            },
        )
        print(f"  Run ID: {run['run_id']}")
        print("  Waiting for completion...")
        sys.stdout.flush()

        # Poll for completion
        start = time.time()
        while True:
            run_status = await client.runs.get(thread_id=thread["thread_id"], run_id=run["run_id"])
            status = run_status.get("status", "unknown")

            if status in ("success", "completed"):
                elapsed = time.time() - start
                print(f"  Completed in {elapsed:.1f}s")
                break
            elif status in ("error", "failed"):
                print(f"  FAILED: {run_status}")
                return None
            else:
                time.sleep(3)

        # Get final state
        state = await client.threads.get_state(thread_id=thread["thread_id"])
        return state

    state = asyncio.run(_run())

    if not state:
        return None

    values = state.get("values", {})

    # Debug: save full state
    debug_file = Path("test_results") / "cloud_state_debug.json"
    debug_file.parent.mkdir(exist_ok=True)
    debug_values = {}
    for k, v in values.items():
        if k == "messages":
            debug_values[k] = f"({len(v)} messages)"
        elif k in ("predicted_conditions_json", "updated_manifest_json"):
            debug_values[k] = f"(string, {len(v)} chars)"
        else:
            debug_values[k] = v
    with open(debug_file, "w") as f:
        json.dump(debug_values, f, indent=2, default=str)
    print(f"\n  State keys: {list(values.keys())}")
    print(f"  document_requests type: {type(values.get('document_requests'))}, len: {len(values.get('document_requests', []))}")
    print(f"  final_output type: {type(values.get('final_output'))}")
    if values.get("final_output"):
        fo = values["final_output"]
        print(f"  final_output keys: {list(fo.keys()) if isinstance(fo, dict) else fo[:200]}")

    final_output = values.get("final_output")

    if final_output:
        document_requests = final_output.get("document_requests", [])
        stats = final_output.get("stats", {})
    else:
        document_requests = values.get("document_requests", [])
        stats = {}

    print(f"\n  Results: {len(document_requests)} document requests")
    total_satisfied = sum(len(dr.get("display", {}).get("satisfied_requirements", [])) for dr in document_requests)
    total_remaining = sum(len(dr.get("display", {}).get("documentation_requirements", [])) for dr in document_requests)
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
    output_file = Path("test_results") / "cloud_reevaluated.json"
    output_file.parent.mkdir(exist_ok=True)
    result = final_output or {"document_requests": document_requests, "stats": stats}
    with open(output_file, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\n  Saved to {output_file}")

    return result


def main():
    args = sys.argv[1:]
    predicted_path = None
    manifest_path = None

    if "--predicted" in args:
        idx = args.index("--predicted")
        predicted_path = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if "--manifest" in args:
        idx = args.index("--manifest")
        manifest_path = Path(args[idx + 1])
        args = args[:idx] + args[idx + 2:]

    if not predicted_path or not manifest_path:
        print("Usage: python3 test_cloud.py --predicted <file.json> --manifest <manifest.json>")
        sys.exit(1)

    run_cloud_test(predicted_path, manifest_path)


if __name__ == "__main__":
    main()

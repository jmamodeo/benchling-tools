"""Interactive, opt-in concurrency and table-write exercise for a Benchling sandbox.

The named environment must already exist. Run ``benchling-tools auth login --env NAME``
in your own terminal first; this script never reads a client secret.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from typing import Any

from benchling_tools.errors import ConflictError
from benchling_tools.runtime import Runtime


def comparable_table(table: dict[str, Any], row: int, column: int) -> dict[str, Any]:
    """Remove expected evaluator changes and the one intentionally edited value."""
    content = deepcopy(table.get("content") or {})
    content.pop("__typename", None)
    content.pop("isEvaluated", None)
    content["columnDefinitions"] = [
        {key: value for key, value in item.items() if key in {"name", "width", "isLocked"}}
        for item in content.get("columnDefinitions") or []
    ]
    supported = {
        "inputValue",
        "format",
        "mergeRange",
        "mergeRangeParent",
        "selectorId",
    }
    for row_index, item in enumerate(content.get("rows") or []):
        for column_index, cell in enumerate(item.get("cells") or []):
            for key in list(cell):
                if key not in supported:
                    cell.pop(key, None)
            if (row_index, column_index) == (row, column):
                cell.pop("inputValue", None)
    return content


async def run(args: argparse.Namespace) -> None:
    """Run the interactive exercise; exits via SystemExit if any verification step fails."""
    runtime = Runtime(args.env)
    try:
        target = await runtime.service.get_table_for_entry(args.entry, args.table_part)
        table = target["table"]
        try:
            _ = table["content"]["rows"][args.row]["cells"][args.column]["inputValue"]["value"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SystemExit("The selected cell is not an editable text-value cell") from exc

        specification = {
            "operation": "set_cells",
            "table_part_id": args.table_part,
            "updates": [{"row": args.row, "column": args.column, "value": args.value}],
        }
        stale = await runtime.planner.plan(args.entry, "table", specification)
        print(json.dumps({"stale_plan_id": stale.id, "diff": stale.diff}, indent=2))
        input(
            "Edit this sandbox entry in the Benchling browser, wait for it to save, "
            "then press Enter. "
        )
        try:
            await runtime.planner.apply(stale.id)
        except ConflictError:
            print("Stale plan correctly rejected.")
        else:
            raise SystemExit(
                "The stale plan was not rejected. Stop and inspect the sandbox entry; "
                "the browser edit may not have saved."
            )

        before = (await runtime.service.get_table_for_entry(args.entry, args.table_part))["table"]
        original = before["content"]["rows"][args.row]["cells"][args.column]["inputValue"]["value"]
        fresh = await runtime.planner.plan(args.entry, "table", specification)
        print(json.dumps({"fresh_plan_id": fresh.id, "diff": fresh.diff}, indent=2))
        if input("Type APPLY to commit this sandbox write: ").strip() != "APPLY":
            raise SystemExit("Fresh plan was not applied.")
        await runtime.planner.apply(fresh.id)
        after = (await runtime.service.get_table_for_entry(args.entry, args.table_part))["table"]
        actual = after["content"]["rows"][args.row]["cells"][args.column]["inputValue"]["value"]
        if actual != args.value:
            raise SystemExit(f"Target cell mismatch: expected {args.value!r}, got {actual!r}")
        if comparable_table(before, args.row, args.column) != comparable_table(
            after, args.row, args.column
        ):
            raise SystemExit("An unrelated table input or supported cell property changed.")
        print("Fresh write verified: only the selected input value changed.")

        if args.restore:
            restore = await runtime.planner.plan(
                args.entry,
                "table",
                {
                    **specification,
                    "updates": [{"row": args.row, "column": args.column, "value": original}],
                },
            )
            if input("Type RESTORE to restore the original cell value: ").strip() != "RESTORE":
                raise SystemExit(f"Original value was not restored: {original!r}")
            await runtime.planner.apply(restore.id)
            print("Original cell input restored.")
    finally:
        await runtime.close()


def main() -> None:
    """Parse arguments and run the exercise; row and column must be nonnegative."""
    parser = argparse.ArgumentParser(
        description="Exercise stale-plan rejection and a targeted write in a sandbox entry."
    )
    parser.add_argument("--env", required=True)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--table-part", required=True)
    parser.add_argument("--row", type=int, required=True)
    parser.add_argument("--column", type=int, required=True)
    parser.add_argument("--value", required=True)
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    if args.row < 0 or args.column < 0:
        parser.error("--row and --column must be nonnegative")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

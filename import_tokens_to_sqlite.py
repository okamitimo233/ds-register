import argparse
import sys
from pathlib import Path

from core.local_tokens import import_legacy_json_tokens


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将 data/tokens 下的历史 JSON token 一次性导入到 data/local_tokens.db",
    )
    parser.add_argument(
        "--source-dir",
        default="data/tokens",
        help="历史 JSON token 目录，默认 data/tokens",
    )
    parser.add_argument(
        "--show-errors",
        type=int,
        default=20,
        help="最多输出多少条失败明细，默认 20",
    )
    parser.add_argument(
        "--delete-imported-json",
        action="store_true",
        help="导入成功后删除对应的历史 JSON 文件；失败文件不会删除",
    )
    args = parser.parse_args()

    result = import_legacy_json_tokens(
        Path(args.source_dir),
        delete_imported=bool(args.delete_imported_json),
    )

    print(f"source_dir: {result['source_dir']}")
    print(f"db_path: {result['db_path']}")
    print(f"total: {result['total']}")
    print(f"imported: {result['imported']}")
    print(f"failed: {result['failed']}")
    if result.get("delete_requested"):
        print(f"deleted: {result['deleted']}")
        print(f"delete_failed: {result['delete_failed']}")

    if result["failed"]:
        print("failed_files:")
        shown = 0
        for item in result.get("results") or []:
            if not item.get("ok"):
                print(f"  - {item.get('filename')}: {item.get('error')}")
                shown += 1
                if shown >= max(0, int(args.show_errors or 0)):
                    break
    if result.get("delete_failed"):
        print("delete_failed_files:")
        shown = 0
        for item in result.get("results") or []:
            if item.get("ok") and item.get("deleted") is False:
                print(f"  - {item.get('filename')}: {item.get('delete_error')}")
                shown += 1
                if shown >= max(0, int(args.show_errors or 0)):
                    break

    if result["failed"] or result.get("delete_failed"):
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

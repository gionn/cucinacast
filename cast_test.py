#!/usr/bin/env python3
"""CLI entry point to search YouTube and cast the top result to a Nest Mini."""
import sys

from castyt import player


def main():
    if len(sys.argv) < 2:
        print("usage: cast_test.py <search query>", file=sys.stderr)
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    title, url = player.play_search(query)
    print(f"Found: {title} ({url})")
    print("Casting started.")


if __name__ == "__main__":
    main()

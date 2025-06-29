#!/usr/bin/env python3
"""
Convenience script for running Alembic migrations.

Usage:
    python migrate.py current           # Show current migration
    python migrate.py upgrade head     # Apply all migrations
    python migrate.py downgrade -1     # Downgrade one migration
    python migrate.py revision -m "Description"  # Create new migration (--autogenerate is automatic)
    python migrate.py history          # Show migration history

Note: The 'revision' command automatically includes --autogenerate unless already specified.
"""

import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)


def main() -> None:
    """Run alembic command with the correct config file."""
    # Get the directory where this script is located
    script_dir = Path(__file__).parent
    config_path = script_dir / "migrations" / "alembic.ini"

    # Build the alembic command
    args = sys.argv[1:]

    # If the command is 'revision', automatically add --autogenerate
    if args and args[0] == "revision":
        # Check if --autogenerate is already present
        if "--autogenerate" not in args:
            # Insert --autogenerate after 'revision'
            args.insert(1, "--autogenerate")

    cmd = ["uv", "run", "alembic", "-c", str(config_path)] + args

    # Run the command
    try:
        result = subprocess.run(cmd, check=False)
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Error running migration command: {e}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    main()

"""Startup script for debug shell."""

import asyncio  # noqa: F401
from pprint import pprint  # noqa: F401

from app.debug import DebugUtils, quick_debug  # noqa: F401

print("\n" + "=" * 60)
print("Nolas Debug Shell (async-enabled)")
print("=" * 60)
print("\n✓ Pre-loaded: DebugUtils, quick_debug, asyncio, pprint")
print("\nQuick start:")
print("  >>> debug = DebugUtils()")
print("  >>> await debug.init()")
print("  >>> account = await debug.get_account_by_email('user@example.com')")
print("  >>> folders = await debug.list_folders(account.uuid)")
print("  >>> messages = await debug.list_messages(account.uuid, 'INBOX', limit=10)")
print("  >>> await debug.close()")
print("=" * 60)
print()

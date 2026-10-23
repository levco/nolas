def folder_attributes(name: str) -> list[str]:
    """Fallback for providers that don't supply special-use folder flags."""
    attribute = {
        "inbox": "Inbox",
        "archive": "Archive",
        "all": "Archive",
        "all mail": "Archive",
        "draft": "Drafts",
        "drafts": "Drafts",
        "junk": "Junk",
        "junk email": "Junk",
        "junk e-mail": "Junk",
        "spam": "Junk",
        "sent": "Sent",
        "sent items": "Sent",
        "sent mail": "Sent",
        "trash": "Trash",
        "deleted": "Trash",
        "deleted items": "Trash",
    }.get(name.lower().removeprefix("\\"))
    return [f"\\{attribute}"] if attribute else []

from collections import defaultdict

from app.api.payloads.messages import EmailAddress, Message
from app.api.payloads.threads import Thread
from app.controllers.providers.base import ListThreadsParams


def build_threads_from_messages(messages: list[Message]) -> list[Thread]:
    grouped: dict[str, list[Message]] = defaultdict(list)
    for message in messages:
        grouped[message.thread_id].append(message)

    threads: list[Thread] = []
    for thread_messages in grouped.values():
        ordered = sorted(thread_messages, key=lambda message: message.date, reverse=True)
        latest = ordered[0]
        earliest = ordered[-1]

        participants_by_email: dict[str, EmailAddress] = {}
        folders: dict[str, None] = {}
        for message in ordered:
            for address in [*message.from_, *message.to, *message.cc, *message.bcc]:
                key = address.email.lower()
                if key not in participants_by_email:
                    participants_by_email[key] = address
            for folder in message.folders:
                folders.setdefault(folder, None)

        threads.append(
            Thread(
                id=latest.thread_id,
                grant_id=latest.grant_id,
                subject=latest.subject,
                snippet=latest.snippet,
                participants=list(participants_by_email.values()),
                message_ids=[message.id for message in ordered],
                folders=list(folders),
                # Draft support can replace this later; for now mirror Nylas shape
                # with the latest non-draft message in the thread.
                latest_draft_or_message=latest,
                has_attachments=any(bool(message.attachments) for message in ordered),
                starred=any(message.starred for message in ordered),
                unread=any(message.unread for message in ordered),
                latest_message_received_date=latest.date,
                earliest_message_date=earliest.date,
            )
        )

    return sorted(threads, key=lambda thread: thread.latest_message_received_date, reverse=True)


def filter_threads(threads: list[Thread], messages: list[Message], params: ListThreadsParams) -> list[Thread]:
    message_groups: dict[str, list[Message]] = defaultdict(list)
    for message in messages:
        message_groups[message.thread_id].append(message)

    filtered = threads
    if params.in_:
        filtered = [
            thread
            for thread in filtered
            if any(params.in_ in message.folders for message in message_groups.get(thread.id, []))
        ]
    if params.from_:
        expected = params.from_.lower()
        filtered = [
            thread
            for thread in filtered
            if any(
                any(address.email.lower() == expected for address in message.from_)
                for message in message_groups.get(thread.id, [])
            )
        ]
    if params.to:
        expected = params.to.lower()
        filtered = [
            thread
            for thread in filtered
            if any(
                any(address.email.lower() == expected for address in message.to)
                for message in message_groups.get(thread.id, [])
            )
        ]
    if params.cc:
        expected = params.cc.lower()
        filtered = [
            thread
            for thread in filtered
            if any(
                any(address.email.lower() == expected for address in message.cc)
                for message in message_groups.get(thread.id, [])
            )
        ]
    if params.bcc:
        expected = params.bcc.lower()
        filtered = [
            thread
            for thread in filtered
            if any(
                any(address.email.lower() == expected for address in message.bcc)
                for message in message_groups.get(thread.id, [])
            )
        ]
    if params.any_email:
        wanted = {email.lower() for email in params.any_email}
        filtered = [
            thread
            for thread in filtered
            if any(participant.email.lower() in wanted for participant in thread.participants)
        ]
    if params.subject:
        filtered = [thread for thread in filtered if params.subject in thread.subject]
    if params.latest_message_after is not None:
        filtered = [
            thread for thread in filtered if thread.latest_message_received_date >= params.latest_message_after
        ]
    if params.latest_message_before is not None:
        filtered = [
            thread for thread in filtered if thread.latest_message_received_date <= params.latest_message_before
        ]
    if params.unread is not None:
        filtered = [thread for thread in filtered if thread.unread is params.unread]
    if params.starred is not None:
        filtered = [thread for thread in filtered if thread.starred is params.starred]
    if params.has_attachment is not None:
        filtered = [thread for thread in filtered if thread.has_attachments is params.has_attachment]
    return filtered

import asyncio

import pytest

from experiments.lan_notice_loop import NOTICE, NoticeLoop


class Mailbox:
    def __init__(self, size=2):
        self.ids = []
        self.size = size
        self.calls = []
        self.fail = False

    def read(self, after_id):
        self.calls.append(after_id)
        if self.fail:
            raise OSError("private content must not be logged")
        rows = [i for i in self.ids if i > after_id]
        return {"messages": [{"message_id": i, "body": "secret body"}
                             for i in rows[:self.size]],
                "more": len(rows) > self.size}


def test_empty_batch_pagination_ack_and_new_mail():
    async def run():
        box = Mailbox()
        notices = []

        async def notify(message):
            notices.append(message)
            return True

        loop = NoticeLoop(box.read, notify)
        assert not await loop.poll_once()
        box.ids = [1, 2, 3, 4, 5]
        assert await loop.poll_once()
        assert box.calls[-3:] == [0, 2, 4]
        assert notices == [NOTICE] and "secret body" not in notices[0]
        assert not await loop.poll_once()
        box.ids = [4, 5]  # external reader acknowledged others
        assert not await loop.poll_once()
        box.ids.append(6)
        assert await loop.poll_once()
        assert notices == [NOTICE, NOTICE]
        box.ids.clear()
        assert not await loop.poll_once()

    asyncio.run(run())


def test_read_failure_does_not_lose_notification():
    async def run():
        box = Mailbox()
        box.ids = [1]
        notices = []

        async def notify(message):
            notices.append(message)
            return True

        loop = NoticeLoop(box.read, notify)
        box.fail = True
        with pytest.raises(OSError):
            await loop.poll_once()
        box.fail = False
        assert await loop.poll_once()
        assert len(notices) == 1

    asyncio.run(run())


def test_unknown_result_is_delayed_known_failure_retries_sooner():
    async def run():
        box = Mailbox()
        box.ids = [1]
        now = [0.0]
        outcomes = [TimeoutError(), False, True]
        notices = []

        async def notify(message):
            notices.append(message)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        loop = NoticeLoop(box.read, notify, clock=lambda: now[0])
        assert not await loop.poll_once()
        now[0] = 59
        assert not await loop.poll_once()
        assert len(notices) == 1
        now[0] = 60
        assert not await loop.poll_once()
        now[0] = 64
        assert not await loop.poll_once()
        now[0] = 65
        assert await loop.poll_once()
        assert len(notices) == 3

    asyncio.run(run())


def test_bad_pagination_never_notifies():
    async def run():
        notices = []

        async def notify(message):
            notices.append(message)
            return True

        for page in ({"messages": [], "more": True},
                     {"messages": [{"message_id": 2}, {"message_id": 1}], "more": False},
                     {"messages": [], "more": "false"}):
            loop = NoticeLoop(lambda after: page, notify)
            with pytest.raises(ValueError):
                await loop.poll_once()
        assert notices == []

    asyncio.run(run())


def test_simultaneous_polls_are_coalesced():
    async def run():
        box = Mailbox()
        box.ids = [1]
        calls = []

        async def notify(message):
            calls.append(message)
            await asyncio.sleep(0)
            return True

        loop = NoticeLoop(box.read, notify)
        results = await asyncio.gather(loop.poll_once(), loop.poll_once())
        assert results == [True, False]
        assert calls == [NOTICE]

    asyncio.run(run())


def test_run_recovers_from_scan_error_and_stops():
    async def run():
        box = Mailbox()
        box.ids = [1]
        box.fail = True
        notices = []
        stop = asyncio.Event()

        async def notify(message):
            notices.append(message)
            stop.set()
            return True

        async def recover():
            await asyncio.sleep(0.03)
            box.fail = False

        loop = NoticeLoop(box.read, notify)
        await asyncio.wait_for(asyncio.gather(loop.run(stop, interval=0.01), recover()), 2)
        assert notices == [NOTICE]

    asyncio.run(run())


def test_accepted_reminders_stop_at_limit_and_ack_clears_status():
    async def run():
        box = Mailbox()
        box.ids = [1]
        now = [0.0]
        calls = []

        async def notify(message):
            calls.append(message)
            return True

        loop = NoticeLoop(box.read, notify, clock=lambda: now[0])
        assert await loop.poll_once()
        assert loop.status() == {"tracked_unread": 1, "exhausted": 0}
        now[0] = 299
        assert not await loop.poll_once()
        now[0] = 300
        assert await loop.poll_once()
        now[0] = 600
        assert await loop.poll_once()
        assert loop.status() == {"tracked_unread": 1, "exhausted": 1}
        now[0] = 5000
        assert not await loop.poll_once()
        assert len(calls) == 3
        box.ids.clear()
        assert not await loop.poll_once()
        assert loop.status() == {"tracked_unread": 0, "exhausted": 0}

    asyncio.run(run())


def test_ack_prevents_reminder_and_new_mail_gets_own_attempts():
    async def run():
        box = Mailbox()
        box.ids = [1]
        now = [0.0]
        calls = []

        async def notify(message):
            calls.append(message)
            return True

        loop = NoticeLoop(box.read, notify, clock=lambda: now[0])
        assert await loop.poll_once()
        box.ids = [2]  # first was acknowledged, second is fresh
        now[0] = 1
        assert await loop.poll_once()
        now[0] = 300
        assert not await loop.poll_once()
        now[0] = 301
        assert await loop.poll_once()
        assert calls == [NOTICE, NOTICE, NOTICE]
        box.ids.clear()
        now[0] = 1000
        assert not await loop.poll_once()

    asyncio.run(run())


@pytest.mark.parametrize("outcome,delay", [(False, 5), (TimeoutError(), 60)])
def test_failed_or_unknown_attempts_are_bounded(outcome, delay):
    async def run():
        box = Mailbox()
        box.ids = [1]
        now = [0.0]
        calls = []

        async def notify(message):
            calls.append(message)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        loop = NoticeLoop(box.read, notify, clock=lambda: now[0])
        for step in range(3):
            now[0] = step * delay
            assert not await loop.poll_once()
        assert loop.status() == {"tracked_unread": 1, "exhausted": 1}
        now[0] = 100000
        assert not await loop.poll_once()
        assert len(calls) == 3

    asyncio.run(run())


def test_restart_resets_limit_and_state_is_explicitly_not_durable():
    async def run():
        box = Mailbox()
        box.ids = [1]
        calls = []

        async def notify(message):
            calls.append(message)
            return True

        assert await NoticeLoop(box.read, notify).poll_once()
        assert await NoticeLoop(box.read, notify).poll_once()
        assert len(calls) == 2

    asyncio.run(run())

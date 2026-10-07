"""Tests for exact L3 queues, parity, and the replay cursor."""

import unittest

from eth_abi import encode as abi_encode

from l3_vertical_slice import (
    L3Book,
    ReplayCursor,
    ReplayEvent,
    parity_report,
)
from rebuild_from_chain import EVENT_SPECS, ReconstructionError


OWNER = "0x0000000000000000000000000000000000000123"


class L3BookTests(unittest.TestCase):
    def test_append_cancel_and_fill_keep_exact_links(self) -> None:
        book = L3Book()
        book.append(1, OWNER, 100, 50, True)
        book.append(2, OWNER, 200, 50, True)
        book.append(3, OWNER, 300, 50, True)

        self.assertEqual(book.queue(True, 50), [1, 2, 3])
        self.assertEqual((book.levels[(True, 50)].head, book.levels[(True, 50)].tail), (1, 3))
        self.assertEqual((book.orders[2].prev, book.orders[2].next), (1, 3))

        book.remove(2)
        self.assertEqual(book.queue(True, 50), [1, 3])
        self.assertEqual((book.orders[1].next, book.orders[3].prev), (3, 1))

        self.assertEqual(book.set_size(1, 40, require_head=True), 60)
        self.assertEqual(book.levels[(True, 50)].total_size, 340)
        book.set_size(1, 0, require_head=True)
        self.assertEqual((book.levels[(True, 50)].head, book.levels[(True, 50)].tail), (3, 3))
        book.check_invariants()

    def test_trade_must_target_queue_head(self) -> None:
        book = L3Book()
        book.append(1, OWNER, 100, 50, False)
        book.append(2, OWNER, 100, 50, False)

        with self.assertRaisesRegex(ReconstructionError, "not queue head"):
            book.set_size(2, 0, require_head=True)

    def test_invariants_find_broken_previous_link(self) -> None:
        book = L3Book()
        book.append(1, OWNER, 100, 50, True)
        book.append(2, OWNER, 100, 50, True)
        book.orders[2] = book.orders[2].__class__(
            **{**book.orders[2].__dict__, "prev": 0}
        )

        with self.assertRaisesRegex(ReconstructionError, "prev"):
            book.check_invariants()


class CursorAndParityTests(unittest.TestCase):
    def order_created(self, block: int, order_id: int) -> ReplayEvent:
        return ReplayEvent(
            block_number=block,
            transaction_index=0,
            log_index=order_id,
            transaction_hash="0x01",
            name="OrderCreated",
            args={
                "order_id": order_id,
                "owner": OWNER,
                "size": 100,
                "price": 50,
                "is_buy": True,
            },
            raw={},
        )

    def test_cursor_replays_each_event_once(self) -> None:
        book = L3Book()
        cursor = ReplayCursor([self.order_created(10, 1), self.order_created(11, 2)])

        self.assertEqual(cursor.replay_through(book, 10, 1), 1)
        self.assertEqual(cursor.position, (10, 0, 1))
        self.assertEqual(cursor.replay_through(book, 10, 1), 0)
        self.assertEqual(cursor.replay_through(book, 11, 1), 1)
        self.assertTrue(cursor.complete)
        self.assertEqual(book.queue(True, 50), [1, 2])

    def test_parity_accepts_same_log_with_different_quantity_forms(self) -> None:
        spec = next(item for item in EVENT_SPECS if item.name == "OrderCreated")
        data = "0x" + abi_encode(
            spec.types, (1, OWNER, 100, 50, True)
        ).hex()
        rpc_log = {
            "address": "0x065C9D28E428A0dB40191a54d33d5B7C71a9C394",
            "blockNumber": "0xa",
            "transactionHash": "0xABC",
            "transactionIndex": "0x1",
            "logIndex": "0x2",
            "data": data,
            "topics": [spec.topic],
        }
        hypersync_log = {
            "address": rpc_log["address"].lower(),
            "block_number": 10,
            "transaction_hash": "0xabc",
            "transaction_index": 1,
            "log_index": 2,
            "data": data,
            "topic0": spec.topic,
            "topic1": None,
            "topic2": None,
            "topic3": None,
        }

        parity_report([hypersync_log], [rpc_log])

    def test_parity_rejects_missing_event(self) -> None:
        with self.assertRaisesRegex(ReconstructionError, "parity failed"):
            parity_report([], [{
                "address": "0x065c9d28e428a0db40191a54d33d5b7c71a9c394",
                "blockNumber": "0x1",
                "transactionHash": "0x01",
                "transactionIndex": "0x0",
                "logIndex": "0x0",
                "data": "0x",
                "topics": ["0x" + "11" * 32],
            }])


if __name__ == "__main__":
    unittest.main()

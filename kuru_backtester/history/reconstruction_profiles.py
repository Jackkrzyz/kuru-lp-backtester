"""Version-specific historical metadata; no strategy or queue-fill changes.

The early runtimes update reciprocal flip IDs in _handleFlipOrderUpdate.
Those assignments have no separate event. Recover them from the flip creation
and the following Trade. All queue mutations still use the verified engine.
"""
from dataclasses import replace
from eth_abi import decode, encode

from kuru_backtester.engine import l3_vertical_slice as l3
from kuru_backtester.engine.rebuild_from_chain import ReconstructionError

CURRENT_IMPLEMENTATION = "7c7a6e72320d71112aaf58ad3604224f15bdf0734f5790cf79d2b04194c80d42"
DEPLOYMENT_IMPLEMENTATION = "fb70240e4197152779b6195ad0388f9d972a6c1f1945946f70b8165e6940c86b"
DEPLOYMENT_IMPLEMENTATION_ADDRESS = "0xea2cc8769fb04ff1893ed11cf517b7f040c823cd"
INTERMEDIATE_IMPLEMENTATION = "982bd98e31124ec14180f3eb44c51b1aae151af6674ba68e80b57a5ace428eb6"
INTERMEDIATE_IMPLEMENTATION_ADDRESS = "0x4c0ba1ba8404076e2b09c7a5d014ec999c3a27e2"
# Actual-runtime EVM tests and captured archive RPC replay verify these two
# versions' non-emitted reciprocal-link assignments. This is not a wildcard
# for other implementations, nor approval of their strategy accounting.
RECIPROCAL_LINK_IMPLEMENTATIONS = frozenset((DEPLOYMENT_IMPLEMENTATION, INTERMEDIATE_IMPLEMENTATION))
SUPPORTED_IMPLEMENTATIONS = RECIPROCAL_LINK_IMPLEMENTATIONS | {CURRENT_IMPLEMENTATION}


def head_metadata(book):
    """Keep inactive predecessor storage separate from the live FIFO links."""
    return getattr(book, "contract_head_prev", {})


def restore_head_metadata(book, values):
    metadata = {int(order_id): int(previous) for order_id, previous in values}
    if len(metadata) != len(values):
        raise ReconstructionError("Duplicate contract head metadata")
    heads = {level.head for level in book.levels.values()}
    if any(order_id not in heads or previous <= 0 or previous >= order_id or previous in book.orders
           for order_id, previous in metadata.items()):
        raise ReconstructionError("Invalid inactive contract head predecessor")
    if metadata:
        book.contract_head_prev = metadata


def load_checkpoint(rpc, market, block, implementation):
    """Read known early storage without weakening canonical queue invariants.

    The contract moves the price-point head on a fill but does not clear the
    new head's old prev slot. Only a head may have this inactive predecessor.
    Interior links, tails, aggregate sizes and cycle checks remain unchanged.
    """
    if implementation not in SUPPORTED_IMPLEMENTATIONS:
        raise ReconstructionError(f"Unsupported reconstruction profile: {implementation}")
    if implementation not in RECIPROCAL_LINK_IMPLEMENTATIONS:
        return l3.load_checkpoint(rpc, market, block)
    heads, metadata = set(), {}
    point_selectors = {l3.encode_call(name) for name in
                       ("s_buyPricePoints(uint256)", "s_sellPricePoints(uint256)")}
    order_selector = l3.encode_call("s_orders(uint40)")
    order_types = ("address", "uint96", "uint40", "uint40", "uint40", "uint32", "uint32", "bool")

    class MetadataRpc:
        def __getattr__(self, name):
            return getattr(rpc, name)

        def normalize(self, method, params, result):
            if method != "eth_call" or params[0].get("to", "").lower() != market.lower() or params[1] != hex(block):
                return result
            data = params[0]["data"]
            if data[:10] in point_selectors:
                head, _ = decode(("uint40", "uint40"), bytes.fromhex(result[2:]))
                heads.add(int(head))
            elif data[:10] == order_selector:
                order_id = int(data[10:], 16)
                if order_id in heads:
                    fields = list(decode(order_types, bytes.fromhex(result[2:])))
                    if fields[2]:
                        metadata[order_id] = int(fields[2])
                        fields[2] = 0
                        return "0x" + encode(order_types, fields).hex()
            return result

        def call(self, method, params):
            return self.normalize(method, params, rpc.call(method, params))

        def batch(self, calls):
            return [self.normalize(method, params, result)
                    for (method, params), result in zip(calls, rpc.batch(calls), strict=True)]

    book, bids, asks = l3.load_checkpoint(MetadataRpc(), market, block)
    restore_head_metadata(book, list(metadata.items()))
    return book, bids, asks


def apply_event(book, event, price_precision, implementation):
    """Keep the current implementation path unchanged; adapt old flip links."""
    if implementation not in SUPPORTED_IMPLEMENTATIONS:
        raise ReconstructionError(f"Unsupported reconstruction profile: {implementation}")
    if implementation not in RECIPROCAL_LINK_IMPLEMENTATIONS:
        return l3.apply_event(book, event, price_precision)
    args = event.args
    source_id = int(args.get("flipped_id", 0)) if event.name == "FlippedOrderCreated" else 0
    source = book.orders.get(source_id)
    if source_id:
        if (source is None or source.flipped_id != 0
                or source.owner != str(args["owner"]).lower()
                or source.is_buy == bool(args["is_buy"])
                or source.price != int(args["flipped_price"])
                or source.flipped_price != int(args["price"])):
            raise ReconstructionError("Legacy flipped order has an inconsistent source")
    filled = None
    if event.name == "Trade" and int(args["updated_size"]) == 0:
        filled = book.orders.get(int(args["order_id"]))
    cancelled = book.orders.get(int(args["order_id"])) if event.name == "OrderCanceled" else None
    l3.apply_event(book, event, price_precision)
    metadata = head_metadata(book)
    if filled is not None:
        metadata.pop(filled.order_id, None)
        if filled.next:
            metadata[filled.next] = filled.order_id
    if cancelled is not None:
        raw_previous = metadata.pop(cancelled.order_id, 0)
        if cancelled.prev == 0 and cancelled.next:
            # _executeCancel copies the raw prev slot, even when it points to
            # an inactive filled order. Canonical queue links remain zero.
            if raw_previous:
                metadata[cancelled.next] = raw_previous
            else:
                metadata.pop(cancelled.next, None)
    if metadata or hasattr(book, "contract_head_prev"):
        book.contract_head_prev = metadata
    if source_id:
        book.orders[source_id] = replace(source, flipped_id=int(args["order_id"]))
    if filled is not None and filled.flipped_id:
        paired = book.orders.get(filled.flipped_id)
        if paired is None or paired.flipped_id != filled.order_id:
            raise ReconstructionError("Legacy filled order has an inconsistent flip pair")
        book.orders[paired.order_id] = replace(paired, flipped_id=0)

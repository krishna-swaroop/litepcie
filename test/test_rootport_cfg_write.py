#
# This file is part of LitePCIe.
#
# Copyright (c) 2026 Krishna Swaroop <swaroopdhulipalla99@gmail.com>
# SPDX-License-Identifier: BSD-2-Clause

"""Root Port configuration writes are non-posted.

A CfgWr0 is answered by a Cpl without data (Byte Count = 4). The TLP controller must give it a
tag/req_queue entry like a read and deliver its Cpl to the requester; the depacketizer must mark a
data-less completion as `end` (length 0 never equals Byte Count/4) so it retires. Memory writes
stay posted.
"""

import unittest

from migen import *
from migen.sim import run_simulation

from litex.gen import *

from litepcie.common import phy_layout
from litepcie.tlp.controller import LitePCIeTLPController
from litepcie.tlp.depacketizer import LitePCIeTLPDepacketizer
from litepcie.tlp.common import fmt_type_dict

POSTED_TAG = 32


class TestRootPortCfgWrite(unittest.TestCase):
    def test_depacketizer_cpl_without_data_is_end(self):
        dw  = 256
        dut = LitePCIeTLPDepacketizer(data_width=dw, endianness="little",
                                      capabilities=["REQUEST", "COMPLETION"])
        res = {}

        def tb():
            # Cpl (no data), Byte Count 4, tag 0x05; 3DW header only.
            h = [(fmt_type_dict["cpl"] << 24), (0x0100 << 16) | 4, (0x0000 << 16) | (0x05 << 8)]
            yield dut.sink.dat.eq(sum(d << (32 * i) for i, d in enumerate(h)))
            yield dut.sink.be.eq(0xfff)
            yield dut.sink.first.eq(1)
            yield dut.sink.last.eq(1)
            yield dut.sink.valid.eq(1)
            yield dut.cmp_source.ready.eq(1)
            for _ in range(20):
                yield
                if (yield dut.sink.ready):
                    yield dut.sink.valid.eq(0)
                if (yield dut.cmp_source.valid):
                    res["end"] = (yield dut.cmp_source.end)
                    res["tag"] = (yield dut.cmp_source.tag)
                    res["err"] = (yield dut.cmp_source.err)
                    break

        run_simulation(dut, tb())
        self.assertEqual(res, {"end": 1, "tag": 0x05, "err": 0})

    def test_controller_tags_config_writes(self):
        dw  = 128
        dut = LitePCIeTLPController(data_width=dw, address_width=32, max_pending_requests=4,
                                    with_configuration=True)
        req_in, req_out = dut.master_in.sink, dut.master_out.sink
        cmp_in, cmp_out = dut.master_out.source, dut.master_in.source
        sent, got = [], []

        def send(we, is_cfg, channel):
            yield req_in.we.eq(we)
            yield req_in.is_cfg.eq(is_cfg)
            yield req_in.channel.eq(channel)
            yield req_in.len.eq(1)
            yield req_in.first.eq(1)
            yield req_in.last.eq(1)
            yield req_in.valid.eq(1)
            yield
            while not (yield req_in.ready):
                yield
            yield req_in.valid.eq(0)

        def requests():
            for _ in range(8):   # Let the tag queue fill.
                yield
            yield from send(we=1, is_cfg=0, channel=1)  # MemWr: posted.
            yield from send(we=1, is_cfg=1, channel=2)  # CfgWr: non-posted.
            yield from send(we=0, is_cfg=1, channel=3)  # CfgRd.
            for _ in range(60):
                yield

        def link():
            # Capture requests; answer non-posted ones in order, as the device would.
            yield req_out.ready.eq(1)
            while len(sent) < 3:
                yield
                if (yield req_out.valid) and (yield req_out.ready):
                    sent.append(dict(we=(yield req_out.we), is_cfg=(yield req_out.is_cfg),
                                     tag=(yield req_out.tag)))
            for r in sent:
                if r["we"] and not r["is_cfg"]:
                    continue
                data = not r["we"]
                yield cmp_in.tag.eq(r["tag"])
                yield cmp_in.len.eq(1 if data else 0)
                yield cmp_in.byte_count.eq(4)
                yield cmp_in.end.eq(1)      # As the depacketizer now reports for both.
                yield cmp_in.dat.eq(0xcafe if data else 0)
                yield cmp_in.first.eq(1)
                yield cmp_in.last.eq(1)
                yield cmp_in.valid.eq(1)
                yield
                while not (yield cmp_in.ready):
                    yield
                yield cmp_in.valid.eq(0)

        def completions():
            yield cmp_out.ready.eq(1)
            for _ in range(200):
                yield
                if (yield cmp_out.valid) and (yield cmp_out.ready):
                    got.append(((yield cmp_out.channel), (yield cmp_out.end), (yield cmp_out.dat) & 0xffff))

        run_simulation(dut, [requests(), link(), completions()])
        self.assertEqual(len(sent), 3)
        self.assertEqual(sent[0]["tag"], POSTED_TAG)       # MemWr stays posted.
        self.assertNotEqual(sent[1]["tag"], POSTED_TAG)    # CfgWr is tagged ...
        self.assertNotEqual(sent[1]["tag"], sent[2]["tag"])
        # ... and both non-posted completions reach their requesters, in request order.
        self.assertEqual(got, [(2, 1, 0), (3, 1, 0xcafe)])


if __name__ == "__main__":
    unittest.main()

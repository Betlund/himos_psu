"""Unit tests covering live-captured KE-1300P frames, CRC-8, x9 fan curve, and client."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from himos_psu.crc8 import crc8, verify_rx_crc
from himos_psu.fan_curve import DEFAULT_CUSTOM_POINTS, clamp_dragged_point, resample_fan_curve, validate_fan_curve
from himos_psu.protocol import (
    CmdId,
    FanMode,
    PowerExtraTelemetry,
    PowerInfo,
    PowerSerial,
    PowerStatus,
    RefreshMode,
    SpecsMerger,
    StreamFrameParser,
    build_clear_fault_frame,
    build_fan_curve_frame,
    build_fan_mode_frame,
    build_info_request_frame,
    build_refresh_mode_frame,
    format_warranty_badge,
    parse_command_content,
    parse_frame,
)
from himos_psu.client import PowerSupplyClient

# Real wire frames captured from live Kunlun KE-1300P (/dev/ttyUSB0)
LIVE_STATUS_HEX = "55 7e 1e 02 eb 0c 8d 13 61 2f 92 13 00 0f 0e 01 97 10 f6 01 7c 3f 12 34 54 1f ee 08 00 00 00 00 e4 ae"
LIVE_EXTRA04_HEX = "55 7e 1e 04 4f 00 54 00 98 00 fd 01 39 00 13 01 74 0f e2 01 d5 00 b8 08 c5 03 a4 03 ce 03 a3 03 97 ae"
LIVE_INFO_SUB0_HEX = "55 7e 1b 03 4b 45 2d 31 33 30 30 50 44 04 14 6e 64 f0 05 14 0a 50 73 65 67 6f 74 65 70 2b ae"
LIVE_INFO_SUB53_HEX = "55 7e 1b 03 4b 45 2d 31 33 30 30 50 44 04 14 6e 64 f0 05 14 0a 50 73 65 67 6f 18 09 14 26 ae"
LIVE_SN_HEX = "55 7e 12 05 53 42 52 43 30 30 30 30 30 30 30 30 31 32 33 20 62 ae"


class ProtocolTestCase(unittest.TestCase):
    def test_crc8_matches_tx_and_live_rx(self) -> None:
        self.assertEqual(build_info_request_frame(0), bytes.fromhex("55 7e 04 04 00 00 f3 ae"))
        self.assertEqual(build_info_request_frame(53), bytes.fromhex("55 7e 04 04 35 00 4b ae"))
        self.assertEqual(crc8(b"123456789"), 0xF4)  # Standard CRC-8 check value
        for hex_str in (LIVE_STATUS_HEX, LIVE_EXTRA04_HEX, LIVE_INFO_SUB0_HEX, LIVE_INFO_SUB53_HEX, LIVE_SN_HEX):
            self.assertTrue(verify_rx_crc(bytes.fromhex(hex_str)))

    def test_decode_live_ke1300p_status_and_specs(self) -> None:
        status_frame = parse_frame(bytes.fromhex(LIVE_STATUS_HEX))
        status = parse_command_content(status_frame.cmd_id, status_frame.payload)
        assert isinstance(status, PowerStatus)
        self.assertEqual(status.voltage_3v3, 3.31)
        self.assertEqual(status.voltage_5v, 5.01)
        self.assertEqual(status.voltage_12v, 12.13)
        self.assertEqual(status.voltage_5v_sb, 5.01)
        self.assertEqual(status.mode, FanMode.AUTO)
        self.assertEqual(status.power, 212.35)
        self.assertEqual(status.frequency, 50.2)
        self.assertEqual(status.high_voltage_temperature, 63.48)
        self.assertEqual(status.low_voltage_temperature, 52.07)
        self.assertEqual(status.environment_temperature, 31.33)
        self.assertEqual(status.input_voltage, 228.6)
        self.assertEqual(status.fan_speed, 0)
        self.assertEqual(status.malfunction_code, 0)

        info_sub0 = parse_command_content(CmdId.POWER_INFO_RESPONSE, parse_frame(bytes.fromhex(LIVE_INFO_SUB0_HEX)).payload)
        info_sub53 = parse_command_content(CmdId.POWER_INFO_RESPONSE, parse_frame(bytes.fromhex(LIVE_INFO_SUB53_HEX)).payload)
        assert isinstance(info_sub0, PowerInfo) and isinstance(info_sub53, PowerInfo)

        merger = SpecsMerger()
        merger.update(info_sub0)
        merged = merger.update(info_sub53)
        self.assertEqual(merged.model, "KE-1300P")
        self.assertEqual(merged.brand_family, "kunlun")
        self.assertEqual(merged.rated_power, 1300)
        self.assertEqual(merged.certification_80plus, "P")
        self.assertEqual(merged.manufacture, "segotep")
        self.assertEqual(merged.firmware_display, "V4.24.09.20")

        sn_obj = parse_command_content(CmdId.POWER_SN_RESPONSE, parse_frame(bytes.fromhex(LIVE_SN_HEX)).payload)
        assert isinstance(sn_obj, PowerSerial)
        self.assertEqual(sn_obj.sn, "SBRC00000000123")
        warranty, badge = format_warranty_badge(merged, sn_obj.sn)
        self.assertIn("终身", warranty)
        self.assertEqual(badge, "No.123/1000")

        extra = parse_command_content(CmdId.POWER_INFO, parse_frame(bytes.fromhex(LIVE_EXTRA04_HEX)).payload)
        assert isinstance(extra, PowerExtraTelemetry)
        self.assertEqual(len(extra.u16_words), 14)

    def test_stream_parser_resync_and_fragmentation(self) -> None:
        stream = (
            b"\x00\xff\x55\x11"  # junk + false 0x55 start
            + bytes.fromhex(LIVE_STATUS_HEX)
            + bytes.fromhex(LIVE_SN_HEX)
            + bytes.fromhex(LIVE_EXTRA04_HEX)
        )
        parser = StreamFrameParser(verify_crc=True)
        frames = []
        # Feed 1-byte/3-byte fragments across split START_FLAG boundary
        for i in range(0, len(stream), 3):
            frames.extend(parser.feed(stream[i : i + 3]))
        self.assertEqual([f.cmd_id for f in frames], [CmdId.POWER_STATUS_RESPONSE, CmdId.POWER_SN_RESPONSE, CmdId.POWER_INFO])
        self.assertEqual(parser.buffered_bytes, 0)

    def test_fan_curve_resampler_and_builders(self) -> None:
        pts = validate_fan_curve(DEFAULT_CUSTOM_POINTS)
        payload = resample_fan_curve(pts)
        self.assertEqual(len(payload), 27)
        # x = 0,5,10..100 across [(0,0),(40,20),(60,30),(80,70),(100,100)]
        self.assertEqual(list(payload[:6]), [0, 3, 5, 8, 10, 13])  # checks JS Math.round(2.5)->3, 7.5->8, 12.5->13
        self.assertEqual(list(payload[21:]), [40, 20, 60, 30, 80, 70])

        tx_curve = build_fan_curve_frame(pts)
        self.assertEqual(len(tx_curve), 33)
        self.assertEqual(tx_curve[:4], b"\x55\x7e\x1d\x1b")
        self.assertEqual(tx_curve[-1], 0xAE)

        self.assertEqual(build_fan_mode_frame(FanMode.SILENT)[:6], b"\x55\x7e\x04\x13\x00\x01")
        self.assertEqual(build_refresh_mode_frame(RefreshMode.MS_2000)[:6], b"\x55\x7e\x04\x16\x00\x02")
        self.assertEqual(build_clear_fault_frame()[:6], b"\x55\x7e\x04\x1e\x00\x00")

        with self.assertRaises(ValueError):
            validate_fan_curve([(0, 0), (20, 10), (60, 30), (80, 70), (100, 100)])  # pt1.x < 30

        dragged = clamp_dragged_point(DEFAULT_CUSTOM_POINTS, 2, 65, 85)
        self.assertEqual(dragged[2], (65, 85))
        self.assertEqual(dragged[3], (80, 85))  # monotonic propagation to pt3.y

    def test_mock_client_roundtrip(self) -> None:
        with PowerSupplyClient(mock=True) as client:
            info, sn = client.query_specs_and_sn(timeout=1.0)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.model, "KE-1300P")
            self.assertEqual(info.firmware_display, "V4.24.09.20")
            self.assertEqual(info.manufacture, "segotep")
            self.assertEqual(sn, "SBRC00000000123")

            client.set_fan_mode(FanMode.CLEAN)
            st = client.wait_for_status(timeout=1.5)
            self.assertEqual(st.mode, FanMode.CLEAN)
            self.assertGreater(st.fan_speed, 0)


if __name__ == "__main__":
    unittest.main()

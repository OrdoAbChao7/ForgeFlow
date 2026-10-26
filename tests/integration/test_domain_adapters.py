from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from forgeflow.adapters.fpga import (
    FpgaAdapter,
    parse_iverilog_output,
    parse_synthesis_report,
    parse_timing_report,
    record_hardware_attestation,
)
from forgeflow.adapters.python import GitHubPRAdapter, PythonAdapter
from forgeflow.domain.errors import ExternalBlockedError
from forgeflow.verification.fingerprint import tool_version


def test_python_adapter_runs_real_checks(tmp_path: Path) -> None:
    (tmp_path / "sample.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text(
        "from sample import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    adapter = PythonAdapter(tmp_path)
    report = adapter.run_checks(include_coverage=False)
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["pytest"]["status"] == "PASS"
    assert checks["pytest"]["exit_code"] == 0
    assert checks["ruff"]["status"] in {"PASS", "FAIL"}  # real tool, not a stub
    assert report["github"] == "NOT_CONNECTED"


def test_github_adapter_is_local_only_without_credentials(tmp_path: Path) -> None:
    adapter = GitHubPRAdapter(tmp_path)
    result = adapter.simulate_ci(
        {"checks": [{"name": "unit", "status": "PASS"}, {"name": "contract", "status": "NOT_RUN"}]}
    )
    assert result["provider"] == "local_simulated_ci"
    assert result["github_connected"] is False
    assert result["not_run"] == ["contract"]
    with pytest.raises(ExternalBlockedError):
        adapter.require_connection()


def test_fpga_output_parsers() -> None:
    diagnostics = parse_iverilog_output(
        "counter.v:12: error: Unknown module type: missing\n"
        "counter.v:14: warning: implicit wire\n"
    )
    assert len(diagnostics) == 2
    assert diagnostics[0].line == 12
    assert diagnostics[0].severity == "error"
    synthesis = parse_synthesis_report(
        "Number of LUTs: 123\nNumber of Flip-Flops: 45\nBRAMs: 2\nDSPs: 3\nWarning: clock domain\n"
    )
    assert (synthesis.lut, synthesis.flip_flops, synthesis.bram, synthesis.dsp) == (123, 45, 2, 3)
    assert synthesis.warnings
    timing = parse_timing_report("WNS: 0.125 ns\nTNS: 0.000 ns\nfmax: 123.4 MHz\n")
    assert timing.met is True
    assert timing.fmax_mhz == 123.4
    failing = parse_timing_report("WNS: -0.500 ns\nTNS: -1.000 ns\n")
    assert failing.met is False
    unknown = parse_timing_report("no timing data")
    assert unknown.met is None


def test_fpga_adapter_missing_tools_is_blocked(tmp_path: Path) -> None:
    (tmp_path / "counter.v").write_text(
        "module counter(input clk, output reg [3:0] q);\nendmodule\n", encoding="utf-8"
    )
    adapter = FpgaAdapter(tmp_path)
    result = adapter.simulate(["counter.v"], "counter")
    if tool_version("iverilog") == "MISSING":
        assert result["status"] == "BLOCKED"
        assert "not installed" in result["reason"]
    else:
        assert result["status"] in {"PASS", "FAIL"}


def test_fpga_artifact_and_hardware_never_claim_pass(tmp_path: Path) -> None:
    bitstream = tmp_path / "design.bit"
    bitstream.write_bytes(b"not-a-real-bitstream")
    adapter = FpgaAdapter(tmp_path)
    artifact = adapter.artifact_evidence(bitstream)
    assert artifact["sha256"] == hashlib.sha256(bitstream.read_bytes()).hexdigest()
    assert artifact["hardware_pass"] is False
    attestation = record_hardware_attestation(
        "operator", "board-1", bitstream, "observed LEDs", signature=None
    )
    evidence = adapter.hardware_evidence(attestation)
    assert evidence["status"] == "UNVERIFIED"
    assert evidence["hardware_pass"] is False
    signed = record_hardware_attestation(
        "operator", "board-1", bitstream, "observed LEDs", signature="opaque"
    )
    evidence = adapter.hardware_evidence(signed)
    assert evidence["status"] == "SIGNATURE_UNTRUSTED"
    assert evidence["hardware_pass"] is False

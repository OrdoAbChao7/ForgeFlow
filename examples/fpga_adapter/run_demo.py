"""Exercise FPGA report parsers, bitstream hashing and honest hardware status."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from forgeflow.adapters.fpga import (
    FpgaAdapter,
    parse_iverilog_output,
    parse_synthesis_report,
    parse_timing_report,
    record_hardware_attestation,
)


def main() -> None:
    simulated = (
        "counter.v:12: error: Unknown module type: missing\n"
        "counter.v:14: warning: implicit wire\n"
    )
    synthesis = parse_synthesis_report(
        "Number of LUTs: 120\nNumber of Flip-Flops: 40\nBRAMs: 1\nDSPs: 0\n"
    )
    timing = parse_timing_report("WNS: 0.125 ns\nTNS: 0.000 ns\nfmax: 150.0 MHz\n")
    root = Path(tempfile.mkdtemp(prefix="forgeflow-fpga-"))
    bitstream = root / "design.bit"
    bitstream.write_bytes(b"fixture-bitstream")
    adapter = FpgaAdapter(root)
    artifact = adapter.artifact_evidence(bitstream)
    attestation = record_hardware_attestation(
        "fixture-operator", "board-fixture", bitstream, "not a real board run"
    )
    payload = {
        "tools": adapter.detect(),
        "iverilog_diagnostics": [item.__dict__ for item in parse_iverilog_output(simulated)],
        "synthesis": synthesis.__dict__,
        "timing": timing.__dict__,
        "artifact": artifact,
        "hardware": adapter.hardware_evidence(attestation),
        "simulation_status": adapter.simulate([], "counter"),
    }
    print(json.dumps(payload, indent=2))
    print("Hardware PASS is never inferred without an independently trusted signature.")


if __name__ == "__main__":
    main()

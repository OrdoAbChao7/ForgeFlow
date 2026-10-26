from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgeflow.domain.errors import ExternalBlockedError
from forgeflow.domain.models import utcnow
from forgeflow.runner.command import CommandPolicy, CommandRunner, RunRequest
from forgeflow.verification.fingerprint import sha256_file, tool_version


@dataclass
class SimulationDiagnostic:
    severity: str
    file: str | None
    line: int | None
    message: str


@dataclass
class SynthesisReport:
    lut: int | None = None
    flip_flops: int | None = None
    bram: int | None = None
    dsp: int | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class TimingReport:
    wns_ns: float | None = None
    tns_ns: float | None = None
    fmax_mhz: float | None = None
    met: bool | None = None
    raw_warnings: list[str] = field(default_factory=list)


@dataclass
class HardwareAttestation:
    operator: str
    board_serial: str
    bitstream_sha256: str
    observed_at: str
    notes: str
    signature: str | None = None

    @property
    def status(self) -> str:
        if not self.signature:
            return "UNVERIFIED"
        return "SIGNATURE_UNTRUSTED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "board_serial": self.board_serial,
            "bitstream_sha256": self.bitstream_sha256,
            "observed_at": self.observed_at,
            "notes": self.notes,
            "signature": self.signature,
            "status": self.status,
            "hardware_pass": False,
        }


def parse_iverilog_output(text: str) -> list[SimulationDiagnostic]:
    diagnostics: list[SimulationDiagnostic] = []
    pattern = re.compile(
        r"^(?P<file>[^:\n]+):(?P<line>\d+):\s*(?P<kind>error|warning|syntax error|fatal):\s*(?P<message>.*)$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            diagnostics.append(
                SimulationDiagnostic(
                    severity=match.group("kind").lower().replace(" ", "_"),
                    file=match.group("file"),
                    line=int(match.group("line")),
                    message=match.group("message"),
                )
            )
    return diagnostics


def parse_synthesis_report(text: str) -> SynthesisReport:
    report = SynthesisReport()
    patterns = {
        "lut": r"(?:Number of LUTs?|LUTs?)\s*[:=]\s*(\d+)",
        "flip_flops": r"(?:Number of (?:Flip[- ]?Flops|Registers)|FFs?)\s*[:=]\s*(\d+)",
        "bram": r"(?:BRAMs?|Block RAMs?)\s*[:=]\s*(\d+)",
        "dsp": r"(?:DSPs?)\s*[:=]\s*(\d+)",
    }
    for field_name, pattern in patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            setattr(report, field_name, int(match.group(1)))
    report.warnings = [
        line.strip()
        for line in text.splitlines()
        if "warning" in line.lower() and line.strip()
    ]
    return report


def parse_timing_report(text: str) -> TimingReport:
    report = TimingReport()
    wns = re.search(r"\bWNS\b\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*ns", text, re.IGNORECASE)
    tns = re.search(r"\bTNS\b\s*[:=]?\s*(-?\d+(?:\.\d+)?)\s*ns", text, re.IGNORECASE)
    fmax = re.search(r"\b(?:fmax|maximum frequency)\b\s*[:=]?\s*(\d+(?:\.\d+)?)\s*MHz", text, re.IGNORECASE)
    if wns:
        report.wns_ns = float(wns.group(1))
        report.met = report.wns_ns >= 0
    if tns:
        report.tns_ns = float(tns.group(1))
    if fmax:
        report.fmax_mhz = float(fmax.group(1))
    if report.wns_ns is not None:
        report.met = bool(report.wns_ns >= 0 and (report.tns_ns is None or report.tns_ns >= 0))
    report.raw_warnings = [line.strip() for line in text.splitlines() if "warning" in line.lower()]
    return report


class FpgaAdapter:
    """Icarus RTL adapter. Hardware PASS is never inferred without signed attestation."""

    name = "fpga"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.runner = CommandRunner(
            self.root,
            log_root=self.root / ".forge" / "runs",
            policy=CommandPolicy(allow_workspace_writes=True),
        )

    def detect(self) -> dict[str, str]:
        return {
            "iverilog": tool_version("iverilog"),
            "verilator": tool_version("verilator"),
            "nextpnr": tool_version("nextpnr-ice40"),
            "yosys": tool_version("yosys"),
        }

    def simulate(self, sources: list[str], top: str, *, testbench: str | None = None) -> dict[str, Any]:
        executable = shutil.which("iverilog")
        vvp_executable = shutil.which("vvp")
        if not executable:
            return {
                "adapter": self.name,
                "status": "BLOCKED",
                "reason": "Icarus Verilog is not installed",
                "diagnostics": [],
                "tool": "MISSING",
            }
        if not vvp_executable:
            return {
                "adapter": self.name,
                "status": "BLOCKED",
                "reason": "Icarus vvp runtime is not installed",
                "diagnostics": [],
                "tool": "PARTIAL",
            }
        script = testbench or top
        output = self.root / ".forge" / "fpga" / f"{top}.vvp"
        output.parent.mkdir(parents=True, exist_ok=True)
        result = self.runner.run(
            RunRequest(
                argv=[executable, "-g2012", "-o", str(output), "-s", script, *sources],
                cwd=self.root,
                purpose="fpga:compile",
                timeout_seconds=120,
                writes=True,
            )
        )
        diagnostics = parse_iverilog_output(result.stdout_excerpt + "\n" + result.stderr_excerpt)
        if not result.succeeded:
            return {
                "adapter": self.name,
                "status": "FAIL",
                "exit_code": result.exit_code,
                "diagnostics": [item.__dict__ for item in diagnostics],
                "compile_log": result.result_path,
            }
        simulation = self.runner.run(
            RunRequest(
                argv=[vvp_executable, str(output)],
                cwd=self.root,
                purpose="fpga:simulate",
                timeout_seconds=120,
                writes=True,
            )
        )
        return {
            "adapter": self.name,
            "status": "PASS" if simulation.succeeded else "FAIL",
            "exit_code": simulation.exit_code,
            "success_marker": "PASS" in simulation.stdout_excerpt,
            "diagnostics": [item.__dict__ for item in diagnostics],
            "compile_log": result.result_path,
            "simulation_log": simulation.result_path,
        }

    def artifact_evidence(self, bitstream: str | Path) -> dict[str, Any]:
        path = Path(bitstream)
        if not path.is_absolute():
            path = self.root / path
        if not path.is_file():
            raise ExternalBlockedError(f"bitstream not found: {path}")
        digest = sha256_file(path)
        return {
            "adapter": self.name,
            "artifact": str(path),
            "sha256": digest,
            "size": path.stat().st_size,
            "kind": "bitstream",
            "hardware_pass": False,
            "status": "ARTIFACT_HASHED",
        }

    def hardware_evidence(self, attestation: HardwareAttestation) -> dict[str, Any]:
        return {
            **attestation.as_dict(),
            "adapter": self.name,
            "message": "hardware status remains unverified without an independent trust root",
        }


def record_hardware_attestation(
    operator: str,
    board_serial: str,
    bitstream: str | Path,
    notes: str,
    *,
    signature: str | None = None,
) -> HardwareAttestation:
    return HardwareAttestation(
        operator=operator,
        board_serial=board_serial,
        bitstream_sha256=sha256_file(bitstream),
        observed_at=utcnow().isoformat(),
        notes=notes,
        signature=signature,
    )

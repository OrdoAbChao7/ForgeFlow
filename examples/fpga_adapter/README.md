# FPGA adapter example

```bash
uv run python examples/fpga_adapter/run_demo.py
```

The parser and artifact hash paths run without FPGA hardware. Icarus Verilog is absent
on this machine unless `iverilog` is installed; the adapter reports `BLOCKED` rather
than simulating success. A bitstream SHA-256 is integrity evidence, not proof that a
board was programmed. Hardware status remains `UNVERIFIED` without a trust root.

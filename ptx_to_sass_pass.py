"""PTX -> SASS lowering pass with dependency-aware control-bit optimization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple
import json
import re


@dataclass(frozen=True)
class ControlBits:
    """Control-bit bundle for one SASS instruction slot."""

    stall: int = 0  # 0..15
    yield_hint: int = 0  # 0..1
    write_barrier: int = 0  # 0..7
    read_barrier: int = 0  # 0..7
    wait_mask: int = 0  # 0..63

    def encode(self) -> int:
        if not (0 <= self.stall <= 0xF):
            raise ValueError(f"stall out of range: {self.stall}")
        if self.yield_hint not in (0, 1):
            raise ValueError(f"yield_hint out of range: {self.yield_hint}")
        if not (0 <= self.write_barrier <= 0x7):
            raise ValueError(f"write_barrier out of range: {self.write_barrier}")
        if not (0 <= self.read_barrier <= 0x7):
            raise ValueError(f"read_barrier out of range: {self.read_barrier}")
        if not (0 <= self.wait_mask <= 0x3F):
            raise ValueError(f"wait_mask out of range: {self.wait_mask}")

        # 17-bit toy encoding:
        # [16:11] wait_mask | [10:8] read_barrier | [7:5] write_barrier |
        # [4] yield | [3:0] stall
        return (
            (self.wait_mask << 11)
            | (self.read_barrier << 8)
            | (self.write_barrier << 5)
            | (self.yield_hint << 4)
            | self.stall
        )

    def to_hex(self) -> str:
        return f"0x{self.encode():05X}"


@dataclass(frozen=True)
class InstructionSpec:
    """Lowering spec for PTX op -> SASS op + control defaults + latency."""

    sass_opcode: str
    default_ctrl: ControlBits
    latency: int = 1


DEFAULT_SPECS: Dict[str, InstructionSpec] = {
    "add.s32": InstructionSpec(
        sass_opcode="IADD3",
        default_ctrl=ControlBits(stall=0, yield_hint=0, write_barrier=1),
        latency=1,
    ),
    "mov.u32": InstructionSpec(
        sass_opcode="MOV",
        default_ctrl=ControlBits(stall=0, yield_hint=0),
        latency=1,
    ),
    "mul.lo.s32": InstructionSpec(
        sass_opcode="IMAD",
        default_ctrl=ControlBits(stall=0, yield_hint=0, write_barrier=2),
        latency=4,
    ),
}


@dataclass
class ParsedInstruction:
    ptx_op: str
    operands: List[str]
    raw_line: str


class PTXToSASSPass:
    """Compile pass that lowers PTX into SASS and auto-optimizes control bits.

    The optimizer computes the *minimum required* stall based on register
    dependencies and per-op latency. Under the in-order/no-reorder assumption,
    this yields the highest throughput while preserving RAW correctness.
    """

    INSN_RE = re.compile(r"^(?P<op>[a-zA-Z0-9_.]+)\s+(?P<body>.+);$")
    REG_RE = re.compile(r"^%[a-zA-Z][a-zA-Z0-9_.]*$")

    def __init__(
        self,
        specs: Optional[Dict[str, InstructionSpec]] = None,
        auto_optimize_ctrl: bool = True,
    ):
        self.specs = specs or DEFAULT_SPECS
        self.auto_optimize_ctrl = auto_optimize_ctrl

    @staticmethod
    def _normalize_operands(body: str) -> List[str]:
        return [x.strip() for x in body.split(",")]

    def _parse_line(self, line: str) -> Optional[ParsedInstruction]:
        s = line.strip()
        if not s or s.startswith("//"):
            return None
        if s.startswith(".entry") or s.startswith("{") or s.startswith("}"):
            return None

        m = self.INSN_RE.match(s)
        if not m:
            return None

        return ParsedInstruction(
            ptx_op=m.group("op"),
            operands=self._normalize_operands(m.group("body")),
            raw_line=s,
        )

    def _extract_dst_src(self, parsed: ParsedInstruction) -> Tuple[Optional[str], Sequence[str]]:
        reg_ops = [op for op in parsed.operands if self.REG_RE.match(op)]
        if not reg_ops:
            return None, []

        dst = reg_ops[0]
        srcs = reg_ops[1:]
        return dst, srcs

    def _optimized_ctrl(
        self,
        spec: InstructionSpec,
        parsed: ParsedInstruction,
        current_cycle: int,
        reg_ready_cycle: Dict[str, int],
        reg_barrier: Dict[str, int],
    ) -> ControlBits:
        base = spec.default_ctrl
        dst, srcs = self._extract_dst_src(parsed)

        # RAW dependency analysis: required stall = max(ready_cycle - now, 0)
        needed_stall = 0
        wait_mask = 0
        for src in srcs:
            ready = reg_ready_cycle.get(src, current_cycle)
            if ready > current_cycle:
                needed_stall = max(needed_stall, ready - current_cycle)
            barrier_id = reg_barrier.get(src)
            if barrier_id is not None:
                wait_mask |= (1 << barrier_id)

        stall = min(needed_stall, 0xF)
        # Heuristic: if long bubbles are unavoidable, request warp switch.
        yield_hint = 1 if stall >= 6 else base.yield_hint

        ctrl = ControlBits(
            stall=stall,
            yield_hint=yield_hint,
            write_barrier=base.write_barrier,
            read_barrier=base.read_barrier,
            wait_mask=wait_mask & 0x3F,
        )

        # Update scoreboard state.
        if dst is not None:
            reg_ready_cycle[dst] = current_cycle + stall + max(1, spec.latency)
            if ctrl.write_barrier > 0:
                reg_barrier[dst] = ctrl.write_barrier
            elif dst in reg_barrier:
                del reg_barrier[dst]

        return ctrl

    def run(self, ptx_text: str) -> str:
        out: List[str] = []
        pc = 0
        cycle = 0
        reg_ready_cycle: Dict[str, int] = {}
        reg_barrier: Dict[str, int] = {}

        for line in ptx_text.splitlines():
            parsed = self._parse_line(line)
            if parsed is None:
                continue

            spec = self.specs.get(parsed.ptx_op)
            if spec is None:
                out.append(f"/*{pc:04X}*/ NOP ; // missing-spec for {parsed.ptx_op}")
                pc += 8
                cycle += 1
                continue

            operands = ", ".join(parsed.operands)
            if self.auto_optimize_ctrl:
                ctrl = self._optimized_ctrl(spec, parsed, cycle, reg_ready_cycle, reg_barrier)
            else:
                ctrl = spec.default_ctrl

            out.append(
                f"/*{pc:04X}*/ {spec.sass_opcode} {operands} ; "
                f"// ctrl={ctrl.to_hex()} ptx={parsed.ptx_op}"
            )

            pc += 8
            cycle += 1 + ctrl.stall

        return "\n".join(out)


def load_specs_from_json(path: str) -> Dict[str, InstructionSpec]:
    """Load instruction specs from a JSON file.

    Extended schema supports latency:
    {
      "add.s32": {
        "sass_opcode": "IADD3",
        "latency": 1,
        "control": {"stall": 0, "yield_hint": 0, "write_barrier": 1}
      }
    }
    """

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    specs: Dict[str, InstructionSpec] = {}
    for ptx_op, value in data.items():
        c = value.get("control", {})
        specs[ptx_op] = InstructionSpec(
            sass_opcode=value["sass_opcode"],
            default_ctrl=ControlBits(
                stall=int(c.get("stall", 0)),
                yield_hint=int(c.get("yield_hint", 0)),
                write_barrier=int(c.get("write_barrier", 0)),
                read_barrier=int(c.get("read_barrier", 0)),
                wait_mask=int(c.get("wait_mask", 0)),
            ),
            latency=int(value.get("latency", 1)),
        )
    return specs


if __name__ == "__main__":
    demo_ptx = """
.entry foo() {
  mov.u32 %r1, %tid.x;
  add.s32 %r2, %r1, 1;
  mul.lo.s32 %r3, %r2, %r2;
  add.s32 %r4, %r3, 7;
}
"""
    print(PTXToSASSPass().run(demo_ptx))

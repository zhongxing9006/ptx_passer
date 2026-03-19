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


@dataclass(frozen=True)
class ParsedInstruction:
    """Normalized PTX instruction record."""

    ptx_op: str
    operands: List[str]
    raw_line: str


@dataclass(frozen=True)
class LoweredInstruction:
    """Final SASS line materialization before text emission."""

    pc: int
    sass_opcode: str
    operands: List[str]
    ctrl: ControlBits
    ptx_op: str

    def to_text(self) -> str:
        ops = ", ".join(self.operands)
        return (
            f"/*{self.pc:04X}*/ {self.sass_opcode} {ops} ; "
            f"// ctrl={self.ctrl.to_hex()} ptx={self.ptx_op}"
        )


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


class PTXParser:
    """Parse PTX text into normalized instructions."""

    INSN_RE = re.compile(r"^(?P<op>[a-zA-Z0-9_.]+)\s+(?P<body>.+);$")

    @staticmethod
    def normalize_operands(body: str) -> List[str]:
        return [x.strip() for x in body.split(",")]

    def parse_line(self, line: str) -> Optional[ParsedInstruction]:
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
            operands=self.normalize_operands(m.group("body")),
            raw_line=s,
        )


class DependencyScheduler:
    """Dependency-aware control-bit scheduler for an in-order toy backend."""

    REG_RE = re.compile(r"^%[a-zA-Z][a-zA-Z0-9_.]*$")

    def __init__(self) -> None:
        self.reg_ready_cycle: Dict[str, int] = {}
        self.reg_barrier: Dict[str, int] = {}

    def _extract_dst_src(self, parsed: ParsedInstruction) -> Tuple[Optional[str], Sequence[str]]:
        reg_ops = [op for op in parsed.operands if self.REG_RE.match(op)]
        if not reg_ops:
            return None, []
        return reg_ops[0], reg_ops[1:]

    def compute_ctrl(
        self,
        spec: InstructionSpec,
        parsed: ParsedInstruction,
        current_cycle: int,
        auto_optimize_ctrl: bool,
    ) -> ControlBits:
        if not auto_optimize_ctrl:
            return spec.default_ctrl

        base = spec.default_ctrl
        dst, srcs = self._extract_dst_src(parsed)

        needed_stall = 0
        wait_mask = 0
        for src in srcs:
            ready = self.reg_ready_cycle.get(src, current_cycle)
            if ready > current_cycle:
                needed_stall = max(needed_stall, ready - current_cycle)
            barrier_id = self.reg_barrier.get(src)
            if barrier_id is not None:
                wait_mask |= 1 << barrier_id

        stall = min(needed_stall, 0xF)
        yield_hint = 1 if stall >= 6 else base.yield_hint
        ctrl = ControlBits(
            stall=stall,
            yield_hint=yield_hint,
            write_barrier=base.write_barrier,
            read_barrier=base.read_barrier,
            wait_mask=wait_mask & 0x3F,
        )

        if dst is not None:
            self.reg_ready_cycle[dst] = current_cycle + stall + max(1, spec.latency)
            if ctrl.write_barrier > 0:
                self.reg_barrier[dst] = ctrl.write_barrier
            elif dst in self.reg_barrier:
                del self.reg_barrier[dst]

        return ctrl


class PTXToSASSPass:
    """Compile pass that lowers PTX into SASS and auto-optimizes control bits."""

    def __init__(
        self,
        specs: Optional[Dict[str, InstructionSpec]] = None,
        auto_optimize_ctrl: bool = True,
    ):
        self.specs = specs or DEFAULT_SPECS
        self.auto_optimize_ctrl = auto_optimize_ctrl
        self.parser = PTXParser()

    def lower(self, ptx_text: str) -> List[LoweredInstruction]:
        lowered: List[LoweredInstruction] = []
        scheduler = DependencyScheduler()
        pc = 0
        cycle = 0

        for line in ptx_text.splitlines():
            parsed = self.parser.parse_line(line)
            if parsed is None:
                continue

            spec = self.specs.get(parsed.ptx_op)
            if spec is None:
                lowered.append(
                    LoweredInstruction(
                        pc=pc,
                        sass_opcode="NOP",
                        operands=[],
                        ctrl=ControlBits(),
                        ptx_op=f"missing-spec:{parsed.ptx_op}",
                    )
                )
                pc += 8
                cycle += 1
                continue

            ctrl = scheduler.compute_ctrl(spec, parsed, cycle, self.auto_optimize_ctrl)
            lowered.append(
                LoweredInstruction(
                    pc=pc,
                    sass_opcode=spec.sass_opcode,
                    operands=parsed.operands,
                    ctrl=ctrl,
                    ptx_op=parsed.ptx_op,
                )
            )
            pc += 8
            cycle += 1 + ctrl.stall

        return lowered

    def run(self, ptx_text: str) -> str:
        return "\n".join(insn.to_text() for insn in self.lower(ptx_text))


def build_feature_description() -> str:
    """Return a concise functional description for integration docs."""

    return (
        "PTXToSASSPass 功能描述:\n"
        "1) PTX文本解析与SASS映射（支持外部JSON定义opcode/control/latency）；\n"
        "2) 基于RAW依赖和latency的control bit自动优化，最小化stall；\n"
        "3) 输出稳定格式: /*PC*/ OPCODE OPERANDS ; // ctrl=0xXXXXX ptx=...；\n"
        "4) 对未配置指令输出NOP占位，保证流程可继续。"
    )


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
    p = PTXToSASSPass()
    print(build_feature_description())
    print(p.run(demo_ptx))

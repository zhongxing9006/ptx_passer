# ptx_passer

一个简化的 SIMT 后端 pass：把 PTX lowering 成 SASS，并根据指令依赖自动优化 control bit。

## 功能描述（可直接对接外部系统）

`build_feature_description()` 返回标准化功能说明，核心能力包括：

1. PTX 文本解析与 SASS opcode 映射（支持外部 JSON 配置）；
2. 基于 RAW 依赖 + 指令 latency 的 control bit 自动优化；
3. 输出稳定格式，方便后续编码、统计和调试；
4. 未配置指令自动降级为 NOP，占位不中断流程。

## 代码结构

- `ControlBits`: control bit 编码与校验。
- `InstructionSpec`: 指令映射、默认控制模板、延迟模型。
- `PTXParser`: PTX 解析器（只做语法清洗与标准化）。
- `DependencyScheduler`: 依赖分析与控制位优化。
- `PTXToSASSPass`: 编排 lowering 流程并生成最终文本。

## 输出格式

每条可识别 PTX 指令输出为：

```text
/*<PC_HEX>*/ <SASS_OPCODE> <OPERANDS> ; // ctrl=<CTRL_HEX> ptx=<PTX_OPCODE>
```

示例（`mul.lo.s32` 结果立刻被 `add.s32` 读取，会自动插入最小 stall）：

```text
/*0000*/ IMAD %r1, %r2, %r3 ; // ctrl=0x00040 ptx=mul.lo.s32
/*0008*/ IADD3 %r4, %r1, 1 ; // ctrl=0x02023 ptx=add.s32
```

## 自动 control bit 优化策略（依赖感知）

- 建立寄存器 RAW 依赖：producer 写寄存器，consumer 读寄存器；
- 使用 `latency` 估算 producer 结果可用周期；
- 自动计算最小 `stall = max(ready_cycle - current_cycle, 0)`；
- 自动生成 `wait_mask`（来源寄存器 barrier 汇总）；
- 长停顿触发 `yield_hint=1`（有利于 warp 切换）。

## 外部配置（指令信息/control bit/latency）

可通过 JSON 外部传入：

```json
{
  "add.s32": {
    "sass_opcode": "IADD3",
    "latency": 1,
    "control": {
      "stall": 0,
      "yield_hint": 0,
      "write_barrier": 1,
      "read_barrier": 0,
      "wait_mask": 0
    }
  }
}
```

> 说明：当 `auto_optimize_ctrl=True`（默认）时，`stall/wait_mask/yield_hint` 会按依赖自动调整；其余位由默认控制模板提供。

## 快速运行

```bash
python ptx_to_sass_pass.py
pytest -q
```

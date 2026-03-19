# ptx_passer

一个简化的 SIMT 后端 pass：把 PTX lowering 成 SASS，并根据指令依赖自动优化 control bit。

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

- 建立寄存器 RAW 依赖：producer 写寄存器，consumer 读寄存器。
- 使用每条指令 `latency` 估算 producer 结果可用周期。
- 对每条 consumer 自动计算最小 `stall = max(ready_cycle - current_cycle, 0)`。
- 该策略在“顺序发射、不可重排”假设下，使停顿最小化（吞吐最优）。
- 同时生成 `wait_mask`（按寄存器来源 barrier 汇总）与 `yield_hint`（长 stall 时置 1）。

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

> 说明：当 `auto_optimize_ctrl=True`（默认）时，`stall/wait_mask/yield_hint` 会按依赖自动调整；其余位可由默认控制模板提供。

## 快速运行

```bash
python ptx_to_sass_pass.py
pytest -q
```

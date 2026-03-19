from ptx_to_sass_pass import PTXToSASSPass, ControlBits


def test_control_bits_encoding():
    bits = ControlBits(stall=1, yield_hint=1, write_barrier=2, read_barrier=3, wait_mask=4)
    assert bits.to_hex() == "0x02351"


def test_dependency_aware_stall_optimization():
    ptx = """
mul.lo.s32 %r1, %r2, %r3;
add.s32 %r4, %r1, 1;
"""
    out = PTXToSASSPass().run(ptx)
    lines = out.splitlines()
    assert "IMAD %r1, %r2, %r3" in lines[0]
    # add depends on mul latency=4, so optimizer should auto-insert minimum stall=3
    assert "IADD3 %r4, %r1, 1" in lines[1]
    assert "ctrl=0x02023" in lines[1]


def test_independent_instructions_keep_low_stall():
    ptx = """
mov.u32 %r1, %tid.x;
mov.u32 %r2, %ntid.x;
add.s32 %r3, %r1, %r2;
"""
    out = PTXToSASSPass().run(ptx)
    lines = out.splitlines()
    # independent mov should not add bubbles
    assert "ctrl=0x00000" in lines[0]
    assert "ctrl=0x00000" in lines[1]

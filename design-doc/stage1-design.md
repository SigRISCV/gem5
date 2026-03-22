# 阶段 1 设计计划：CSR 骨架与 DEBUG 指令

## 1. 阶段 1 的目标

阶段 1 的目标是把这套扩展第一次接入 gem5 主代码，但只做最小闭环，不把范围扩散到 ID 传播、加解密和时序流水线。它的交付重点是两个：

1. 建立扩展 CSR 的架构可见骨架。
2. 建立 `DEBUG` 指令的最小可用调试通路。

这里的最小可用定义为：

1. 软件可以通过标准 CSR 指令访问新增 CSR。
2. gem5 可以识别并执行 `opcode=0b1011011` 下的 `DEBUG` 指令。
3. `DEBUG` 能输出足够的状态，用来支撑阶段 2 之后的 bring-up 和问题定位。

## 2. 阶段 1 的边界

### 2.1 本阶段要完成的内容

1. 新增扩展 CSR 的编号、物理映射、名字和基本 mask。
2. 将这些 CSR 接入 gem5 的 RISC-V CSR 访问路径。
3. 定义 `DEBUG` 指令的编码、反汇编格式、执行语义。
4. 为 `DEBUG` 提供一套统一的输出 helper。
5. 补齐阶段 1 的汇编测试计划和目录安排。

### 2.2 本阶段明确不做的内容

1. 不实现 `SETDUMMYID/SETRAWID/SETNEWID`。
2. 不实现 `LS/SS/LS_MAP/SS_ID`。
3. 不实现 `SWITCHS`。
4. 不实现 `pcid/gprid/use/puse/idgen/encmap/exitraw` 的流水线 shadow state。
5. 不对 `MinorCPU`、`O3CPU` 做任何时序相关修改。

阶段 1 是先把架构态和调试入口打通，而不是把全部扩展都做一半。

## 3. 为什么阶段 1 要这样切

这样切分有三个好处：

1. CSR 骨架和 `DEBUG` 指令是后续所有阶段的公共依赖，越早稳定越好。
2. 这一步基本只涉及 `arch/riscv`，改动集中，便于控制回归面。
3. 有了 `DEBUG` 之后，后续实现 `SET*ID`、`LS/SS` 时可以直接把状态打出来，调试成本会明显降低。

如果在阶段 1 就同时引入 ID 传播或加解密，会把问题混在一起，审查和验证都会变难。

## 4. 本阶段的核心设计决策

### 4.1 CSR 的实现策略

本阶段把所有扩展寄存器先作为架构态 CSR 接入，不在这个阶段实现任何与流水线 shadow state 绑定的优化逻辑。

也就是说：

1. `MKEYH/MKEYL/SKEYH/SKEYL/GPRID0-31/PCID/IDCSR/ENCMAP/EXITRAW` 都先进入 `CSRData` 和 `miscRegFile`。
2. 它们都支持通过 `csrr/csrw/csrs/csrc` 访问。
3. 先保证可读、可写、可序列化、可调试，再在阶段 2 以后逐步赋予更复杂的执行语义。

这样做的好处是后续每个阶段都可以依赖同一套 CSR 基础设施，不需要重复改 CSR 框架。

### 4.2 CSR 权限策略

本阶段建议严格按你给出的权限语义接入：

1. `MKEYH/MKEYL` 按 M 态 CSR 处理。
2. `SKEYH/SKEYL/GPRID0-31/PCID/IDCSR/ENCMAP/EXITRAW` 按 S 态 CSR 处理。
3. U 态直接访问这些 CSR 时，应由 gem5 现有 CSR 权限检查路径拒绝。

这样可以避免阶段 1 就引入软件看起来能读写，但后续权限逻辑要回头修的问题。

### 4.3 CSR mask 策略

本阶段只做最小正确 mask：

1. `MKEYH/MKEYL/SKEYH/SKEYL/GPRID0-31/PCID/EXITRAW`：按 64 位可见处理。
2. `ENCMAP`：仅低 32 位可写，其余位读为 0 或保持掩蔽。
3. `IDCSR`：先按 64 位寄存器接入，但明确只定义 `puse/use/idgen` 相关位，未定义位保留为 0。

这里刻意不在阶段 1 引入太细的字段 helper，只把字段布局在文档中固定，便于阶段 3 再做字段级语义。

### 4.4 DEBUG 指令的实现策略

`DEBUG` 是阶段 1 唯一要接入的新扩展指令，但它的定位是仿真辅助指令，不是严格硬件指令。因此本阶段建议用最直接、最稳定的方式实现：

1. 放入 `opcode=0b1011011` 的 custom decode 路径。
2. 使用 I-type 编码，`func3=0b111`。
3. 在 execute 阶段直接调用 helper 输出状态或退出仿真。
4. 不引入任何额外时序模型。

### 4.5 DEBUG 输出策略

`DEBUG` 的 2/3/4 本来就是串口替代机制，因此本阶段的输出策略调整如下：

1. 输出时直接打印，不加 `SIGRISCV-DEBUG:` 之类的前缀。
2. 不限制输出长度，不为了日志简短而压缩内容。
3. `imm=0` 可以完整输出所有 GPR 和扩展 CSR。
4. `IDCSR` 在输出时按 `puse/use/idgen` 三个字段依次展开，其中 `puse` 位于 bit31，`use` 位于 bit30，`idgen` 位于 bits[23:0]。
5. `imm=2/3/4` 按串口替代接口处理，优先采用直接输出风格，而不是包装成额外的 gem5 调试前缀。

这意味着 `DEBUG` 的设计目标优先级是可调试性，而不是日志整洁度。

### 4.6 DEBUG 语义分层策略

为了避免第一版实现过于复杂，建议把 `DEBUG` 分成本阶段必须完成和允许最小化实现两层。

本阶段必须完成：

1. `imm=0`：输出所有 GPR 和全部扩展 CSR。
2. `imm=1`：把 `rs1` 当作字符输出。
3. `imm=2`：把 `rs1` 当作十进制整数直接输出。
4. `imm=4`：把 `rs1` 视为 CSR 编号，直接输出对应 CSR 的值。
5. `imm=5`：退出仿真。

本阶段建议一并完成，但允许实现上简化：

1. `imm=3`：把 `rs1` 当作指针输出，并尝试输出其 GPRID。

这里的允许简化指的是：在阶段 2 的 `gprid` 功能语义还没完全接上前，`imm=3` 可以先按如果该 GPRID CSR 已存在则读出来输出，否则给出 0 处理。

## 5. 模块级实现计划

### 5.1 `arch/riscv/regs/misc.hh`

职责：建立阶段 1 的 CSR 骨架。

本阶段计划改动：

1. 在 `MiscRegIndex` 中增加新的 `MISCREG_*` 物理寄存器索引。
2. 在 `CSRIndex` 中增加对应的 CSR 编号常量。
3. 在 `CSRData` 中登记这些 CSR 的名字、物理索引、RV 类型和权限属性。
4. 在 `CSRMasks` / `CSRWriteMasks` 中为 `ENCMAP` 和 `IDCSR` 加入最小必要 mask。

设计注意点：

1. `GPRID0-31` 要连续编号，方便后续通过 helper 按寄存器号计算索引。
2. `PCID/IDCSR/ENCMAP/EXITRAW` 建议紧跟在扩展 S 态寄存器之后，保持布局整齐。
3. 名字要稳定，因为后续 `DEBUG imm=0` 输出会依赖名字表。

### 5.2 `arch/riscv/isa.hh`

职责：声明扩展 CSR 和 debug helper 接口。

本阶段计划改动：

1. 增加扩展 helper 的声明，例如：
   - 按 GPR 编号读取/写入 GPRID
   - 读取/写入 `PCID`
   - dump 扩展状态
   - 执行 debug 子命令
2. 尽量把 helper 保持在 `ISA` 层，避免在第一版把逻辑散到指令模板里。

设计注意点：

1. helper 名字要服务于后续阶段，不只服务 `DEBUG`。
2. 不在本阶段加入任何 `MinorCPU` 专用接口。

### 5.3 `arch/riscv/isa.cc`

职责：补齐 CSR 访问实现和 `DEBUG` 后端语义。

本阶段计划改动：

1. 在 `MiscRegNames` 中加入新寄存器名。
2. 扩展 `readMiscRegNoEffect/readMiscReg/setMiscRegNoEffect/setMiscReg` 所需路径，使新增 `MISCREG_*` 能正常工作。
3. 确保 serialize/unserialize 时新寄存器会跟随 `miscRegFile` 保存和恢复。
4. 增加 `debug` 输出 helper。
5. 增加 `debug imm=5` 的退出 helper。
6. 为 `imm=4` 增加 CSR 编号到值的查询 helper。

设计注意点：

1. `DEBUG` 的 2/3/4 直接打印，不加调试前缀。
2. `imm=0` 不需要担心输出过长，重点是完整可读。
3. `IDCSR` 输出时要拆成 `puse/use/idgen` 三个字段。
4. `imm=5` 的退出要走 gem5 已有的仿真退出机制，避免直接 `abort`。
5. `imm=4` 对非法 CSR 编号的行为要明确，建议打印 `unknown csr` 并返回 `NoFault`，而不是直接 panic。

### 5.4 `arch/riscv/isa/decoder.isa`

职责：把 `opcode=0b1011011` 接入 decode。

本阶段计划改动：

1. 为 custom opcode 新增 decode 分支。
2. 至少接入 `func3=0b111` 的 `DEBUG`。
3. 其他 `func3` 在阶段 1 暂时返回 unknown/illegal，避免误解码。

设计注意点：

1. decode 表里要提前为后续 `LS/SETNEWID/SWITCHS` 预留清晰位置。
2. 本阶段只接 `DEBUG`，但 decode 组织方式要能自然扩展到后续其他 custom 指令。

### 5.5 `arch/riscv/isa/formats/*.isa`

职责：为 `DEBUG` 提供执行模板和反汇编格式。

本阶段计划改动：

1. 若现有 `ImmOp` 足够，可复用 I-type 格式实现 `DEBUG`。
2. 若现有模板不利于后续 custom 指令扩展，可新增一个小的 custom I-type format。
3. `generateDisassembly()` 至少输出 `debug rd, rs1, imm`。

设计注意点：

1. 阶段 1 更推荐新增轻量 custom format，因为后续 `SET*ID`、`LS_MAP` 也会重用。
2. 不建议把太多阶段 2 语义塞进阶段 1 的模板里。

## 6. DEBUG 指令的详细语义建议

### 6.1 `debug rd, rs1, imm`

本阶段建议语义如下：

1. `rd`：保留，阶段 1 不写回有意义的新值，可保持与 NOP 类似处理。
2. `rs1`：作为输入操作数。
3. `imm`：作为 debug 子命令。

### 6.2 子命令定义

#### `imm=0`

输出：

1. 所有 GPR 的值。
2. 所有阶段 1 已接入的扩展 CSR 值。
3. `IDCSR` 额外按 `puse`、`use`、`idgen` 三个字段依次输出。

格式建议：

1. 一行标题。
2. GPR 保持稳定顺序。
3. CSR 保持稳定顺序。
4. `IDCSR` 在普通十六进制值之外，再展开字段值。

#### `imm=1`

输出：

1. `rs1` 低 8 位作为字符直接输出。

建议：

1. 不对不可打印字符做复杂转义，先直接按字节输出。

#### `imm=2`

输出：

1. `rs1` 按有符号十进制直接输出。
2. 不附加调试前缀。

#### `imm=3`

输出：

1. `rs1` 按指针格式直接输出。
2. 同时尝试输出该寄存器对应的 GPRID 值。
3. 不附加调试前缀。

说明：

1. 这里输出的是源寄存器编号对应的 GPRID CSR 值，不是内存中对象的 ID。
2. 该能力是为后续指针 ID bring-up 提前铺路。

#### `imm=4`

输出：

1. 把 `rs1` 当作 CSR 编号。
2. 直接输出该 CSR 当前值。
3. 如果目标是 `IDCSR`，则一并输出 `puse/use/idgen` 三个字段，其中 `puse/use` 分别来自 bit31/bit30。
4. 不附加调试前缀。

建议：

1. 若 CSR 号非法，打印 `unknown csr`，不终止仿真。

#### `imm=5`

行为：

1. 请求 gem5 结束仿真。
2. 退出消息里带上 `DEBUG imm=5` 标识。

## 7. 阶段 1 的详细开发步骤

### 步骤 1：补阶段 1 设计文档

输出：
1. `design-doc/stage1-design.md`
2. 在 `gem5-design.md` 中登记阶段 1 的设计文档位置。

### 步骤 2：接入 CSR 编号和 metadata

输出：
1. 新的 `MISCREG_*`
2. 新的 `CSR_*`
3. `CSRData`、`CSRMasks`、`CSRWriteMasks` 更新

验收：
1. 编译通过
2. `csrr/csrw` 能访问新 CSR

### 步骤 3：接入 ISA helper

输出：
1. 扩展 CSR helper
2. `DEBUG` 输出 helper

验收：
1. helper 不依赖后续阶段未实现的流水线逻辑
2. 输出格式稳定

### 步骤 4：接入 custom decode 和 DEBUG 指令

输出：
1. `decoder.isa` 更新
2. `DEBUG` execute/disassembly 接入

验收：
1. `DEBUG` 能被反汇编
2. gem5 能执行而不是报非法指令

### 步骤 5：补阶段 1 汇编测试

建议新增：

1. `tests/stage1/debug_char.S`
2. `tests/stage1/debug_int.S`
3. `tests/stage1/debug_dump_regs.S`
4. `tests/stage1/debug_csr_read.S`
5. `tests/stage1/debug_exit.S`
6. `tests/stage1/csr_rw_smoke.S`

验收：
1. 至少覆盖 CSR 读写和 `debug imm=1/2/4/5`
2. `imm=0` 用于人工日志检查

### 步骤 6：本地验证

输出：
1. 阶段 1 测试可编译
2. 至少部分测试可在 gem5 中运行

## 8. 阶段 1 的风险评估

### 8.1 低风险项

1. 新增 CSR 编号和 metadata
2. `DEBUG imm=1/2/5`
3. 汇编测试接入

### 8.2 中风险项

1. CSR mask 定义是否一次到位
2. `DEBUG imm=4` 的 CSR 查询路径
3. `DEBUG imm=0` 输出量较大时的日志可读性

### 8.3 潜在高风险项

1. custom opcode decode 组织不好，后续扩展指令接入会变乱
2. `DEBUG` 的退出实现若选错接口，可能影响测试脚本稳定性

### 8.4 风险控制措施

1. 本阶段只接入 `DEBUG` 一个 custom 指令，先把 decode 骨架搭好。
2. 输出 helper 尽量全部放在 `isa.cc`，减少散乱逻辑。
3. `imm=3` 若实现复杂度偏高，可先完成最小版本，但接口和测试名先保留。

## 9. 阶段 1 的测试计划

### 9.1 测试目录

建议使用：

1. `benchmark/simple-sigriscv-test/gem5_test/tests/stage1/`

### 9.2 测试分组

#### A. CSR 骨架测试

1. `csr_rw_smoke.S`
   目标：验证新增 CSR 可写可读。
2. `csr_privilege_negative.S`
   目标：验证低权限访问被拒绝。

#### B. DEBUG 基础输出测试

1. `debug_char.S`
2. `debug_int.S`
3. `debug_exit.S`

#### C. DEBUG 状态输出测试

1. `debug_dump_regs.S`
2. `debug_csr_read.S`

### 9.3 阶段 1 的验收标准

阶段 1 完成建议以以下条件为准：

1. 新增 CSR 已进入 gem5 的 RISC-V CSR 框架。
2. `csrr/csrw` 对新增 CSR 生效。
3. `DEBUG` 指令可正确 decode、反汇编和执行。
4. `debug imm=5` 可稳定结束仿真。
5. 至少一组汇编测试能证明 `DEBUG` 与 CSR 骨架已打通。

## 10. 阶段 2 的衔接点

阶段 1 完成后，阶段 2 可以直接依赖以下基础设施：

1. `GPRID0-31/PCID/IDCSR` 已经作为 CSR 存在。
2. `DEBUG imm=0/3/4` 可用于观察寄存器与扩展状态。
3. custom opcode decode 骨架已存在，后续可继续加入 `SETDUMMYID/SETRAWID`。

因此阶段 1 的设计目标不是做很多功能，而是为后续阶段提供一套稳定、可观测、可测试的架构入口。

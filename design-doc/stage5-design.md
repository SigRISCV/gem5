# 阶段 5 设计计划：TimingSimpleCPU 的 LS/SS 功能版与 QARMA 接入

## 1. 阶段 5 的目标

阶段 5 的目标是把 `LS/SS` 第一次真正接入 gem5 的 RISC-V ISA 执行路径，并优先保证 `AtomicSimpleCPU/TimingSimpleCPU` 上的功能正确性。

这一阶段的交付重点有四个：

1. 把 `LS/SS` 的 decode、反汇编和 execute/initiateAcc/completeAcc` 语义接入 gem5。
2. 把 QARMA64 的 `enc/dec` 纯函数 helper 接到 `arch/riscv`，实现直接复制 `repo/qemu/target/riscv/qarma.h` 与 `repo/qemu/target/riscv/qarma.c`。
3. 明确 `LS/SS` 在 U/S 模式下的 key、tweak、ID 拆装与 `use` 降级规则。
4. 让 `TimingSimpleCPU` 直接通过现有 mem path 跑通 `LS/SS`，不在本阶段引入额外时序 stall 建模。

这里的“跑通”明确指：

1. `LS` 能从内存取回 64 位密文，按给定 tweak 与 key 做 `qarma64_dec(..., 7)`，再把结果拆成 `rd` 与 `GPRID[rd]`。
2. `SS` 能把 `GPRID[rs2][23:0]` 与 `REG[rs2][39:0]` 拼成 64 位明文，按给定 tweak 与 key 做 `qarma64_enc(..., 7)`，再写回内存。
3. `use` 不生效时，`LS/SS` 必须严格退化为普通 `LD/SD`。

本阶段不要求：

1. `MinorCPU` 对 `LS/SS` 建新的解密等待状态。
2. `LS/SS` 在 `MinorCPU` 上新增功能单元延迟或 cache pipeline 并行加密建模。
3. `LS_MAP/SS_ID`、`ENCMAP`、`SWITCHS` 已经开始实现。

## 2. 当前代码基线与阶段 5 的约束

### 2.1 当前 gem5 基线

截至当前仓库状态，前四阶段已经形成如下实现基线：

1. 扩展 CSR 已经在 `arch/riscv/regs/misc.hh`、`isa.hh`、`isa.cc` 中接通。
2. `SETDUMMYID/SETRAWID/SETNEWID` 和普通整数 ID 传播已经接入。
3. 当前 `shouldApplyIntIdSemantics()` 的行为基线是“仅 U 态且 `IDCSR.use=1` 才启用整数 ID 语义”。
4. `MinorCPU` 的阶段 4 方案已经确定为“在 commit 阶段直接复用 ISA helper”，而不是 companion future state。

阶段 5 必须和这条基线相容，但不能机械复用“整数 ID helper”的所有判定条件。

### 2.2 当前工具链编码基线

虽然最早的需求文档把扩展指令统一描述为 `opcode=0b1011011`，但当前仓库里的 LLVM XSig 实现已经固定了下面这组编码基线：

1. `LS` 与 `LS_MAP` 走 `OPC_MISC_MEM`。
2. `SS` 与 `SS_ID` 走 `OPC_STORE`。
3. `SET*ID/DEBUG/SWITCHS` 继续走 `OPC_CUSTOM_2`。

因此 stage5 的 gem5 设计应以“当前 LLVM/汇编器已支持的真实编码”作为实现依据，而不是重新回到最早那版统一 custom opcode 的设想。

这点必须在实现前先写清楚，否则后面会出现“汇编器能组装，但 gem5 解码不到”的错位。

### 2.3 QEMU 参考的使用边界

本阶段对 QEMU 侧文件的使用边界要明确：

1. `repo/qemu/target/riscv/qarma.h`
2. `repo/qemu/target/riscv/qarma.c`

这两份文件在 stage5 中只作为 QARMA64 `enc/dec` 算法实现参考。

也就是说：

1. gem5 需要直接复制其中的 `text2cell/cell2text/forward/backward/pseudo_reflect/forward_update_key/backward_update_key/qarma64_enc/qarma64_dec` 及相关常量定义，不自行重新实现另一版 QARMA。
2. 但 `LS/SS` 的最终语义仍以本阶段文档中固定的公式为准，而不是机械照抄 QEMU helper 的 privilege/use gating 细节。

这是因为你已经给出了更明确的 stage5 算法定义，优先级高于旧 helper 的行为习惯。

## 3. 阶段 5 的精确定义

这一节直接固定 `LS/SS` 的算法语义，后续实现不再重新解释。

### 3.1 `ls rd, rs1, imm`

当扩展语义生效时，定义为：

1. `addr = REG[rs1] + sext(imm)`
2. `key = (priv == U) ? {SKEYL, SKEYH} : {MKEYL, MKEYH}`
3. `tweak = (priv == U) ? { GPRID[rs1][23:0], addr[39:0] } : { 24'b0, addr[39:0] }`
4. `result = qarma64_dec(mem[addr], tweak, keyl, keyh, 7)`
5. `REG[rd] = sext(result[39:0])`
6. `GPRID[rd] = zext(result[63:40])`

补充约束：

1. `rd=x0` 时，数据写回仍按 RISC-V 规则丢弃。
2. `rd=x0` 时，`GPRID0` 也不更新，和阶段 2/3/4 的整数 ID 规则保持一致。

### 3.2 `ss rs2, rs1, imm`

当扩展语义生效时，定义为：

1. `addr = REG[rs1] + sext(imm)`
2. `key = (priv == U) ? {SKEYL, SKEYH} : {MKEYL, MKEYH}`
3. `tweak = (priv == U) ? { GPRID[rs1][23:0], addr[39:0] } : { 24'b0, addr[39:0] }`
4. `plain = { GPRID[rs2][23:0], REG[rs2][39:0] }`
5. `mem[addr] = qarma64_enc(plain, tweak, keyl, keyh, 7)`

补充约束：

1. `REG[rs2]` 只取低 40 位。
2. `GPRID[rs2]` 只取低 24 位。
3. 拼接顺序固定为高 24 位 ID、低 40 位地址值。

### 3.3 `use` 降级规则

阶段 5 固定如下规则：

1. 在 U 态，只有 `IDCSR.use=1` 时 `LS/SS` 才启用扩展语义。
2. 在 U 态且 `IDCSR.use=0` 时：
   1. `LS` 完全退化为 `LD`
   2. `SS` 完全退化为 `SD`
   3. 不得额外读写任何 `GPRID`
3. 在非 U 态，`LS/SS` 始终启用扩展语义。

这一条刻意与当前整数 ID helper 区分开来：

1. 当前整数 ID helper 的基线是“仅 U 态且 `use=1` 生效”。
2. 但 `LS/SS` 的阶段 5 语义按你给出的公式和 QEMU 当前 `is_sigriscv_use_enabled()` 的模式，应在 S/M 态直接生效。

因此阶段 5 需要新的专用判定 helper，而不能直接复用 `shouldApplyIntIdSemantics()`。

### 3.4 key 与 tweak 的字段解释

#### key

本阶段统一按 QEMU `qarma64_enc/qarma64_dec` 的接口解释：

1. `keyl` 对应 `w0`
2. `keyh` 对应 `k0`
3. U 态使用 `{SKEYL, SKEYH}`
4. 非 U 态使用 `{MKEYL, MKEYH}`

这里把你消息中的 `skeyl:keyh` 解释为 `skeyl:skeyh`，原因是：

1. QEMU helper 的 `get_sigriscv_key()` 明确返回 low/high 两段。
2. 现有 CSR 也正好是 `MKEYL/MKEYH/SKEYL/SKEYH` 成对存在。

如果后续你想把这条解释改回别的 key 组合，需要在实现前单独改文档。

#### tweak

本阶段固定：

1. tweak 总宽度为 64 位。
2. 在 U 态，高 24 位来自 `GPRID[rs1][23:0]`。
3. 在 S/M 态，高 24 位固定为 `0`。
4. 低 40 位来自 `addr[39:0]`。

这里不引入额外的 privilege-dependent tweak 变体，也不在本阶段把 `pcid`、`encmap` 等状态混入 tweak。

## 4. 模块级设计

### 4.1 `arch/riscv/isa.hh` 与 `arch/riscv/isa.cc`

阶段 5 的核心应继续收敛在 ISA helper 层。

建议新增的 helper 类别如下：

1. `bool shouldApplyLsSsSemantics(ExecContext *xc) const`
2. `RegVal readLsSsKeyLow(ExecContext *xc) const`
3. `RegVal readLsSsKeyHigh(ExecContext *xc) const`
4. `RegVal buildLsSsTweak(ExecContext *xc, RegIndex base_reg_idx, Addr addr) const`
5. `RegVal packLsSsPlain(ExecContext *xc, RegIndex data_reg_idx) const`
6. `RegVal qarma64Encrypt(RegVal plain, RegVal tweak, RegVal keyl, RegVal keyh, int rounds) const`
7. `RegVal qarma64Decrypt(RegVal cipher, RegVal tweak, RegVal keyl, RegVal keyh, int rounds) const`
8. `RegVal finishLsResult(ExecContext *xc, RegIndex rd_idx, RegVal result) const`

这些 helper 的职责划分建议如下：

1. key/tweak/pack/unpack 逻辑放在 `isa.cc`。
2. `.isa` 模板只负责调用 helper，不在模板里散落大段位操作和 QARMA 细节。
3. `finishLsResult()` 负责：
   1. 提取 `result[63:40]`
   2. 写 `GPRID[rd]`
   3. 对 `result[39:0]` 做 40 位符号扩展
   4. 返回最终写入 `rd` 的 64 位值

这样后续 stage6 如果要把解密后处理延后到 `MinorCPU` 的专门阶段，也能继续复用同一组 ISA helper。

### 4.2 QARMA helper 的放置方式

阶段 5 建议把 QARMA helper 独立成 `arch/riscv` 下的新文件，而不是把 QEMU 代码整段粘进 `isa.cc`。

建议结构：

1. `repo/gem5/src/arch/riscv/qarma.hh`
2. `repo/gem5/src/arch/riscv/qarma.cc`

设计要求：

1. 接口最小化，只暴露 gem5 需要的：
   1. `qarma64Enc(...)`
   2. `qarma64Dec(...)`
2. 实现直接复制 QEMU `qarma.h/c`，不在 gem5 中自行改写出另一版等价算法。
3. 常量、sbox、round 常数、LFSR、key/tweak update 与 QEMU 保持一致。
4. 本阶段 rounds 固定使用 `7`，但 helper 接口仍保留 `rounds` 参数，便于未来调试。

### 4.3 `arch/riscv/isa/formats/mem.isa`

阶段 5 推荐复用现有 `Load` / `Store` format，而不是新造一套完全独立的访存模板。

原因：

1. gem5 现有 mem format 已经天然兼容 `AtomicSimpleCPU`、`TimingSimpleCPU` 与 `MinorCPU` 的 `execute/initiateAcc/completeAcc` 结构。
2. `LS` 的解密后处理最自然地落在 load 的 `memacc_code`。
3. `SS` 的加密打包最自然地落在 store 的 `memacc_code`。

建议接入方式：

1. `LS`
   1. 仍走 `Load` format
   2. 先按 64 位取出原始内存值
   3. 若退化路径生效，直接等价 `LD`
   4. 若扩展路径生效，调用 `qarma64Dec + finishLsResult`
2. `SS`
   1. 仍走 `Store` format
   2. 若退化路径生效，直接等价 `SD`
   3. 若扩展路径生效，先 `packLsSsPlain()`，再 `qarma64Enc()`，最后把密文写入 `Mem_ud`

### 4.4 `arch/riscv/isa/decoder.isa`

阶段 5 要在当前 LLVM 编码基线上补齐 gem5 decoder：

1. 在 `MISC_MEM` 对应分支中加入：
   1. `FUNCT3=0b011` 的 `LS`
   2. `FUNCT3=0b100` 的 `LS_MAP` 位置预留
2. 在 `STORE` 对应分支中加入：
   1. `FUNCT3=0b101` 的 `SS`
   2. `FUNCT3=0b110` 的 `SS_ID` 位置预留

本阶段只实现：

1. `LS`
2. `SS`

而 `LS_MAP/SS_ID` 只要求 decode 布局预留清晰位置，不要求进入功能实现。

### 4.5 `arch/riscv/SConscript`

如果 QARMA helper 放到独立 `qarma.cc`，则需要把它加入 RISC-V ISA 的编译单元列表。

本阶段这是一个必要的 build plumbing 改动，不属于额外扩面。

## 5. 为什么这版设计适合 TimingSimpleCPU

阶段 5 的目标虽然写成“simple CPU 功能线”，但这里优先强调 `TimingSimpleCPU`，原因是它正好能复用 gem5 标准访存两段式接口：

1. `initiateAcc()` 负责发起访存。
2. `completeAcc()` 负责在数据返回后完成 load 后处理。

对 `LS` 来说：

1. 访存本身仍按普通 load 发起。
2. 数据返回后在 `completeAcc()` 里做 `qarma64_dec` 与 `rd/gprid` 写回。
3. 因而不需要修改 `cpu/simple/timing.cc` 的主控制流。

对 `SS` 来说：

1. 在发起 store 前就能从 `rs1/rs2/GPRID` 构造完整密文。
2. 因此仍可直接复用普通 store 的 `initiateAcc()` 路径。
3. `TimingSimpleCPU` 不需要知道“这是加密 store”。

这就是为什么 stage5 最应该把变化收敛在 ISA mem format，而不是改 CPU 核心代码。

## 6. 与阶段 4 的衔接关系

这一节非常关键，因为阶段 4 刚刚固定了 Minor 的设计基线。

### 6.1 为什么 stage5 不破坏 stage4 的思路

stage4 的核心结论是：

1. 对非访存整数扩展，Minor 可以在 commit 阶段直接执行 ISA helper。

stage5 并不推翻这条结论，因为：

1. `LS/SS` 天然就是访存指令。
2. gem5 本来就给访存指令提供 `initiateAcc/completeAcc` 两段执行模型。
3. stage5 只是开始把扩展语义接到这条既有模型上。

也就是说：

1. stage4 解决的是“整数扩展怎么在 Minor 上做功能正确”。
2. stage5 解决的是“访存扩展怎么先在 simple/timing CPU 上做功能正确”。
3. 真正涉及 Minor 的 `LS/SS` 时序化，仍然属于 stage6。

### 6.2 为什么不能直接复用整数 ID helper

当前 `shouldApplyIntIdSemantics()` 只在 `U && use=1` 时返回 true。

但 stage5 的 `LS/SS` 必须按新规则：

1. U 态：`use=1` 启用，`use=0` 退化
2. 非 U 态：扩展语义直接生效

所以 stage5 必须新建独立 helper，例如：

1. `shouldApplyLsSsSemantics()`

而不是直接调用：

1. `shouldApplyIntIdSemantics()`

这点如果文档里不先说清楚，后面实现时很容易把 S 态 `LS/SS` 做错成始终退化。

## 7. 测试计划

阶段 5 建议新增两份 baremetal 测试。

### 7.1 `stage5_ls_ss_u_mode.S`

目标：

1. 验证 U 态 `use=1` 时的真正加解密路径。
2. 验证 U 态 `use=0` 时的退化路径。

建议覆盖点：

1. 设置 `SKEYL/SKEYH`
2. 设置 `GPRID[rs1]` 作为 tweak 高 24 位来源
3. 执行一次 `SS` 后再 `LS`
4. 检查：
   1. `REG[rd]` 是否恢复到原 `REG[rs2][39:0]` 的符号扩展结果
   2. `GPRID[rd]` 是否恢复到原 `GPRID[rs2][23:0]`
5. 把 `use` 清零后再做同地址 `SS/LS`
6. 检查其行为是否与 `SD/LD` 完全一致

### 7.2 `stage5_ls_ss_s_mode.S`

目标：

1. 验证 S 态下使用 `MKEYL/MKEYH`。
2. 验证非 U 态不受 `use=0` 退化规则影响。

建议覆盖点：

1. 设置 `MKEYL/MKEYH`
2. 给 `rs1/rs2` 分别写入已知的 `GPRID`
3. 执行 `SS -> LS`
4. 检查结果是否闭环
5. 把 `IDCSR.use` 清零后重复一次
6. 检查是否仍然走扩展路径

### 7.3 额外建议的断言点

1. 边界地址值：
   1. `REG[rs2][39] = 0`
   2. `REG[rs2][39] = 1`
   3. 验证 40 位符号扩展是否正确
2. 边界 ID 值：
   1. `GPRID = 0`
   2. `GPRID = 1`
   3. `GPRID = 0xFFFFFF`
3. `rd=x0`
   1. `REG[x0]` 仍为 0
   2. `GPRID0` 不被 `LS` 覆盖

## 8. 预估改动范围

如果按这份设计推进，阶段 5 预计主要改动 6 到 9 个模块：

1. `repo/gem5/src/arch/riscv/isa.hh`
2. `repo/gem5/src/arch/riscv/isa.cc`
3. `repo/gem5/src/arch/riscv/qarma.hh`
4. `repo/gem5/src/arch/riscv/qarma.cc`
5. `repo/gem5/src/arch/riscv/SConscript`
6. `repo/gem5/src/arch/riscv/isa/decoder.isa`
7. `repo/gem5/src/arch/riscv/isa/formats/mem.isa`
8. `benchmark/simple-sigriscv-test/gem5_test/tests/stage5_ls_ss_u_mode.S`
9. `benchmark/simple-sigriscv-test/gem5_test/tests/stage5_ls_ss_s_mode.S`

## 9. 本阶段的审查重点

在真正开始写代码前，我建议你先重点审核下面五点：

1. `LS/SS` 的 privilege/use 使能规则是否按“U 态受 use 控制，非 U 态始终启用”固定。
2. key 的 low/high 组合是否按 `{SKEYL,SKEYH}` 与 `{MKEYL,MKEYH}` 固定。
3. tweak 是否固定为“U 态 `{GPRID[rs1][23:0], addr[39:0]}`，S/M 态 `{24'b0, addr[39:0]}`”。
4. `LS` 是否固定写 `REG[rd] = sext(result[39:0])` 且 `GPRID[rd] = zext(result[63:40])`。
5. `SS` 是否固定打包 `plain = {GPRID[rs2][23:0], REG[rs2][39:0]}`。

如果这五点通过，stage5 的实现路径就已经足够清楚，后面可以直接开始代码落地。

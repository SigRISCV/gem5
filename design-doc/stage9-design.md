# 阶段 9 设计与实现总结：完整的 ID 传播规则、模板重构、测试覆盖与 baremetal 配置补全

## 1. 阶段 9 的目标

阶段 9 的目标是在 stage8 基线之上，把整数寄存器 `gprid` 的传播与清空规则整理成一套完整、稳定、可维护的实现，并补齐下面四类内容：

1. 普通整数指令的默认 `clearId` 规则。
2. 少数特例指令的显式 ID 传播/写入规则。
3. 压缩指令与普通指令保持一致的 ID 语义。
4. baremetal 配置中 `rdtime` 所需的 `RiscvSystem + CLINT + RTC` 后端。

本阶段的重点不再是继续发散式地给单条指令补丁，而是把“默认行为”和“特例覆盖”的边界收干净，避免后面继续出现：

1. 模板层默认逻辑和 decode 层显式逻辑互相打架。
2. `id_code` 参数插入位置导致旧指令位置参数被误解析。
3. 压缩指令 `rd == rs1` 时被“先 clear 后传播”破坏。
4. 测试只覆盖部分路径，无法确认扩展指令和杂项整数指令的默认清零行为。

## 2. 阶段 9 的最终设计原则

### 2.1 默认规则

对于所有“写整数 `rd`”的普通指令，默认规则是：

1. 若该指令没有显式 ID 语义，则执行结束后 `rd.gprid <- 0`。
2. 默认清零只影响整数目标寄存器，不应误伤浮点、向量、branch-only、store-only 指令。

这一点通过公共执行模板中的：

1. `clearInstDestIntRegIdIfNeeded(...)`

来统一实现。

### 2.2 特例规则

只有少数指令需要显式覆盖默认规则。这些规则包括：

1. `add` 类传播规则。
2. `sub` 类传播规则。
3. `addi` 类传播 `rs1`。
4. `jal/jalr/auipc` 类传播 `pcid`。
5. `setdummyid/setrawid/setnewid` 直接写特定 ID。
6. `switchs/ret` 对 `USE/PUSE/EXITRAW` 的提交时修改。
7. `ls/ls.map/ss/ss.id` 对 `gprid/encmap` 的 LS/SS 语义。

### 2.3 设计边界

本阶段明确采用下面两个边界：

1. `CSR` 类指令不纳入本阶段的默认 `clearId` 覆盖范围。
   原因：本阶段的 ID 传播主要服务 U 模式路径，而大多数 `csr*` 在当前软件使用中位于 M/S 模式。
2. `ls/ls_map` 不应被 load 公共模板默认 `clearId` 误伤。
   原因：它们有自己独立的 LS/SS 语义，不属于普通 `lb-ld` 这类整数 load。

## 3. ID 规则的最终语义

### 3.1 `add` 传播规则

`add` 类指令采用：

1. 若 `rs1.id != 0`，则 `rd.id <- rs1.id`
2. 若 `rs1.id == 0`，则 `rd.id <- rs2.id`

实现 helper：

1. `propagateAddIntRegId(...)`

当前覆盖：

1. `add`
2. `c_add`

### 3.2 `sub` 传播规则

`sub` 类指令采用：

1. 若 `rs2.id == 0`，则 `rd.id <- rs1.id`
2. 若 `rs1.id != 0 && rs2.id != 0`，则 `rd.id <- 0`
3. 若 `rs1.id == 0 && rs2.id != 0`，则当前实现也写 `0`

实现 helper：

1. `propagateSubIntRegId(...)`

当前覆盖：

1. `sub`
2. `c_sub`

### 3.3 直接传播 `rs1`

这类指令直接采用：

1. `rd.id <- rs1.id`

实现 helper：

1. `propagateIntRegIdFromRs1(...)`

当前覆盖：

1. `addi`
2. `c_addi`
3. `c_addi4spn`
4. `c_addi16sp`
5. `c_mv`

说明：

1. `addiw/addw/subw/c_addw/c_subw/c_addiw` 最终都不做传播，而是默认清零。
2. 这是阶段 9 明确收敛后的结论。

### 3.4 `pcid` 传播规则

跳转与控制流类采用：

1. `rd.id <- pcid`

实现 helper：

1. `propagatePcidToIntRegId(...)`

当前覆盖：

1. `auipc`
2. `jal`
3. `jalr`
4. `c_jal`
5. `c_jalr`
6. `switchs`

### 3.5 直接写固定值或新 ID

当前规则：

1. `setdummyid`：`rd.id <- 1`
2. `setrawid`：`rd.id <- 0`
3. `setnewid`：`rd.id <- idgen`

实现 helper：

1. `writeIntRegIdImmediate(...)`
2. `allocateIntRegNewId(...)`

### 3.6 默认清零规则

凡是“写整数 `rd`，但没有显式 special ID 规则”的指令，统一默认：

1. `rd.id <- 0`

典型覆盖：

1. 普通整数 load：`lb/lh/lw/ld/lbu/lhu/lwu`
2. 压缩整数 load：`c_lw/c_ld/c_lbu/c_lhu/c_lh/c_lwsp/c_ldsp`
3. 立即数/逻辑类：`slti/sltiu/xori/ori/andi/srli/srai/lui`
4. 压缩立即数/逻辑类：`c_li/c_lui/c_srli/c_srai/c_andi/c_slli`
5. W 类：`addiw/addw/subw/c_addw/c_subw/c_addiw`
6. 杂项整数扩展类，例如 `mul/div/rem/xor/or/and/sll/sra`

## 4. 阶段 9 的核心重构

### 4.1 helper 层扩展

在：

1. [isa.hh](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa.hh)
2. [isa.cc](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa.cc)

中补齐并整理了下面这些接口：

1. `readPcid()`
2. `readIdCsrUse()`
3. `readIdCsrPuse()`
4. `clearInstDestIntRegIdIfNeeded(...)`
5. `clearIntRegId(...)`
6. `propagateIntRegIdFromRs1(...)`
7. `propagateAddIntRegId(...)`
8. `propagateSubIntRegId(...)`
9. `propagatePcidToIntRegId(...)`
10. `writeIntRegIdImmediate(...)`
11. `allocateIntRegNewId(...)`
12. `applySwitchsCommit(...)`
13. `restoreUseOnUserReturn(...)`

其中最关键的是：

1. `clearInstDestIntRegIdIfNeeded(...)`
   只在当前指令确实有整数目标寄存器时才清零。
2. `propagateAddIntRegId(...)`
   吸收了 `add/c_add` 的专门规则。
3. `propagateSubIntRegId(...)`
   吸收了 `sub/c_sub` 的专门规则。

### 4.2 模板层默认 `clearId`

在公共执行模板中，把默认行为统一为：

1. 正常执行原始指令语义。
2. 执行普通寄存器写回。
3. 对整数目标寄存器执行 `clearInstDestIntRegIdIfNeeded(...)`。

这部分分布在：

1. [basic.isa](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa/formats/basic.isa)
2. [standard.isa](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa/formats/standard.isa)
3. [compressed.isa](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa/formats/compressed.isa)
4. [mem.isa](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa/formats/mem.isa)
5. [amo.isa](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa/formats/amo.isa)
6. [zcmp.isa](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa/formats/zcmp.isa)

另外还统一把模板里默认 clear 使用的局部变量名改成：

1. `idIsa`

从而避免和 `id_code` 自己声明的 `auto isa` 冲突。

### 4.3 解决 `rd == rs1` 被先清零的问题

在早期迭代中，带 `id_code` 的模板曾经采用：

1. 先默认 clear
2. 再执行 `id_code`

这会导致：

1. `addi/c_addi/c_add/c_sub` 这种 `rd == rs1` 的指令，
2. 在传播 helper 读取源寄存器 ID 之前，
3. `rd/rs1` 的 ID 已经先被清成 0

因此最终收敛为：

1. 普通模板：默认 clear
2. 特例模板：不先 clear，而是完全交给显式 `id_code`

这样 `rd == rs1` 的指令不会再被破坏。

### 4.4 `id_code` 与 `opt_flags` 的参数重构

这是阶段 9 最重要的一次结构性修复。

早期做法把 `id_code` 插到了 `opt_flags` 前面，导致很多旧调用，例如：

1. `mul({{...}}, IntMultOp)`
2. `rem({{...}}, IntDivOp)`
3. `c_mul({{...}}, IntMultOp)`

中的第二个位置参数会被错误当成 `id_code`，从而造成：

1. 默认 `clearId` 丢失
2. `IntMultOp/IntDivOp` 被错误塞到生成的执行代码里
3. `mul/rem/c_mul` 无法按普通整数指令默认清零

最终采用的方案不是继续做“兼容猜测补丁”，而是明确拆分两类 format：

1. 普通 format
   1. `ROp/IOp/Jump/SwitchOp/UOp/JOp`
   2. `CROp/CIAddi4spnOp/CIOp/CJOp/CompressedROp/CJump`
   3. 完全保留老的位置参数语义
2. 特例 format
   1. `IdROp/IdIOp/IdJump/IdSwitchOp/IdUOp/IdJOp`
   2. `IdCROp/IdCIAddi4spnOp/IdCIOp/IdCJOp/IdCompressedROp/IdCJump`
   3. 专门给需要显式 ID 规则的指令使用

这部分主要修改在：

1. [standard.isa](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa/formats/standard.isa)
2. [compressed.isa](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa/formats/compressed.isa)

对应好处是：

1. `mul/rem/c_mul` 自动回到老的参数路径
2. 特例指令只在 decode 中显式选用 `Id*` format
3. 不再需要在 format 层猜测某个位置参数到底是 `OpClass` 还是 `id_code`

### 4.5 `decoder.isa` 的特例切换

在：

1. [decoder.isa](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa/decoder.isa)

中，所有需要显式 ID 规则的指令都切换成使用对应 `Id*` format。

典型例子：

1. `IdROp::add`
2. `IdROp::sub`
3. `IdIOp::addi`
4. `IdUOp::auipc`
5. `IdJump::jalr`
6. `IdJOp::jal`
7. `IdSwitchOp::switchs`
8. `IdCIAddi4spnOp::c_addi4spn`
9. `IdCIOp::c_addi`
10. `IdCIOp::c_addi16sp`
11. `IdCJOp::c_jal`
12. `IdCJump::c_jalr`
13. `IdCompressedROp::c_add`
14. `IdCompressedROp::c_sub`
15. `IdCROp::c_mv`

而像下面这些普通指令则不再显式干预：

1. `mul`
2. `rem`
3. `c_mul`
4. 大量普通扩展整数指令

它们全部回到默认 `clearId`。

## 5. Load/Store 与 LS/SS 的收口

### 5.1 普通 load 的默认规则

在：

1. [mem.isa](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa/formats/mem.isa)

中，`LoadStoreBase(...)` 被收敛成统一机制：

1. 普通 `Load/HyperLoad` 默认 `id_code` 即为 `clearInstDestIntRegIdIfNeeded(...)`
2. `id_code is None` 或者 DSL 传下来的字符串 `"None"` 时，统一回落到默认 clear

这样解决了早期生成代码中出现：

1. `None;`

导致编译失败的问题。

### 5.2 `ls/ls_map` 不受默认 clear 误伤

`ls/ls_map` 仍在 [decoder.isa](riscv-spike-sdk/repo/gem5/src/arch/riscv/isa/decoder.isa) 里显式使用：

1. `id_code=';'`

从而覆盖普通 load 的默认清零。

最终效果：

1. `lb/lh/lw/ld/...` 继续默认 clear
2. `ls/ls_map` 不被模板额外清零
3. `ls_map` 自身保留“未加密寄存器时显式 clear”的语义，这属于指令本身逻辑，而不是模板误伤

## 6. 压缩指令的参数索引与语义对齐

阶段 9 还重新梳理了压缩指令的源/目的寄存器语义，确保 helper 接收的是“语义正确”的寄存器索引。

结论如下：

1. `c_add`
   1. 语义等价于 `add rd, rd, rs2`
   2. 对应使用 `srcRegIdx(0), srcRegIdx(1), destRegIdx(0)`
2. `c_sub`
   1. 语义等价于 `sub rd, rd, rs2`
   2. 最终切到 `propagateSubIntRegId(...)`
3. `c_addi`
   1. 语义等价于 `addi rd, rd, imm`
4. `c_addi4spn`
   1. 语义等价于 `addi rd', sp, imm`
   2. 显式传播 `sp` 的 ID
5. `c_addi16sp`
   1. 语义等价于 `addi sp, sp, imm`
6. `c_mv`
   1. 语义上相当于 `mv rd, rs2`
   2. 实现上采用“把唯一数据源传给 `propagateIntRegIdFromRs1(...)`”
7. `c_jal/c_jalr`
   1. 都采用 `pcid` 传播

同时，阶段 9 明确取消了这些 32 位 W 类压缩指令的额外传播：

1. `c_addw`
2. `c_subw`

它们现在都回到默认 `clearId`。

## 7. 测试设计

### 7.1 `stage9_umode_id.S`

新增并完善：

1. [stage9_umode_id.S](riscv-spike-sdk/benchmark/simple-sigriscv-test/asm_test/tests/stage9_umode_id.S)

这份测试是阶段 9 的核心回归，覆盖多阶段 U 模式 ID 语义：

1. `phase0`
   1. `add/sub` 的 3 类传播情况
2. `phase1`
   1. 压缩传播类
   2. `c_add/c_sub/c_addi/c_addi4spn/c_addi16sp/c_mv/c_jalr`
3. `phase2`
   1. `pcid` 传播与 `setdummyid/setrawid/setnewid`
4. `phase3`
   1. 普通非特例整数指令的默认 clear
5. `phase4`
   1. 压缩普通指令的默认 clear
6. `phase5`
   1. 杂项整数扩展类默认 clear
   2. 例如 `mul/div/rem/xor/or/and/sll/sra`

### 7.2 其它回归

为了确认这次模板重构没有破坏已有功能，还回归了：

1. [stage2_id_mode.S](riscv-spike-sdk/benchmark/simple-sigriscv-test/asm_test/tests/stage2_id_mode.S)
2. [stage5_ls_ss.S](riscv-spike-sdk/benchmark/simple-sigriscv-test/asm_test/tests/stage5_ls_ss.S)
3. [stage7_ls_map_ss_id.S](riscv-spike-sdk/benchmark/simple-sigriscv-test/asm_test/tests/stage7_ls_map_ss_id.S)

这三份分别保证：

1. stage2 原始 U 模式传播/清零语义不退化
2. LS/SS 路径不被默认 `clearId` 误伤
3. `LS.MAP/SS.ID` 与 `ENCMAP` 相关功能不回归

## 8. 回归执行与结果

阶段 9 使用下面这些命令完成回归：

```bash
env LD_LIBRARY_PATH=/home/zyy/miniconda3/lib:$LD_LIBRARY_PATH \
toolchain/bin/gem5.opt -d m5out-stage9 \
repo/gem5/configs/sigriscv/baremetal.py \
--kernel benchmark/simple-sigriscv-test/asm_test/build/stage9_umode_id.elf \
--max-ticks 0
```

```bash
env LD_LIBRARY_PATH=/home/zyy/miniconda3/lib:$LD_LIBRARY_PATH \
toolchain/bin/gem5.opt -d m5out-stage2 \
repo/gem5/configs/sigriscv/baremetal.py \
--kernel benchmark/simple-sigriscv-test/asm_test/build/stage2_id_mode.elf \
--max-ticks 0
```

```bash
env LD_LIBRARY_PATH=/home/zyy/miniconda3/lib:$LD_LIBRARY_PATH \
toolchain/bin/gem5.opt -d m5out-stage5 \
repo/gem5/configs/sigriscv/baremetal.py \
--kernel benchmark/simple-sigriscv-test/asm_test/build/stage5_ls_ss.elf \
--max-ticks 0
```

```bash
env LD_LIBRARY_PATH=/home/zyy/miniconda3/lib:$LD_LIBRARY_PATH \
toolchain/bin/gem5.opt -d m5out-stage7 \
repo/gem5/configs/sigriscv/baremetal.py \
--kernel benchmark/simple-sigriscv-test/asm_test/build/stage7_ls_map_ss_id.elf \
--max-ticks 0
```

最终结果：

1. `stage9_umode_id`：通过，输出 `Stage2 umode id pass` 与 `All Tests Passed`
2. `stage2_id_mode`：通过，输出 `Stage2 U-mode test pass` 与 `All Tests Passed`
3. `stage5_ls_ss`：通过，输出 `All Tests Passed`
4. `stage7_ls_map_ss_id`：通过，输出 `All Tests Passed`

## 9. baremetal 配置补充：`rdtime` 为什么会 illegal，以及如何修复

### 9.1 问题根因

在当前 gem5 的 RISC-V 实现中：

1. `rdtime` 通过 CSR `time` 读取
2. `CSRExecute` 会对 `time/timeh` 调用 `isa->hpmCounterCheck(...)`
3. `hpmCounterCheck(...)` 对 `time` 有额外要求：
   1. 当前 `System` 必须是 `RiscvSystem`
   2. `RiscvSystem` 必须能提供 `getClint()`
4. `readMiscReg(MISCREG_TIME)` 最终会调用：
   1. `RiscvSystem::tryReadMtime()`
   2. 再读取 `Clint::registers.mtime`

因此，如果 baremetal 配置只是普通 `System()`，并且没有 `Clint`，那么：

1. `rdcycle` 可以工作
2. `rdinstret` 可以工作
3. `rdtime` 会因为缺失 `RiscvSystem + Clint` 后端而抛出 illegal

### 9.2 本阶段的配置修改

在：

1. [baremetal.py](riscv-spike-sdk/repo/gem5/configs/sigriscv/baremetal.py)

中进行了最小修改：

1. `System()` 改为 `RiscvSystem()`
2. 增加 `RiscvRTC`
3. 增加 `Clint`
4. 将 `clint.int_pin <- rtc.int_pin`
5. 将 `clint.pio <- membus.mem_side_ports`
6. 采用标准地址布局：
   1. DRAM 从 `0x80000000` 开始
   2. `CLINT` 位于 `0x2000000`

这样后续 benchmark 若执行 `rdtime`，就具备了正确的 CSR 后端。

### 9.3 为什么采用标准地址布局

最终没有采用“把 CLINT 临时挂到高地址”的折中方案，而是改成：

1. `mem_ranges = [AddrRange(0x80000000, size=args.mem_size)]`
2. `clint.pio_addr = 0x2000000`

原因是：

1. 这是更标准的 RISC-V baremetal/FS 布局
2. 不需要继续保留一个只为避免地址冲突的临时高地址
3. 后续 benchmark、runtime、libc 如果假定常见 MMIO/DRAM 地址空间，更容易兼容

## 10. 阶段 9 的结论

阶段 9 最终完成了三件关键工作：

1. 把 ID 传播规则从“散落在 decode 中的补丁”收敛成“默认 clear + 少数特例覆盖”的稳定结构。
2. 通过拆分 `Id*` format，彻底解决了 `id_code` 插入位置破坏旧位置参数的问题，使 `mul/rem/c_mul` 等普通扩展指令重新回到默认清零路径。
3. 把 baremetal 配置补齐到支持 `rdtime` 的 `RiscvSystem + Clint + RTC` 版本，为后续 benchmark 测试扫清平台依赖问题。

从代码可维护性上看，阶段 9 的主要收益是：

1. 模板层职责更清楚。
2. decode 层只有真正的特例才显式声明 ID 语义。
3. 压缩与非压缩指令采用相同的设计方法。
4. 测试已经能覆盖 add/sub/control/load/imm/compressed/misc 扩展的主要 ID 语义。

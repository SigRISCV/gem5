# 阶段 8 设计计划：`switchs/ret` 在 TimingSimpleCPU 与 MinorCPU 上的提交时切换

## 1. 阶段 8 的目标

阶段 8 的目标是在现有 stage7 基线上，补齐 `SWITCHS` 与 `ret` 的扩展模式切换语义，并保证它们在 `TimingSimpleCPU` 和 `MinorCPU` 上都满足下面三点：

1. `switchs` 的基本控制流行为等价于 `jalr`。
2. `switchs` 对 `IDCSR.use/puse` 与 `EXITRAW` 的修改只在“提交成功”时生效。
3. `ret` 作为 `jalr` 的特殊情况，只在 `U` 模式且 `PUSE=1` 时恢复 `USE/PUSE`。

本阶段不再引入新的访存 timing 子流水线，重点是把“控制流跳转”和“扩展模式开关”放到 gem5 已有的正确提交边界上。

## 2. 本阶段采用的最终语义

### 2.1 `switchs rd, rs1, rs2`

本阶段按用户最新语义实现：

1. 基本控制流等价于 `jalr rd, rs1, 0`。
2. 目标 PC 为 `rs1 & ~1`。
3. `rd` 写入 `pc + 4`，而不是 `rs1 + 4`。
4. 若当前 `priv == U` 且 `USE == 1`，则在提交时执行：
   1. `PUSE <- USE`
   2. `USE <- 0`
   3. `EXITRAW <- pc + 4`
5. 若当前不满足 `priv == U && USE == 1`，则完全退化为普通 `jalr/call`，不修改 `IDCSR/EXITRAW`。

### 2.2 `ret`

本阶段把 `ret` 视为 `jalr x0, x1, 0` 的特殊情况，但恢复逻辑不再依赖 `EXITRAW`：

1. 对返回类间接跳转，先按原有 `jalr` 语义计算 `next_pc`。
2. 若当前 `priv == U` 且 `PUSE == 1`，则在提交时执行：
   1. `USE <- PUSE`
   2. `PUSE <- 0`
3. 若当前不满足 `priv == U && PUSE == 1`，则完全保持原有 `jalr/ret` 语义，不修改 `USE/PUSE`。
4. `EXITRAW` 仍保持软件可见，但本阶段只作为 `switchs` 返回地址记录，不再作为恢复判定条件。

### 2.3 一个必须明确的点

`ret` 的恢复判定不能再用 `shouldApplyLsSsSemantics()`，也不再需要检查 `EXITRAW`。

原因是：

1. `switchs` 生效后会把 `USE` 清零，并把旧 `USE` 保存到 `PUSE`。
2. 真正触发恢复的 `ret` 运行在 `U/use=0` 的 raw 区间里。
3. 因而恢复逻辑必须忽略当前 `USE`，改为检查 `PUSE == 1`。
4. 同理，`switchs` 自身也必须要求 `USE == 1`，否则连续两次 `switchs` 会覆盖唯一的恢复槽位。

这是阶段 8 最重要的语义约束之一。

## 3. 当前代码基线与实现落点

### 3.1 ISA 层现状

目前 `arch/riscv` 已经具备阶段 8 需要复用的大部分状态 helper：

1. `repo/gem5/src/arch/riscv/regs/misc.hh`
   1. 已有 `MISCREG_IDCSR`
   2. 已有 `MISCREG_EXITRAW`
2. `repo/gem5/src/arch/riscv/isa.hh`
   1. 已有 `readIdCsrUse()`
   2. 已有 `readIdCsrPuse()`
   3. 已有 `readPcid()`
   4. 已有整数寄存器 ID 传播 helper
3. `repo/gem5/src/arch/riscv/isa.cc`
   1. 已有 `readIdCsrUse/readIdCsrPuse/readIdCsrIdgen`
   2. 已有 `shouldApplyIntIdSemantics()`
   3. 已有 `shouldApplyLsSsSemantics()`
   4. 已有 `propagatePcidToIntRegId()`

因此阶段 8 不需要再新建一套 shadow CSR 体系，直接在 ISA helper 上补齐 `USE/PUSE/EXITRAW` 的字段访问与更新接口即可。

### 3.2 `jalr` 当前落点

标准 `jalr` 当前已经位于：

1. `repo/gem5/src/arch/riscv/isa/decoder.isa`
2. opcode `0x19`, `FUNCT3 == 0x0`
3. 其现有行为已经包含：
   1. `rd <- NPC`
   2. `NPC <- (imm + rs1) & ~1`
   3. `rd.gprid <- pcid` 的已有 ID 传播

这意味着阶段 8 最稳的做法不是去改 `MinorCPU` 的 branch 框架，而是：

1. 给 `switchs` 复用同类跳转模板。
2. 给 `jalr/ret` 在 ISA execute 阶段追加一个“提交时可见的恢复 helper”。

### 3.3 TimingSimpleCPU 与 MinorCPU 的提交边界

#### TimingSimpleCPU

`TimingSimpleCPU::completeIfetch()` 的非访存路径顺序是：

1. `preExecute()`
2. `curStaticInst->execute(...)`
3. `postExecute()`
4. `advanceInst()`

对当前阶段的非访存控制流指令来说：

1. 一次只有一条指令在飞。
2. `execute()` 成功返回就已经等价于提交。
3. 所以 `switchs/ret` 的 CSR 副作用可以直接放在 ISA execute helper 中完成，不需要额外改 `cpu/simple/*`。

#### MinorCPU

`MinorCPU` 的非访存指令在：

1. `repo/gem5/src/cpu/minor/execute.cc`
2. `Execute::commitInst()`

中调用 `staticInst->execute(...)`，之后才调用 `tryToBranch()` 推进 PC 与 flush younger 指令。

因此对阶段 8 来说：

1. `execute()` 本身就是 Minor 的正确提交点。
2. `switchs/ret` 的 CSR 副作用只要放在 ISA execute helper 中，就天然具备“提交时生效”的性质。
3. 本阶段预计不需要新增 `cpu/minor/lsq.*`、`scoreboard.*`、`fetch*.cc` 的结构字段。

这也是阶段 8 与阶段 6/7 的最大区别。

## 4. 阶段 8 的实现方案

### 4.1 ISA helper 扩展

在 `repo/gem5/src/arch/riscv/isa.hh/.cc` 中新增下面几类 helper：

1. `readExitraw()`
2. `writeExitraw(Addr)`
3. `writeIdCsrUse(bool)`
4. `writeIdCsrPuse(bool)`
5. `applySwitchsCommit(ExecContext *xc, Addr link_addr)`
6. `restoreUseOnUserReturn(ExecContext *xc, Addr next_pc)`

建议把 `IDCSR` 的字段写回都集中到 helper 中，避免在 `.isa` 模板里直接拼 bitfield。

推荐语义如下：

1. `applySwitchsCommit()`
   1. 若当前不在 `U` 态，则直接返回
   2. 若当前 `USE != 1`，则直接返回，不修改 `PUSE/USE/EXITRAW`
   3. 否则读取旧 `USE`
   4. `PUSE <- old USE`
   5. `USE <- 0`
   6. `EXITRAW <- link_addr`
2. `restoreUseOnUserReturn()`
   1. 若当前不在 `U` 态，直接返回
   2. 若当前 `PUSE != 1`，直接返回，不修改 `PUSE/USE`
   3. 若 `next_pc != EXITRAW`，直接返回，不修改 `PUSE/USE`
   4. 否则 `USE <- PUSE`
   5. `PUSE <- 0`

### 4.2 `switchs` 的 decode 与 execute

#### 4.2.1 decode 位置

沿用扩展 opcode `0b1011011`，即当前 `decoder.isa` 里的 `0x16` 分支。

建议新增：

1. `FUNCT3 == 0x4` 对应 `switchs`

#### 4.2.2 指令格式

用户定义是 R-type：`switchs rd, rs1, rs2`。

本阶段的设计选择是：

1. 保持 R-type 编码不变。
2. `rs2` 作为保留字段，本阶段不参与语义。
3. 测试中统一要求 `rs2 = x0`，避免引入无意义依赖。

实现上建议新增一个专用 `SwitchOp` format，而不是硬塞进现有 `Jump`：

1. `Jump` 当前默认是 I-type `jalr` 风格，带 `imm`。
2. `switchs` 是寄存器间接跳转，但 link 与 branchTarget 逻辑和 `jalr` 很接近。
3. 单独加 `SwitchOp` 更容易控制：
   1. 源寄存器索引
   2. `branchTarget()`
   3. disassembly
   4. `IsIndirectControl/IsUncondControl/IsCall`

#### 4.2.3 execute 顺序

`switchs` 的 execute 顺序必须固定为：

1. 先完成普通 `jalr` 语义
   1. `rd <- pc + 4`
   2. `next_pc <- rs1 & ~1`
2. 再执行现有的 `pcid -> rd.gprid` 传播
3. 最后调用 `applySwitchsCommit(xc, pc + 4)`

之所以要把 CSR 切换放在最后，是为了保证：

1. 在 `U/use=1` 下执行 `switchs` 时，`rd.gprid` 仍按跳转前的扩展模式传播 `pcid`
2. `switchs` 自己对返回地址的 link 行为完全等同于 `jalr`

### 4.3 `ret` 的恢复挂点

阶段 8 不新增“ret 指令” decode，而是在现有返回类 `jalr` 路径上补恢复逻辑。

推荐挂点：

1. 标准 `jalr`
2. 可选：`c_jr/c_jalr`

第一版最小范围只要覆盖：

1. `ret -> jalr x0, x1, 0`

即可满足当前 baremetal 测试需求。

建议做法是：

1. 在普通 `jalr` 语义完成后计算 `next_pc`
2. 把 `next_pc` 传给 `restoreUseOnUserReturn(xc, next_pc)`

恢复规则必须再加一层约束：只有 `ret` 的跳转目标恰好等于前一次 `switchs` 写入的 `EXITRAW`，才允许恢复 `USE`。

为了减少误伤，本阶段建议采用更保守的版本：

1. 仅在 `staticInst->isReturn()` 为真时调用 `restoreUseOnUserReturn()`
2. 由 helper 内部检查 `PUSE == 1`
3. 由 helper 内部检查 `next_pc == EXITRAW`

这样恢复语义仍然只绑定到“命中返回槽位的 `ret`”，而不是任何普通 `jalr` 或错误返回地址上的 `ret`。

## 5. 为什么阶段 8 预计不需要改 TimingSimpleCPU/MinorCPU 核心

### 5.1 TimingSimpleCPU

因为 `switchs/ret` 都是非访存控制指令：

1. 不需要 LSQ 新状态
2. 不需要额外 response-ready cycle
3. 不需要 store buffer 协调

对 `TimingSimpleCPU` 来说，只要 ISA execute 在无 fault 情况下完成：

1. CSR 更新已经等价于提交
2. `advanceInst()` 随后再推进 PC

所以功能实现预计全部停留在 `arch/riscv/isa/*`。

### 5.2 MinorCPU

对 `MinorCPU` 来说：

1. `execute()` 是 head-of-pipe 指令的提交点
2. `tryToBranch()` 紧随其后
3. younger 指令在 branch data 生效后被 flush

因此：

1. `switchs` 在 `execute()` 中改 `USE/PUSE/EXITRAW`，正好符合“commit 时修改”
2. `ret` 在 `execute()` 中只在 `U/puse=1` 时恢复 `USE/PUSE`，也正好发生在真正返回提交时
3. 不需要给 `MinorDynInst`、`Fetch1/Fetch2`、`Scoreboard` 增加新的 per-inst metadata

本阶段如果 `MinorCPU` 需要改动，最大概率只会是：

1. 少量 `DPRINTF`
2. 少量名字判定 helper

而不是结构性流水线改造。

## 6. 需要注意的边界情况

### 6.1 `S/M` 态或 `U/use=0` 下的 `switchs`

这时它必须退化成普通 `jalr/call`：

1. `rd <- pc + 4`
2. 跳转照常发生
3. `IDCSR.use/puse` 不改
4. `EXITRAW` 不改

`S/M` 态里的 `switchs` 不会尝试关闭扩展能力；`U/use=0` 下的 `switchs` 也不会覆盖当前 `PUSE`。

### 6.2 `ret` 必须在 `use=0` 时仍然能恢复

这是阶段 8 最容易写错的地方。

若代码仍以“当前扩展语义是否生效”来判定是否恢复，就会导致：

1. `switchs` 已把 `use` 关掉
2. 真正返回时永远进不了恢复路径

因此恢复路径必须单独判断，不能复用 `shouldApplyLsSsSemantics()`。

另外，恢复路径只限制在 `U` 态内；同时必须要求 `PUSE == 1`，这样连续两次 `ret` 不会凭空把 `USE` 打开。

### 6.3 `EXITRAW` 是否清零

按当前需求：

1. `ret` 恢复时不消耗 `EXITRAW`
2. `EXITRAW` 保持原值

这样软件调试最直接，也与现有 `DEBUG_CSR exitraw` 的可观测方式一致。

### 6.4 `switchs` 的 `rd.gprid`

因为 `switchs` 的基本行为要等价 `jalr`，所以：

1. 若在 `U/use=1` 下执行
2. `rd.gprid` 应继续按 `pcid` 传播

不能因为 `switchs` 最后会关掉 `USE`，就让 link 寄存器丢失 `pcid`。

### 6.5 `S/M` 态下的 `ret`

`S/M` 态下的 `ret` 也不应触发阶段 8 的恢复语义：

1. 即使 `EXITRAW` 有值
2. 也只保留普通 `ret/jalr` 的控制流行为
3. 不修改 `USE/PUSE`

原因是阶段 8 的开关语义只服务于 `U` 态进入 raw 区、再从 raw 区返回的流程。

## 7. 本次开发实际修改

本次开发已经按上面的设计把阶段 8 的主体功能落到了 `arch/riscv` ISA 层，并补了对应 baremetal 测试。

### 7.1 ISA helper 落地

实际在 `repo/gem5/src/arch/riscv/isa.hh/.cc` 中补了下面这些 helper：

1. `readExitraw()`
2. `writeExitraw(RegVal exitraw)`
3. `writeIdCsrUse(bool use)`
4. `writeIdCsrPuse(bool puse)`
5. `applySwitchsCommit(ExecContext *xc, Addr link_addr)`
6. `restoreUseOnUserReturn(ExecContext *xc, Addr next_pc)`

其中：

1. `applySwitchsCommit()` 已按最终语义实现：
   1. 仅在 `priv == U && USE == 1` 时生效
   2. 执行 `PUSE <- USE`
   3. 执行 `USE <- 0`
   4. 执行 `EXITRAW <- link_addr`
2. `restoreUseOnUserReturn()` 已按最终语义实现：
   1. 仅在 `priv == U` 时考虑恢复
   2. 仅在 `PUSE == 1` 时考虑恢复
   3. 仅在 `next_pc == EXITRAW` 时真正恢复
   4. 执行 `USE <- PUSE`
   5. 执行 `PUSE <- 0`

这一步的核心收益是：

1. `switchs` 与 `ret` 的扩展状态修改都集中在 ISA helper 中
2. `.isa` 模板只负责在正确的执行点调用 helper
3. `TimingSimpleCPU` 与 `MinorCPU` 都可以复用同一份提交时语义

### 7.2 `switchs` decode 与 execute 落地

本次开发在 `repo/gem5/src/arch/riscv/isa/decoder.isa` 中新增了：

1. 扩展 opcode `0x16` 下的 `FUNCT3 == 0x4`
2. 对应指令名 `switchs`

同时在 `repo/gem5/src/arch/riscv/isa/formats/standard.isa` 中新增了 `SwitchOp` format，用于承载：

1. `switchs rd, rs1, rs2` 的 R-type 编码
2. `rd <- pc + 4`
3. `NPC <- rs1 & ~1`
4. `pcid -> rd.gprid` 传播
5. `applySwitchsCommit(xc, pc + 4)` 的提交时切换

实际顺序与设计保持一致：

1. 先做普通 `jalr` 风格的 link 和 branch target 计算
2. 再做现有整数 ID 传播
3. 最后再做 `USE/PUSE/EXITRAW` 更新

这样可以保证：

1. `switchs` 的基本控制流与 `jalr` 一致
2. `rd.gprid` 使用跳转前的 `pcid`
3. CSR 修改只在指令真正提交时可见

### 7.3 `ret` 恢复挂点落地

本次开发没有新增独立 `ret` decode，而是把恢复逻辑挂在现有返回类跳转上：

1. 标准 `jalr` 路径：`repo/gem5/src/arch/riscv/isa/formats/standard.isa`
2. 压缩返回路径：`repo/gem5/src/arch/riscv/isa/formats/compressed.isa`

具体做法是：

1. 先按原有 `jalr/c.jr/c.jalr` 语义算出 `next_pc`
2. 只在 `staticInst->isReturn()` 为真时调用 `restoreUseOnUserReturn(xc, next_pc)`

因此当前实现的恢复语义已经是：

1. 不是 `ret` 的普通 `jalr` 不恢复
2. 即使是 `ret`，如果 `next_pc != EXITRAW` 也不恢复
3. 只有命中上一次 `switchs` 写入的返回槽位时，才恢复 `USE`

### 7.4 测试落地

本次开发在 `benchmark/simple-sigriscv-test/gem5_test/tests` 中补了阶段 8 测试。

#### 7.4.1 汇编宏

在 `test_macros.inc` 中新增：

1. `SWITCHS rd, rs1, rs2`

当前编码方式是：

1. `.insn r 0x5b, 0x4, 0x00, rd, rs1, rs2`

#### 7.4.2 `stage8_switchs.S`

这个测试覆盖了 timing/minor 都共享的主语义：

1. `U/use=1` 时 `switchs` 会进入 raw 区并在正确 `ret` 后恢复
2. `U/use=0` 时 `switchs` 退化为普通 `jalr`，不修改 `USE/PUSE/EXITRAW`
3. 嵌套 `switchs` 不会在 `use=0` 的 raw 区里覆盖外层 `EXITRAW`
4. 错误返回地址上的 `ret` 不恢复
5. 只有真正返回到 `exitraw == pc+4` 时才恢复

#### 7.4.3 `stage8_switchs_minor_flush.S`

这个测试额外覆盖了 `MinorCPU` 更关心的行为：

1. `switchs` 重定向后 younger fallthrough 指令会被 flush
2. `puse=1` 但返回目标不等于 `exitraw` 时不会误恢复
3. `puse=0` 时普通 `ret/jalr` 不会凭空恢复 `USE`

### 7.5 本次没有改动的部分

本次开发没有对下面这些位置做结构性修改：

1. `cpu/simple/*`
2. `cpu/minor/lsq.*`
3. `cpu/minor/scoreboard.*`
4. `cpu/minor/fetch*.cc`

这与阶段 8 的预期一致，因为：

1. `switchs/ret` 都是非访存控制指令
2. 对 `TimingSimpleCPU` 和 `MinorCPU` 来说，`staticInst->execute()` 本身就是本阶段需要的提交边界
3. 所有状态切换都可以在 ISA execute helper 内完成

### 7.6 当前状态说明

截至本次开发结束：

1. 设计文档已经与当前实现语义对齐
2. 代码改动已经完成
3. 测试用例已经补齐

需要注意的是，当前环境中的 `repo/gem5/build/RISCV/gem5.opt` 在重新构建时出现了异常产物问题，导致本轮无法基于新的二进制继续完成最终回归记录。因此后续在构建环境恢复正常后，建议重新执行：

1. `make run-time TEST=stage8_switchs ...`
2. `make run-minor TEST=stage8_switchs ...`
3. `make run-minor TEST=stage8_switchs_minor_flush ...`

并把最终通过日志补回到本节作为收尾记录。

## 7. 测试设计

阶段 8 计划新增两组 baremetal 测试，二者都在 `benchmark/simple-sigriscv-test/gem5_test/tests/` 下实现。

### 7.1 `stage8_switchs.S`

该测试同时用于 `TimingSimpleCPU` 与 `MinorCPU`，覆盖功能主路径。

建议覆盖以下场景：

1. `U/use=1` 的 `switchs` 主路径
   1. 执行前设置 `IDCSR.use=1, puse=0`
   2. 执行 `switchs`
   3. 在目标块中检查：
      1. `USE == 0`
      2. `PUSE == 1`
      3. `EXITRAW == switchs` 的返回地址
      4. `ra == switchs` 的返回地址
   4. 在 raw 区域执行一条普通整数指令，确认 `use=0` 时它不再更新 GPRID
   5. `ret` 返回后检查：
      1. `USE == 1`
      2. `PUSE == 0`
2. `U/use=0` 的退化路径
   1. 执行前显式设置 `USE=0`
   2. 执行 `switchs`
   3. 检查它退化为普通 `jalr`
   4. 检查 `PUSE/USE/EXITRAW` 保持不变
3. `S/M` 态退化路径
   1. 在 `S` 或 `M` 态执行 `switchs`
   2. 检查控制流仍等价 `jalr`
   3. 检查 `IDCSR/EXITRAW` 不被修改
4. raw 区域里的嵌套 `switchs`
   1. 外层 `switchs` 进入 raw 区
   2. raw 区内再次执行 `switchs`
   3. 因当前 `use=0`，内层只应表现为普通 call
   4. 检查外层 `PUSE/EXITRAW` 未被覆盖
5. 连续两次 `ret`
   1. 第一次 `ret` 在 `U/puse=1` 时恢复 `USE`
   2. 第二次 `ret` 因 `PUSE=0` 退化为普通 return
   3. 检查不会错误重复打开 `USE`

### 7.2 `stage8_switchs_minor_flush.S`

该测试主要针对 `MinorCPU` 的提交与重定向边界。

建议覆盖：

1. `switchs` 后 younger fall-through 指令不会在返回前提前生效
2. `U/puse=1` 的 `ret` 会在提交时恢复 `USE/PUSE`
3. `U/puse=0` 与 `S/M` 态 `ret` 都不会错误恢复 `USE/PUSE`
4. `S/M` 态控制流重定向不会错误触发 `switchs/ret` 的扩展开关

这个测试的重点不是额外 timing 延迟，而是确认：

1. `MinorCPU` 的恢复动作发生在真正提交的返回指令上
2. flush 后不会留下错误的 CSR 状态

## 8. 预计修改文件

第一轮实现预计只会改下面这些文件：

1. `repo/gem5/src/arch/riscv/isa.hh`
2. `repo/gem5/src/arch/riscv/isa.cc`
3. `repo/gem5/src/arch/riscv/isa/formats/standard.isa`
4. `repo/gem5/src/arch/riscv/isa/decoder.isa`
5. `benchmark/simple-sigriscv-test/gem5_test/tests/test_macros.inc`
6. `benchmark/simple-sigriscv-test/gem5_test/tests/stage8_switchs.S`
7. `benchmark/simple-sigriscv-test/gem5_test/tests/stage8_switchs_minor_flush.S`

目前设计上不预期需要改：

1. `repo/gem5/src/cpu/simple/*`
2. `repo/gem5/src/cpu/minor/lsq.*`
3. `repo/gem5/src/cpu/minor/execute.*`

如果实现中发现 `MinorCPU` 对返回类 compressed 指令需要额外识别，再最小化补充。

## 9. 实施顺序

建议按下面顺序落地：

1. 在 `isa.hh/.cc` 增加 `IDCSR.use/puse` 与 `EXITRAW` 的 helper
2. 在 `standard.isa` 新增 `SwitchOp` format
3. 在 `decoder.isa` 挂入 `switchs`
4. 在标准 `jalr` 路径接入返回恢复 helper
5. 先跑 `TimingSimpleCPU` 的 `stage8_switchs`
6. 再跑 `MinorCPU` 的 `stage8_switchs`
7. 最后补 `stage8_switchs_minor_flush`

## 10. 阶段 8 的结论

阶段 8 的实现重点不是“给 Timing/Minor 新造一套控制流状态机”，而是：

1. 利用 gem5 已有的 ISA execute 边界表达 `switchs/ret` 的架构语义
2. 利用 `TimingSimpleCPU` 的单指令提交模型和 `MinorCPU` 的 `commitInst()` 调用点，让这些语义天然在提交时生效

如果按这个方案实现，阶段 8 应该可以在较小改动范围内完成：

1. `switchs` 在功能上等价 `jalr`，同时只在 `U/use=1` 时于提交点关闭 `USE`、保存 `PUSE`、写 `EXITRAW`
2. `S/M` 态下的 `switchs/ret` 始终退化为普通 `jalr/ret`
3. `ret` 只在 `U/puse=1` 时恢复 `USE/PUSE`
4. `TimingSimpleCPU` 与 `MinorCPU` 都不需要新增复杂 pipeline metadata

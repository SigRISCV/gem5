# 阶段 4 设计计划：贴近 Minor 模型的功能正确实现

## 1. 阶段 4 的目标

新的阶段 4 不再追求“把 SIG-RISCV 的 `GPRID/IDGEN` 完整下沉成一套微架构伴随数据流”，而是改成更贴近 `MinorCPU` 实际抽象层次的目标：

1. 在 `MinorCPU` 上保证 `gprid/pcid/idgen/use` 的功能语义正确。
2. 继续使用 `Minor` 现有的 `issue/scoreboard/FU/LSQ/commit` 作为时间模型。
3. 不为 `GPRID` 额外构造复杂的 issue-time forward、future file、ID scoreboard。
4. 将扩展状态的真正读写尽量放到 `execute()/completeAcc()` 阶段，复用 `Minor` 本来“按顺序提交、按顺序真正执行”的风格。

这版阶段 4 的关键词不再是“ID 伴随数据流”，而是：

1. 功能正确优先
2. 时间模型复用 Minor 现有机制
3. 忽略极少数跨 `switchs` 的 `GPR/GPRID` producer 分裂 corner case

## 2. 为什么要重写阶段 4

旧版阶段 4 方案的问题，不在于目标错误，而在于它把 `MinorCPU` 当成了比实际更细的流水线模型。

后续代码阅读表明：

1. `Minor` 会对 issue、FU latency、commit、branch redirect、LSQ、cache hit/miss 做时间建模。
2. 但很多普通整数指令的真正功能执行，最终是在 commit 路径调用 `staticInst->execute()` 时一步完成。
3. `Minor` 并不一比一模拟每一级流水寄存器值、每条 bypass 总线值和所有瞬态 companion 状态。

因此旧方案里这些内容都显得过重：

1. `rs1Id/rs2Id` issue-time 前递
2. `futureIdgen`
3. `stream-change` 上的 companion 状态重同步
4. 针对 rare corner case 的 `GPRID ready` 建模
5. 试图把 `GPRID` 做成与 `GPR` 一样精细的 issue-time 依赖资源

这些内容会显著增加实现复杂度，但对当前阶段真正需要的功能收益有限。

## 3. 对 MinorCPU 的重新理解

### 3.1 Minor 真正建模了什么

`MinorCPU` 主要建模的是：

1. 指令什么时候可以 issue
2. 指令进入哪个 FU
3. FU latency / issue latency / relative latency
4. 内存访问经过 LSQ、cache、memory system 后的延迟
5. 分支预测、stream change、异常和提交顺序

也就是说，`Minor` 是一个时间模型很明确的 in-order pipeline simulator。

### 3.2 Minor 没有完整建模什么

`MinorCPU` 没有完整一比一建模：

1. 每一级流水寄存器里的中间值
2. 每条整数 bypass 总线上的实际值选择
3. 所有 companion 状态的中间瞬态窗口
4. 像 RTL 那样显式的 speculative shadow state 网络

### 3.3 功能执行发生在什么时候

对很多普通整数指令而言：

1. issue 只是决定它什么时候进入 FU / inFlight
2. 真正的 ISA 语义执行，是在 commit 路径里调用 `staticInst->execute()`
3. 结果通过 `ExecContext::setRegOperand()` 直接落到 thread 的寄存器文件

这意味着：

1. `Minor` 不是“issue 时真的算出所有值”
2. 它更接近“issue 负责 timing，commit 时真正按顺序执行功能”

## 4. 新阶段 4 的设计原则

### 4.1 功能正确和时间近似解耦

新阶段 4 采用下面的总体原则：

1. 普通 `GPR` 的 timing 继续由 `scoreboard` 和现有 FU timing 建模。
2. `GPRID/IDGEN/use` 的功能语义，在真正执行时按顺序读写架构态。
3. 不让 `GPRID` 进入 issue-time 的主要 stall 判定。
4. 不为 `IDGEN/use` 维护独立 future state。

因此阶段 4 的目标是：

1. 功能正确
2. timing 大体合理
3. 不追求 `GPRID` 在所有 corner case 下都与 `GPR` 完全 timing 同构

### 4.2 忽略 `GPRID` 对 issue 时机的影响

这是这版新设计最核心的取舍。

直接结论：

1. `issue` 时机只由普通 `GPR`、现有 `scoreboard`、FU timing、LSQ timing 决定。
2. `GPRID` 不参与 issue stall 判定。
3. 不额外设计 `GPRID ready`、`ID scoreboard`、`ID canIssue`。

原因是：

1. `GPR` 与 `GPRID` 的最近 producer 不一致，只会在极少数跨 `switchs/use` 边界的场景出现。
2. 这些 rare case 在真实 ABI 和真实调用序列里通常很难稳定出现。
3. 为了这些 rare case 额外构造复杂的 companion timing 模型，在当前阶段得不偿失。

### 4.3 `switchs` 是语义边界，不是流水线主约束

新阶段 4 认为：

1. `switchs` 会把执行流切成“使用 ID”和“不使用 ID”的两个区域。
2. 跨 `switchs` 的 `GPR/GPRID` producer 分裂属于 rare corner case。
3. 当前阶段不把它当作必须 timing 精确覆盖的主设计约束。

因此：

1. 可以接受 `switchs` 前后的 `GPR` 和 `GPRID` 在 issue 依赖上不完全同构。
2. 阶段 4 只保证真正执行时的功能语义正确。
3. 若未来需要对跨 `switchs` 依赖做更严格 timing 建模，再单独设计阶段 5/6 的扩展方案。

## 5. 阶段 4 的功能语义方案

### 5.1 `GPRID` 的实现原则

新的阶段 4 不再把 `GPRID` 视为 issue-time companion 数据，而是视为一类在真正执行时读写的架构扩展状态。

原则如下：

1. 指令真正执行时，读取当前架构态 `GPRID[rs1]` / `GPRID[rs2]`。
2. 按指令语义计算本条指令应写回的 `rd.gprid`。
3. 若本条指令应写 ID，则在执行完成时直接把结果写入架构态 `GPRID[rd]`。
4. 若本条指令不应写 ID，则保持原有 `GPRID[rd]` 或按既有语义退化。

这样做的含义是：

1. `GPRID` 不再依赖 issue-time 前递
2. `GPRID` 不再依赖 `MinorDynInst` 中的瞬态 companion 槽位
3. `GPRID` 的功能正确性直接依赖顺序执行和顺序提交

### 5.2 `IDGEN` 的实现原则

新的阶段 4 不再维护 `futureIdgen`。

原则如下：

1. `setnewid` 真正执行时，直接从当前架构态 `IDCSR.idgen` 读取旧值。
2. 旧值写入 `rd.gprid`。
3. 同一条指令执行内，再把架构态 `IDCSR.idgen` 更新为下一个可分配值。
4. 由于真正执行是顺序发生的，因此连续 `setnewid` 自然可以在功能上串起来。

这意味着：

1. 不需要 `futureIdgen`
2. 不需要 `stream-change` 上的额外 `idgen` 回滚
3. 错误路径指令只要不真正执行，就不会污染架构态 `idgen`

### 5.3 `use/puse` 的实现原则

对 `use/puse` 也采用同样风格：

1. 不维护 speculative shadow state
2. 不在 issue 阶段做显式 future 传播
3. 直接在真正执行时读取和写回架构态 CSR

阶段 4 只要求：

1. `use` 对扩展语义的开关行为功能上正确
2. `switchs` 能正确改变后续指令是否走 ID 模式

## 6. 指令级语义边界

### 6.1 需要阶段 4 正确支持的内容

1. `setrawid`
2. `setdummyid`
3. `setnewid`
4. `add/sub/addi` 这类传播 `GPRID` 的整数指令
5. `auipc/jal/jalr` 对 `pcid` 的写入
6. `use=0` 时的降级路径
7. `switchs` 前后扩展模式切换的功能语义

### 6.2 阶段 4 明确不追求 timing 精确的内容

1. `GPRID` 对 issue stall 的影响
2. 跨 `switchs` 的 `GPR/GPRID` producer 分裂
3. rare corner case 下 `GPRID` 与 `GPR` 的不同 ready 时机
4. `LS/SS/LS_MAP/SS_ID` 这类后续阶段才会真正依赖 `GPRID` 的指令族

## 7. 模块级实现计划

### 7.1 `arch/riscv/isa.hh` 与 `arch/riscv/isa.cc`

职责：把阶段 2/3 已有的 helper 继续作为功能语义后端使用，并尽量让 `MinorCPU` 直接复用这些架构态读写 helper。

计划改动：

1. 保留已有 `readGprId/readPcid/readIdCsrUse/readIdCsrIdgen/writeGprId/writeIdCsrIdgen` 等 helper。
2. 重新审视当前为 `Minor` 加进去的 defer/future/companion 逻辑，尽可能回到“执行时直接改架构态”的路径。
3. 对 `setnewid`、`setrawid`、`setdummyid` 这类指令，确保其真正执行时能直接更新功能结果。

### 7.2 `cpu/minor/exec_context.hh`

职责：继续作为 `Minor` 指令真正执行时的状态读写入口。

计划改动：

1. 复用已有 `getRegOperand()` / `setRegOperand()` 风格。
2. 如有必要，在这里补充 `GPRID/IDCSR` 的直接访问入口。
3. 不在这里引入额外 speculative state。

### 7.3 `cpu/minor/execute.cc`

职责：只保留阶段 4 需要的最小调度 glue，不再承载复杂 companion 微架构。

计划改动：

1. 删除或撤销旧方案中的：
   1. `futureIdgen`
   2. `rs1Id/rs2Id` issue-time 初始化
   3. `forwardSigriscvIds()`
   4. `canIssueSigriscvIds()`
   5. 各类 `stream-change` / fault 上针对 companion future 的额外同步
2. 保留真正必要的：
   1. 在 commit/execute 之后做最小的 SIG-RISCV 结果收尾
   2. 与异常、中断、branch 正常配合
3. 不把 `GPRID` 做成一套新的 issue-time 资源

### 7.4 `cpu/minor/scoreboard.*`

阶段 4 不修改 scoreboard。

原因很明确：

1. 现有 scoreboard 已经足够建模普通 `GPR` 的 timing
2. 当前阶段明确忽略 `GPRID` 对 issue 时机的影响
3. 因此不需要为 `GPRID` 增加独立 scoreboard 资源

## 8. 测试计划

新的阶段 4 测试仍然以“功能正确优先”为主。

### 8.1 继续复用的主测试

1. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_id_mode.S`
2. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_unid_mode.S`

这两份测试继续承担：

1. `use=1` 主功能回归
2. `use=0` 降级语义回归
3. `gprid/pcid/idcsr` 的基本正确性检查

### 8.2 新增的小型 stage4 测试

建议保留并重写这两类测试，但测试目标改成“功能语义”，而不是“精确前递”：

1. `stage4_minor_functional.S`
   1. 覆盖 `setrawid/setdummyid/setnewid`
   2. 覆盖 `add/addi/sub` 的 `GPRID` 传播
   3. 覆盖 `auipc/jal/jalr` 的 `pcid`
2. `stage4_minor_switchs.S`
   1. 覆盖 `switchs` / `use=0` / `use=1` 的模式切换
   2. 验证降级路径下 `GPR` 改而 `GPRID` 不改
   3. 验证重新进入 `use=1` 后功能语义仍然正确

### 8.3 对 rare corner case 的态度

建议把这类序列明确标成“阶段 4 不做 timing 精确承诺”：

1. `ID-enabled -> ID-disabled -> ID-enabled`
2. 同一寄存器跨 `switchs` 的 `GPR` / `GPRID` producer 不一致
3. load producer 与中间非 ID writer 分裂

它们可以记录为：

1. 设计已知边界
2. 后续阶段再处理

## 9. 预估改动范围

如果按这版新设计推进，阶段 4 的改动范围会明显小于旧方案，主要集中在：

1. `repo/gem5/src/arch/riscv/isa.hh`
2. `repo/gem5/src/arch/riscv/isa.cc`
3. `repo/gem5/src/cpu/minor/exec_context.hh`
4. `repo/gem5/src/cpu/minor/execute.cc`
5. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_id_mode.S`
6. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_unid_mode.S`
7. 视需要新增简化后的 stage4 小测试

这版新设计的收益是：

1. 实现更贴近 Minor 本身的抽象层次
2. 代码显著更简单
3. 功能正确性更容易审阅
4. 不再为了极少数 edge case 承担复杂的微架构模型维护成本

## 10. 结论

新的阶段 4 不是“完整的 ID 伴随流水线建模”，而是：

1. 在 `MinorCPU` 上把 SIG-RISCV 扩展功能真正跑通
2. 保留 `Minor` 原有 timing 模型
3. 放弃过度细化的 `GPRID` issue-time 建模
4. 接受少数跨 `switchs` corner case 不做 timing 精确承诺

这版方案更符合当前阶段的真实需求，也更适合作为从 stage3 重新出发的实现基线。

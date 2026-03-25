# 阶段 4 设计计划：MinorCPU 的 ID 功能正确实现

## 1. 阶段 4 的目标

阶段 4 不再把 `MinorCPU` 当成需要显式建模整套 `GPRID/IDGEN` 伴随数据流的 RTL 风格流水线，而是采用更贴近 gem5 Minor 实际抽象层次的实现方式：

1. 在 `MinorCPU` 上保证 `gprid/pcid/idgen/use` 的功能语义正确。
2. 继续复用 `MinorCPU` 现有的 `issue/FU latency/LSQ/commit` 时序模型。
3. 对 ID 相关的整数指令，不新增 `future GPRID`、`future IDGEN`、ID scoreboard 或单独的前递网络。
4. 把真正的 ID 读写放在 `commitInst()` 里调用 `staticInst->execute()` 的时刻完成。

本阶段的核心判断是：

1. 对 `setrawid/setdummyid/setnewid/add/sub/addi/auipc/jal/jalr` 这类 ID 相关指令，真正功能执行发生在 commit 阶段。
2. 当某条指令进入 `staticInst->execute()` 时，程序序更老的指令已经提交。
3. 因此这些指令可以直接从架构态读取最终的 `GPRID/PCID/IDCSR`，再完成后续计算，不需要额外 shadow companion state。

## 2. 为什么采用这条设计线

### 2.1 Minor 的关键事实

当前 `MinorCPU` 的关键行为是：

1. `issue()` 负责 timing、FU 选择和 scoreboard 约束。
2. 普通非访存指令的真实 ISA 语义，在 `Execute::commitInst()` 中调用 `staticInst->execute()` 时完成。
3. 访存指令分成 `initiateAcc()` 和 `completeAcc()` 两段，但本阶段不覆盖 `LS/SS/LS_MAP/SS_ID`，因此阶段 4 的 ID 主路径仍以普通整数指令为主。

这意味着阶段 4 不需要在 decode、pipe register、旁路网络里显式携带 `rs1_id/rs2_id/rd_id`。

### 2.2 为什么可以直接读架构态

对本阶段覆盖的 ID 指令族，语义依赖的状态主要是：

1. `GPRID[rs1]`
2. `PCID`
3. `IDCSR.use`
4. `IDCSR.idgen`

这些状态在 Minor 的 commit 语义下具有一个很重要的性质：

1. older instruction 已经先执行并提交。
2. younger instruction 在自己执行时看到的就是最终架构态。
3. 所以 `setnewid` 连续分配、`add/addi/sub` 链式传播、`jal/jalr/auipc` 传播 `pcid`，都可以直接通过现有 ISA helper 正确完成。

换句话说，本阶段的实现前提不是“ID 不受 shadow 状态影响”，而是“对当前阶段覆盖的指令，真正执行发生在 commit，因此当下可见的架构态已经是所有 older 指令提交后的最终结果”。

## 3. 与旧版 stage4 方案的区别

历史上已经有一版更激进的 stage4 方案，记录在 `stage4-design-revoke.md` 中。那一版尝试：

1. 给 `MinorDynInst` 增加 `rs1Id/rs2Id` 伴随字段。
2. 为 `IDGEN` 建 `future` 状态。
3. 为 `GPRID` 建 issue-time ready / forward 检查。
4. 在 flush、stream change、fault 上回滚未提交 ID 状态。

这版方案最终放弃，原因是：

1. 它把 Minor 过度当成 RTL 流水线。
2. 实现复杂度明显高于 Minor 自身抽象层次。
3. 当前阶段真正需要的功能正确性，其实可以通过 commit-stage execute 自然得到。

因此当前有效的 stage4 基线是：

1. `stage4-design-revoke.md` 只保留为历史记录。
2. `stage4-design.md` 才是新的实现依据。

## 4. 本阶段的边界

### 4.1 本阶段要完成的内容

1. 让 `MinorCPU` 直接复用阶段 2/3 已在 `arch/riscv/isa.cc` 中实现的 ID helper。
2. 让 `setrawid/setdummyid/setnewid` 在 Minor 上按 commit 顺序正确更新 `GPRID` 和 `IDCSR.idgen`。
3. 让 `add/sub/addi` 在 Minor 上按 commit 顺序正确传播 `rs1.gprid`。
4. 让 `auipc/jal/jalr` 在 Minor 上按 commit 顺序正确传播 `pcid`。
5. 让 branch、jalr、ecall、illegal、load fault 等 flush/trap 场景下，只有真正提交过的 ID 更新生效。

### 4.2 本阶段明确不做的内容

1. 不新增 `future GPRID[32]`。
2. 不新增 `future IDGEN`。
3. 不修改 `cpu/minor/scoreboard.*`。
4. 不在 `cpu/minor/dyn_inst.hh` 和 `cpu/minor/pipe_data.hh` 中增加 companion ID 元数据网络。
5. 不实现 `LS/SS/LS_MAP/SS_ID`。
6. 不在本阶段引入 `switchs/use/puse/exitraw` 的完整前端 shadow 控制流实现。

## 5. 模块级设计

### 5.1 `arch/riscv/isa.hh` 与 `arch/riscv/isa.cc`

职责：继续作为 SIG-RISCV ID 语义的唯一后端。

本阶段直接复用已有 helper：

1. `readGprId()`
2. `writeGprId()`
3. `readPcid()`
4. `readIdCsrUse()`
5. `readIdCsrIdgen()`
6. `shouldApplyIntIdSemantics()`
7. `clearIntRegId()`
8. `propagateIntRegIdFromRs1()`
9. `propagatePcidToIntRegId()`
10. `writeIntRegIdImmediate()`
11. `allocateIntRegNewId()`

设计要求：

1. 这些 helper 不需要区分 `SimpleCPU` 和 `MinorCPU` 两条特殊路径。
2. 在 `MinorCPU` 上，它们直接作用于当前线程的架构态即可。
3. 正确性依赖于 Minor 的 commit 顺序，而不是依赖 defer/future 机制。

### 5.2 `cpu/minor/exec_context.hh`

职责：作为 Minor 在 commit 时执行 ISA helper 的状态入口。

设计要求：

1. 继续让 `ExecContext` 暴露标准 `ExecContext` 接口。
2. 不引入 `MinorCPU` 专用的 ID shadow API。
3. 让 `staticInst->execute()` 在该上下文中直接访问 thread 的真实寄存器和 CSR 状态。

### 5.3 `cpu/minor/execute.cc`

职责：维持 Minor 原有“issue 建 timing、commit 做真正执行”的主结构。

本阶段的关键实现点只有一个：

1. 对非访存整数指令，`Execute::commitInst()` 中的
   `inst->staticInst->execute(&context, inst->traceData)` 必须保持为 ID 语义的真正生效点。

这意味着：

1. older 指令先 commit，先更新架构态 ID。
2. younger 指令随后执行时，直接读到最新架构态 ID。
3. 若 younger 指令因为 branch/fault/trap 被 squash，它根本不会执行，也就不会污染 ID 状态。

### 5.4 `cpu/minor/dyn_inst.hh`

本阶段不要求新增 ID 元数据字段。

原因：

1. 当前阶段不做 issue-time ID companion 建模。
2. `MinorDynInst` 只需要继续承担 Minor 原有的动态指令职责。
3. 把阶段 4 设计强行落到 `MinorDynInst` 元数据，会把实现重新带回已废弃的旧方案。

## 6. 功能语义判断

### 6.1 `setdummyid/setrawid/setnewid`

这些指令在 Minor 上的正确实现方式是：

1. issue 仍按普通整数指令参与 FU 和 scoreboard。
2. 真正的 ID 修改在 commit 时执行 ISA helper。
3. `setnewid` 在执行时直接读取当前架构态 `IDCSR.idgen`。
4. 因为 older 指令已经提交，所以连续 `setnewid` 会自然看到递增后的最新 `idgen`。

### 6.2 `add/sub/addi`

这些指令在 Minor 上的 ID 传播方式是：

1. 数据结果仍按原本整数语义执行。
2. 执行时调用 `propagateIntRegIdFromRs1()`。
3. 由于执行发生在 commit，`rs1` 对应的 `GPRID` 已经是所有 older 指令提交后的最终值。

### 6.3 `auipc/jal/jalr`

这些指令在 Minor 上继续通过 `propagatePcidToIntRegId()` 实现。

本阶段的设计前提仍然是：

1. `pcid` 的功能语义由架构态 helper 提供。
2. 不在 Minor 内部单独建 `pcid` 伴随控制流网络。

## 7. flush / trap / squash 语义

本阶段对 flush 正确性的依赖来自 Minor 现有提交模型，而不是新回滚机制。

结论如下：

1. 已提交的 older 指令，其 ID 更新已经写入架构态，应当保留。
2. 未提交、被 squash 的 younger 指令，不会进入自己的 `staticInst->execute()`，因此不会修改架构态 ID。
3. 因而 branch mispredict、jalr redirect、ecall、illegal instruction、load misaligned 等情况，不需要再为 ID 引入额外回滚逻辑。

这是阶段 4 设计里最重要的简化之一。

## 8. 测试计划

### 8.1 主测试

阶段 4 以两份 Minor 定向测试为主：

1. `benchmark/simple-sigriscv-test/gem5_test/tests/stage4_minor_forward.S`
2. `benchmark/simple-sigriscv-test/gem5_test/tests/stage4_minor_flush.S`

覆盖内容分别是：

1. `stage4_minor_forward.S`
   1. 连续 `setnewid`
   2. `setdummyid/setrawid`
   3. `add/addi` 链式 ID 传播
   4. 不同 producer-consumer 间隔下的功能正确性
2. `stage4_minor_flush.S`
   1. taken branch
   2. untaken branch
   3. `jalr`
   4. `ecall`
   5. illegal instruction
   6. load misaligned
   7. nested redirect/trap

### 8.2 运行方式

当前 baremetal 工程可直接用 Minor 跑：

1. `make run-minor TEST=stage4_minor_forward MAX_TICKS=100000000`
2. `make run-minor TEST=stage4_minor_flush MAX_TICKS=150000000`

注意：

1. 之前较小的 `MAX_TICKS=2000000/4000000` 对 `MinorCPU` 不足，容易误判为“卡死”。
2. 目前实际通过时，`stage4_minor_forward` 退出 tick 约为 `4164000`。
3. `stage4_minor_flush` 退出 tick 约为 `11736000`。

### 8.3 当前验证结果

按当前仓库代码，阶段 4 已完成一轮实际运行验证：

1. `stage4_minor_forward` 在 `MinorCPU` 上通过。
2. `stage4_minor_flush` 在 `MinorCPU` 上通过。
3. 两者都通过 `DEBUG imm=5` 正常退出。

这说明当前阶段 4 的有效实现路线确实是：

1. 依赖 commit-stage execute
2. 直接读写架构态 helper
3. 不额外实现 companion future state

## 9. 结论

阶段 4 的正确实现，不是把 `MinorCPU` 改造成一套新的 ID 伴随流水线，而是：

1. 明确 Minor 的功能执行点在 commit。
2. 明确对当前覆盖的 ID 指令族，执行时所有 older 指令都已经提交。
3. 因而可以直接读取架构态中的最终 `GPRID/PCID/IDCSR`，再完成后续语义计算。

这条设计线和历史旧方案相比更简单、更符合 Minor 抽象层次，也已经被当前两份 stage4 baremetal 测试验证通过。

# 阶段 4 设计计划：MinorCPU 的 ID 伴随数据流

## 1. 阶段 4 的目标

阶段 4 的目标是把阶段 2/3 已经在功能线上跑通的 `gprid/pcid/idgen` 语义，下沉到 `MinorCPU` 的顺序流水线内部，实现真正的“ID 伴随数据流”。

这一阶段的交付重点有五个：

1. `GPRID` 不再通过 `SimpleCPU` 那种“execute 时立刻改架构态”的路径实现，而是改成“架构态提供旧值，流水线通过前递得到最新值，commit 时再落架构态”。
2. `MinorDynInst` 要同时携带 `rs1/rs2` 的普通数据和对应的 `GPRID`，让 ID 真正伴随数据流动。
3. `rd.gprid` 的生成复用 `rs1.gprid` 槽位，并直接由指令类型和 `rd` 字段决定写回来源，不再额外引入一整套独立的 `rd` ID 数据网络。
4. `IDGEN` 不走全流水线前递，而是保留一个小规模 `future IDGEN`，用来支撑连续 `setnewid`。
5. 异常、中断、flush、stream change 时，未提交的 `GPRID/IDGEN` 更新能够自然撤销。

这一版阶段 4 的关键词不是“给 MinorCPU 复制一份 32 项 GPRID future file”，而是“让 GPRID 像普通寄存器值一样按旧值读、按前递拿最新值、按提交落架构态”。

## 2. 对你这版方案的评价

先直接说结论：这版设计比我上一版“`futureGprId[32] + futureIdgen`”更优雅，也更贴近你想要的硬件实现风格；在 `MinorCPU` 上实现起来也更自然。

### 2.1 为什么更优雅

主要有四个原因：

1. `GPRID` 和整数寄存器值保持同一种微架构哲学：架构态里拿到的是旧值，最新值靠流水中的结果和前递拿到，而不是为伴随属性再单独建 32 项 future file。
2. `rd.gprid` 复用 `rs1.gprid` 槽位后，数据通路更统一，`setdummyid/setrawid/setnewid/clear/auipc` 都只是“换一个来源”，而不是“再加一条旁路”。
3. `IDGEN` 单独保留 future 语义是合理的，因为它本质上是一个单寄存器分配器，做前递反而更复杂。
4. 这套思路天然能扩展到后面的 `encmap/puse/use/exitraw`：大规模伴随数据走前递，小规模控制/分配状态走 future CSR。

### 2.2 为什么实现上也更简单

和上一版相比，复杂度主要从“维护 32 项 shadow 状态的一致性”变成了“把 GPRID 前递并入现有数据前递/issue 语义”。

这对 `MinorCPU` 来说更合适，因为：

1. `MinorCPU` 本来就已经有按程序顺序 issue、依赖 scoreboard 保护、再通过 FU/commit 保证顺序完成的框架。
2. `MinorDynInst` 已经是跨 stage 的稳定载体，很适合存 `rs1.gprid/rs2.gprid` 和 `newcsr/newcsridx` 这类中间结果。
3. `IDGEN` 只有一个 future 状态，回滚时同步一次架构态即可，不需要处理 32 个寄存器的 shadow 恢复。

### 2.3 需要明确的代价

这版方案也不是“完全没有代价”，它把复杂度集中在两个地方：

1. `GPRID` 前递要和普通数据前递一起考虑，至少要在阶段 4 设计里明确“前递点”和“优先级”。
2. `MinorCPU` 当前 `Scoreboard` 不显式感知 GPRID，因此必须依赖“整数值依赖已经保证 issue 顺序”这个前提。对阶段 4 要覆盖的整数指令，这是成立的；但文档里要明确这是设计假设。

整体上，这个代价是值得的，因为它换来了更小的状态开销和更整齐的微架构边界。

### 2.4 `GPRID` 前递是否还有额外难点

结论是：如果 `GPRID` 的前递时机、优先级和可见条件严格跟随对应 `GPR`，那么阶段 4 没有新的结构性难点，不需要再为 `GPRID` 设计一套独立的 issue/forward 框架。

原因是当前 `MinorCPU` 的“前递”本质上并不是在 `Execute` 里显式维护一张通用数据旁路表，而是通过下面这套现成机制约束“什么时候允许 issue”：

1. `Scoreboard::markupInstDests()` 记录目标寄存器的 `returnCycle` 和 `writingInst`
2. `Scoreboard::canInstIssue()` 结合 `srcRegsRelativeLats` 和 `cantForwardFromFUIndices` 判断某个源寄存器此时是否已经可见
3. 一旦指令被允许 issue，`ExecContext::getRegOperand()` 读到的就是与该 issue 时机一致的正确值

因此阶段 4 只要满足下面三条一致性约束即可：

1. `GPRID` 的“可前递时机”必须与对应整数结果的 `returnCycle` 保持一致
2. `GPRID` 的来源选择优先级必须与整数值的来源优先级一致，不能出现“值来自较新的 producer，但 ID 仍取较旧 producer”的情况
3. flush、stream change、异常撤销时，值和 ID 必须同时失效，不能只撤销其中一边

在这些约束成立的前提下，`GPRID` 前递的实现难点主要是工程细节，而不是体系结构复杂度：

1. 需要在代码里找到最合适的采样点，把 `rs1Id/rs2Id` 的更新放在与整数源值同一个“已经允许 issue”的时机
2. 需要避免 `auipc/jal/jalr` 这类复用槽位的指令误用真实 `rs1Id`
3. 需要在 trace 或 debug 输出中能看见“值和 ID 同步可见”，方便 bring-up

也就是说，阶段 4 对 `GPRID` 前递的判断应写成：

1. 不存在新的原理性难点
2. 存在需要仔细对齐时机和优先级的实现细节

## 3. 当前代码基线与阶段 4 的约束

### 3.1 当前 `arch/riscv` 代码基线

当前仓库里，阶段 2/3 已经完成了这些工作：

## 附录：放弃这版设计的原因

这份文档记录的 stage4 设计已经决定放弃，原因不是“语义目标错误”，而是这版方案对 `MinorCPU` 做了过多微架构层面的过度建模，与 gem5 Minor 的真实抽象层次并不匹配。

### 1. 这版方案过度把 Minor 当成了 RTL 级流水线

旧方案默认了下面这些前提：

1. `GPRID` 需要像真实硬件里的伴随数据一样在 issue 阶段前递
2. `IDGEN` 需要显式 future state、回滚和 stream-change 同步
3. `GPRID` 的 ready/visible 时机需要和 `GPR` 一样被精细建模
4. `MinorDynInst` / `Execute` 需要显式携带大量中间态

但后续代码阅读表明，`MinorCPU` 并不是这种“完整模拟内部值流动”的模型：

1. `issue` / `scoreboard` / `FU latency` / `LSQ` / `commit` 主要是在做 timing 建模
2. 很多普通整数指令的真实功能执行，是在 commit 路径里调用 `staticInst->execute()` 一步完成
3. `Minor` 不是 RTL 式的“每一级流水寄存器值怎么流、每条 bypass 总线怎么取值”的一比一模拟

因此继续把 `GPRID` 硬塞成一套完整的 future/forward/rollback 微架构，会让设计复杂度明显高于 Minor 本身需要的抽象层次。

### 2. 旧方案把极少数 corner case 放大成了主设计约束

旧方案最大的复杂度来源，是试图让 `GPRID` 对 issue 时机的影响也做到和 `GPR` 一样精确。

但后续分析表明，真正会出现 `GPR` 和 `GPRID` producer 不一致的场景，主要集中在：

1. `switchs` 关闭 `use` 之后，指令仍写 `GPR` 但不写 `GPRID`
2. 之后又重新进入 `use=1` 区域
3. 同一个寄存器的值依赖跨越了“使用 ID / 不使用 ID / 再使用 ID”三个区段

这是一个非常少见的边界情况。真实 ABI 和真实函数调用序列里：

1. `switchs` 往往天然把执行流切成两段
2. 跨段活跃寄存器通常会经过保存/恢复
3. 很多情况下流水线窗口本身已经被调用序列拉开

因此，为了这个 rare corner case 去引入复杂的 `GPRID` issue-time forward / future / scoreboard 扩展，在当前阶段是得不偿失的。

### 3. 旧方案把“功能正确”和“时序精确”混在了一起

对于 `MinorCPU` 来说，更自然的切分应当是：

1. `scoreboard` 负责普通 `GPR` 的 timing 近似
2. `execute()/completeAcc()` 负责按程序顺序完成真正的功能语义
3. 自定义扩展状态只需要在功能上正确，不必都进入 issue-time 微架构建模

旧方案的问题在于：

1. 它把 `GPRID` 的功能正确性和 issue-time timing 精确性绑定在了一起
2. 于是不得不引入 `future IDGEN`
3. 不得不设计 `rs1Id/rs2Id` 前递
4. 不得不处理 flush/stream-change 时的额外 companion 状态同步

这使阶段 4 从“让 Minor 跑通功能”变成了“重造一套比 Minor 更细的流水线伴随状态模型”。

### 4. 重新设计后的原则

因此，这版方案最终被放弃，改为采用更贴近 Minor 抽象层次的新原则：

1. 优先保证功能正确，而不是为少数 corner case 追求过度精细的微架构时序
2. 让时间建模和功能实现解耦
3. issue 时机继续主要由现有 `scoreboard` 和普通 `GPR` 依赖决定
4. `GPRID/IDGEN/use` 等扩展状态尽量在真正执行时直接读写架构态
5. 不为当前阶段引入额外的 future GPRID、复杂前递网络或完整的 ID scoreboard

### 5. 本文档的定位

因此，`stage4-design-revoke.md` 的定位是：

1. 保留这一版被放弃的设计思路和分析过程
2. 作为后续审查“为什么不继续沿这条路做”的记录
3. 不再作为新的 stage4 实现依据

1. `isa.hh/isa.cc` 已经提供 `readGprId/readPcid/readIdCsrUse/readIdCsrIdgen/writeIntRegIdImmediate/allocateIntRegNewId` 等 helper。
2. `decoder.isa` 里的 `setdummyid/setrawid/setnewid` 已经直接调用这些 helper。
3. 当前 `shouldApplyIntIdSemantics()` 的实现基线是“仅 U 态且 `use=1` 生效”。

这意味着阶段 4 必须承接现有语义基线，不能在同一阶段里重新打开“是否 S 态也生效”的问题。

### 3.2 当前 `MinorCPU` 代码基线

当前 `MinorCPU` 有三个很重要的特点：

1. `MinorDynInst` 会从 `Decode` 一直活到 `Execute` commit，非常适合挂伴随 ID 元数据。
2. `ForwardInstData` 只传 `MinorDynInstPtr`，因此只要元数据挂到 `MinorDynInst`，通常不需要扩宽 `pipe_data.hh`。
3. `Execute` 目前是先 commit、再 issue，同一线程严格按程序顺序 issue；`Scoreboard` 只跟踪真实整数/浮点/向量寄存器，不跟踪 `MiscRegClass`。

### 3.3 当前实现和阶段 4 的直接冲突

如果不改设计，直接让 `MinorCPU` 继续执行现有 ISA helper，会有两个问题：

1. helper 会立刻改真实 `ISA::miscRegFile`，不具备未提交状态的回滚能力。
2. younger instruction 如果在 older instruction commit 前就需要消费新的 `GPRID/IDGEN`，会因为只看架构态而读到旧值。

所以阶段 4 必须把“execute 立刻改架构态”拆开：

1. `GPRID` 改成“读旧值 + 前递拿最新值 + commit 写回”。
2. `IDGEN` 改成“future IDGEN + commit 写回 + 异常时同步回架构态”。

## 4. 阶段 4 的边界

### 4.1 本阶段要完成的内容

1. 为 `MinorDynInst` 增加 `rs1.gprid/rs2.gprid`、`newcsr/newcsridx` 等元数据。
2. 在 `Execute` 中增加 `future IDGEN`，但不增加 `future GPRID[32]`。
3. 在 issue/前递路径中，让 `rs1/rs2` 的普通值和 `rs1.gprid/rs2.gprid` 同步获得最新结果。
4. 在 commit 时把 `rd.gprid` 和 `IDGEN` 的结果写回真实架构态。
5. 在 stream change、异常、中断、flush、drain discard 时，将 `future IDGEN` 与架构态重新同步，撤销未提交效果。
6. 让 baremetal 测试在 `MinorCPU` 上覆盖链式传播、`setnewid`、清空规则和 squash 回滚。

### 4.2 本阶段明确不做的内容

1. 不实现 `LS/SS/LS_MAP/SS_ID`。
2. 不实现 `encmap` 的时序更新，但文档会为其预留 `newcsridx` 编码规则。
3. 不实现 `switchs`、`puse/use/exitraw` 的前端控制流 shadow state，但文档会为其预留 CSR 组合编码。
4. 不把 GPRID 扩展成 `Scoreboard` 的独立资源。
5. 不改 `O3CPU`。

## 5. 核心设计决策

### 5.1 `GPRID` 不设 32 项 future file

这是这版方案最关键的决策。

新的阶段 4 不再维护：

1. `futureGprId[32]`
2. `futureGprIdValid`

理由是：

1. 32 项 future GPRID 虽然逻辑上清楚，但状态开销大，且和“GPRID 伴随整数数据流”这一本意不一致。
2. `GPRID` 最适合像普通寄存器值那样处理：架构态读出旧值，流水线里靠前递拿到最新值。
3. 这样 `GPRID` 的回滚天然跟着指令回滚，不需要额外维护一套 32 项撤销逻辑。

因此阶段 4 对 `GPRID` 的总原则是：

1. 架构层 `GPRID[32]` 只保存已提交状态。
2. 指令进入流水线时拿到的是旧 `GPRID`。
3. newer instruction 需要的最新 `GPRID` 通过前递覆盖到 `MinorDynInst.rs1Id/rs2Id`。
4. commit 时才把结果写回真实 `GPRID[rd]`。

### 5.2 `MinorDynInst` 承载旧值、前递值和提交值

阶段 4 推荐把下面这些字段挂到 `MinorDynInst`：

1. `rs1Id`
2. `rs2Id`
3. `writesId`
4. `sigMode`
5. `newcsr`
6. `newcsridx`
字段语义建议如下：

1. `rs1Id/rs2Id`
   1. 只有在 `sigMode=1` 时才有意义
   2. 当 `sigMode=1` 时，初始值来自架构态 `GPRID[rs1]/GPRID[rs2]`
   3. 当 `sigMode=1` 时，后续允许被前递逻辑覆盖为更新值
   4. 当 `sigMode!=1` 时，这两个字段直接置 0，不参与任何 `GPRID` 传播语义
2. `writesId`
   1. 在 U 模式、`use=1`、该指令确实会修改 `rd.gprid`、且 `rd!=x0` 时置 1
   2. commit 时据此决定是否写真实 `GPRID[rd]`
3. `sigMode`
   1. 在 U 模式、`use=1` 时为 1
   2. 或在 S/M 模式且指令应走扩展模式时为 1
   3. 为 0 时表示本条指令按降级模式执行
4. `newcsr`
   1. 存储本条指令执行后、本地最新的 future CSR 结果
5. `newcsridx`
   1. `1` 表示 `IDGEN`
   2. `2` 表示 `ENCMAP`
   3. `3` 表示 `PUSE/USE/EXITRAW` 组合
   4. `0` 表示本条指令不携带 CSR 中间结果
### 5.3 `rd.gprid` 与 `rs1.gprid` 共用一个槽

这里完全采用你的约束：`rd.gprid` 写回值和 `rs1.gprid` 共用一个槽位。

具体地说：

1. 对 `add/sub/addi` 这类传播类指令，`rd.gprid` 直接复用 `rs1.gprid`。
2. 对 `clear/setrawid/setdummyid/setnewid/auipc/jal/jalr` 这类“`rd.gprid` 需要写入、但 `rs1.gprid` 本身并不需要被消费”的情况，`rs1.gprid` 槽位被复用为一个可选来源槽。

但这里不再建议保留 `resultIdSel` 这种中间状态。

阶段 4 更直接的做法是：

1. `rd` 字段直接决定最终写哪个 `GPRID[rd]`。
2. 是否真的写入，由 `writesId` 和 `rd!=x0` 决定。
3. 写入什么值，直接由指令类型决定：
   1. `add/sub/addi` 写当前 `rs1.gprid`
   2. `setrawid` 和清空类写 0
   3. `setdummyid` 写 1
   4. `setnewid` 写当前 `futureIdgen`
   5. `auipc/jal/jalr` 写 `pcid`

这样 `rd.gprid` 的目标由 `rd` 自身表达，来源由指令语义直接表达，不需要再额外保留一个 `resultIdSel`。

### 5.4 `GPRID` 前递是阶段 4 的核心

阶段 4 的真正关键不再是维护 shadow file，而是把 `GPRID` 前递做对。

建议语义是：

1. 指令刚进入 `Execute` 时，从架构态读出 `rs1Id/rs2Id` 的旧值。
2. 在 issue 前，执行一次和整数值前递同源的 `GPRID` 前递检查。
3. 如果 older instruction 已经在流水线里产生了更新后的 `rd.gprid`，那么把该值覆盖到 younger instruction 的 `rs1Id/rs2Id`。
4. 普通值前递和 `GPRID` 前递应保持同一套优先级，避免“值已更新但 ID 还是旧的”。

这里再加一条总门控条件：

1. 只有 `sigMode=1` 时，`rs1Id/rs2Id` 才会被读取、前递和消费。
2. 当 `sigMode!=1` 时：
   1. `rs1Id=0`
   2. `rs2Id=0`
   3. 不执行任何 `GPRID` 前递
   4. 所有需要 `rd.gprid` 的扩展写回也应被视为降级路径

但这里还要进一步收紧到“只在真正需要消费该 ID 的指令上做前递”，而不是默认所有指令都做：

1. `rs1.gprid` 只有在 `add/sub/addi/load/store` 这几类指令上需要前递。
2. 其余指令即使带有 `rs1`，也不要求消费真实 `rs1.gprid`：
   1. 要么根本不会用到 `rs1.gprid`
   2. 要么 `rd.gprid` 的写入来源已经由指令语义固定确定
3. `rs2.gprid` 只有 `store` 需要前递，因为只有这一类指令会真正消费 `rs2` 对应的 ID。
4. 上述前递需求都还要额外满足 `sigMode=1`。

因此阶段 4 不应把 `rs1Id/rs2Id` 前递做成“无条件对所有指令启用”的通用逻辑，而应由指令分类驱动：

1. `needsRs1Id`
2. `needsRs2Id`

其中建议取值为：

1. `needsRs1Id = 1`
   1. `add`
   2. `sub`
   3. `addi`
   4. `load`
   5. `store`
2. `needsRs2Id = 1`
   1. `store`
3. 其他情况均为 0

这样做的好处是：

1. 实现范围最小，不会为了不需要的指令平白增加前递判断
2. `auipc/jal/jalr/setrawid/setdummyid/setnewid/clear` 等指令可以直接走固定来源，不必关心真实 `rs1.gprid`
3. 后续 `LS/SS` 进入阶段 5 时，也能在同一框架下继续扩展“哪些指令需要消费哪个源 ID”

这里要特别强调一个设计假设：

1. 阶段 4 只覆盖那些“整数值依赖已经被现有 scoreboard 保护”的指令族。
2. 但为了让 `GPRID` 的 ready 语义更严谨，阶段 4 可以在 `canIssue` 附近增加一层与整数值平行的 `ID ready` 检查。

进一步地，阶段 4 可以把 `GPRID` 前递视为“跟随 GPR 可见时机的伴随元数据同步”，但这里的“同步”最好显式体现在 issue 判定上，而不是完全依赖 issue 之后再扫描：

1. 对普通 `rs1/rs2`，`Scoreboard::canInstIssue()` 已经会根据最近 producer 的 `returnCycle/srcRegsRelativeLats/cantForwardFromFUIndices` 计算该源值何时 ready
2. 对 `rs1Id/rs2Id`，阶段 4 可以在 `Execute` 中增加一个伴随的 `canIssueSigriscvIds()` 检查，按 `forwardSigriscvIds()` 同样的匹配规则找到最近的 `ID producer`
3. 若找到 producer，则沿用该 producer 对应整数结果的 ready 时机，计算本条指令还需要额外等待多少 cycle
4. 只有当普通值 ready 且 `ID ready` 同时满足时，consumer 才允许 issue
5. issue 之后的 `forwardSigriscvIds()` 则只负责“取值”，不再承担“判断是否该 stall”这一职责

这样做的好处是：

1. 逻辑和现有 Minor 的整数前递模型保持一致，都是“先算 ready，再 issue，再取值”
2. 可以最小化复用现有 `Scoreboard` 的 ready 概念，而不必一开始就把 `GPRID` 扩成一套新的 scoreboard 资源
3. 后续把 `load` 这类 late producer 纳入 `GPRID` 前递时，也更容易把“值 ready”和“ID ready”绑成同一个时机

对实现复杂度的判断如下：

1. 可行性高
   1. 因为当前 `forwardSigriscvIds()` 已经能按 `rs1/rs2 -> dest` 的方式找到最近的 producer
   2. 当前 `Scoreboard` 也已经能提供对应整数寄存器的 `returnCycle` / `writingInst` / `fuIndices`
2. 复杂度中等偏低
   1. 不需要修改前端、解码、FU pipeline 或 LSQ 主逻辑
   2. 主要是在 `Execute::issue()` 旁边补一层 `ID ready` 判定
3. 复用空间大
   1. 可以直接复用 `needsRs1Id/needsRs2Id`
   2. 可以直接复用 `forwardSigriscvIds()` 的 producer 选择原则
   3. 可以直接复用现有 FU timing 的 `srcRegsRelativeLats/cantForwardFromFUIndices`

因此阶段 4 更推荐的最小实现路线是：

1. 不为 `GPRID` 扩展独立 scoreboard 项
2. 在 `Execute` 中新增一个轻量 helper，例如 `canIssueSigriscvIds()` 或 `calcSigriscvIdReadyCycle()`
3. 该 helper 做两件事：
   1. 按 `forwardSigriscvIds()` 的同样规则找到 `rs1Id/rs2Id` 各自最近的 producer
   2. 读取该 producer 对应整数目标寄存器在 scoreboard 中的 ready 时机，换算成 consumer 还需等待的周期
4. 若额外等待周期不为 0，则本条指令先不 issue
5. 当本条指令最终允许 issue 时，再由 `forwardSigriscvIds()` 真的把 `ID` 值填到 `rs1Id/rs2Id`

这也回答了“后续 forward 机制能不能改为直接找到对应的值获得 ID 或者从 scoreboard 得到 ID”：

1. 从 scoreboard 直接得到 `ID` 不合适
   1. 因为 scoreboard 现在只保存“谁在写、何时 ready”，不保存 `GPRID` 数值本身
   2. 就算扩 scoreboard，也会把它从 timing 结构变成 timing+data 的混合结构，改动面更大
2. 直接从 producer 动态指令获得 `ID` 更合适
   1. producer 里的 `MinorDynInst` 已经持有 `rs1Id/newcsr/writesId` 等必要元数据
   2. 当前 `forwardSigriscvIds()` 也是沿这个方向实现的
3. 因此更自然的分工是：
   1. scoreboard 负责“时间”，也就是 ready 时机
   2. `MinorDynInst` / `inFlightInsts` 负责“数据”，也就是实际 `ID` 值

### 5.5 `IDGEN` 仍然保留 future 语义

`IDGEN` 和 `GPRID` 不一样，单独保留 future 机制是合理且更简单的。

推荐在 `Execute` 中增加：

1. `futureIdgen`

其语义如下：

1. `setnewid` 执行时，把当前 `futureIdgen` 值写入 `rs1Id` 复用槽，作为最终 `rd.gprid` 来源。
2. 随后把 `futureIdgen` 推进到下一个可分配值。
3. 同时把本条指令执行后的本地最新 CSR 结果存入 `newcsr`，并令 `newcsridx = 1`。
4. commit 时根据 `newcsridx` 把 `newcsr` 写回真实 `IDCSR.idgen`。

这样后续连续 `setnewid` 就总能看到最新 `IDGEN`，而不需要做多 stage 间的 CSR 前递。

### 5.6 rollback 策略：`GPRID` 靠不提交自然撤销，`future IDGEN` 靠同步撤销

这版方案的 rollback 比我上一版更简洁：

1. `GPRID`
   1. 未提交指令的更新只存在于 `MinorDynInst` 和前递结果里
   2. 只要没 commit，就不会污染架构态
   3. flush 后这些动态指令被丢弃，效果自然消失
2. `future IDGEN`
   1. flush/异常/中断后，把 `futureIdgen` 重新同步为真实架构态 `IDCSR.idgen`
   2. 这样未提交的 `setnewid` 推进会被整体撤销

这个思路也能直接扩展到后续 CSR：

1. `newcsridx = 1`：`IDGEN`
2. `newcsridx = 2`：`ENCMAP`
3. `newcsridx = 3`：`PUSE/USE/EXITRAW`

### 5.7 `sigMode` 与 `writesId` 分开建模

这一点我认为非常好，应该明确写进设计。

两个状态分工如下：

1. `sigMode`
   1. 标记本条指令是否按扩展模式执行
   2. 解决“这条指令是扩展语义还是降级语义”的问题
2. `writesId`
   1. 标记本条指令 commit 时是否真的要写 `rd.gprid`
   2. 解决“这条指令最终是否产生 ID 写回”的问题

这样可以避免把“是否走扩展模式”和“是否写 ID”混成一个位。

## 6. 指令级语义落点

### 6.1 `rd.gprid` 写回来源分类

阶段 4 不再建议保留 `resultIdSel`。

更直接的做法是：在 `Execute` 中按 `RiscvStaticInst::machInst` 或稳定的指令类型信息，直接决定 `rd.gprid` 的写回来源。

建议覆盖范围为：

1. `add/sub/addi` -> 写 `rs1.gprid`
2. `setrawid` -> 写 0
3. 清空类整数写回 -> 写 0
4. `setdummyid` -> 写 1
5. `setnewid` -> 写当前 `futureIdgen`
6. `auipc/jal/jalr` -> 写 `pcid`
7. `rd=x0` -> 不写

### 6.2 `setnewid`

`setnewid` 在 `MinorCPU` 上必须和阶段 3 保持一致：

1. 分配给 `rd.gprid` 的是 `futureIdgen` 的旧值。
2. 这个旧值写进 `rs1Id` 复用槽，作为最终 `rd.gprid` 来源。
3. 随后 `futureIdgen` 推进到下一个可分配值。
4. 推进时仍跳过 `0/1/2`。
5. `rd=x0` 时不改 `GPRID0`，但如果语义生效，仍消耗一次 `futureIdgen`。
6. `newcsridx = 1`，`newcsr` 记录提交时应写回的 `idgen` 结果。

### 6.3 `auipc/jal/jalr`

这组指令不消费真实 `rs1.gprid`，但需要给 `rd.gprid` 写 `pcid`。

因此它们采用复用槽方案：

1. 不要求 `rs1Id` 表示架构意义上的源 ID
2. commit 时若 `writesId=1`，则把 `pcid` 写入 `GPRID[rd]`

### 6.4 `sigMode` 生效条件

按你这次的要求，阶段 4 的设计建议把 `sigMode` 定义为：

1. 在 U 模式且 `use=1` 时，`sigMode=1`
2. 在 S/M 模式时，若该指令应走扩展模式，则 `sigMode=1`
3. 其余情况 `sigMode=0`

但这里要特别说明一个现实约束：

1. 当前仓库实际代码基线仍是“仅 U 态且 `use=1` 生效”
2. 所以阶段 4 真正实现时，如果要让 S/M 模式也置 `sigMode=1`，那会连带改变阶段 2/3 的语义基线

因此文档建议分两层写：

1. 微架构字段设计允许 S/M 模式扩展
2. 阶段 4 落地时默认先保持“仅 U 态且 `use=1` 生效”，除非你明确要求同步修正阶段 2/3 语义

## 7. 本次已实现的模块与功能

这一节记录当前仓库里已经落地的 stage4 代码实现，而不是仅停留在设计计划。

### 7.1 `cpu/minor/dyn_inst.hh`

职责：为 `MinorDynInst` 增加 SIG-RISCV 伴随元数据。

已实现字段：

1. `rs1Id`
2. `rs2Id`
3. `newcsr`
4. `newcsridx`
5. `writesId`
6. `sigMode`
7. `needRs1Id`
8. `needRs2Id`

这些字段分别承载：

1. `rs1Id/rs2Id`：issue 时读取的旧 `GPRID`，以及经前递覆盖后的最新 `GPRID`
2. `newcsr/newcsridx`：本条指令执行后应在 commit 写回的 future CSR 结果，目前只实际使用 `IDGEN`
3. `writesId`：本条指令 commit 时是否真的写 `GPRID[rd]`
4. `sigMode`：本条指令是否按 SIG 扩展路径执行
5. `needRs1Id/needRs2Id`：本条指令是否真的需要消费 `rs1/rs2` 对应的 `GPRID`

### 7.2 `cpu/minor/exec_context.hh` 与 `cpu/exec_context.hh`

职责：把阶段 2/3 已经写在 ISA helper 中的“立刻改架构态”语义，对 `MinorCPU` 改成 defer 模式。

已实现内容：

1. 在基类 `ExecContext` 中增加 `deferRiscvIntRegIdSemantics()` 虚接口，默认返回 `false`
2. 在 `Minor` 的执行上下文中 override 该接口并返回 `true`

这样 `arch/riscv/isa.cc` 中的 `setrawid/setdummyid/setnewid/clear` 一类 helper 在 `MinorCPU` 上不会直接写真实 `miscRegFile/GPRID`，而是把语义下沉到 `Execute`

### 7.3 `arch/riscv/isa.hh` 与 `arch/riscv/isa.cc`

职责：保留阶段 2/3 的功能语义，同时为 `MinorCPU` 提供可 defer 的 helper 边界。

已实现内容：

1. 保留 `readGprId/readPcid/readIdCsrIdgen/writeIntRegIdImmediate/allocateIntRegNewId` 等已有 helper
2. 在涉及整数 ID 语义的 helper 中增加 `deferRiscvIntRegIdSemantics()` 判断
3. 当执行上下文来自 `MinorCPU` 时，helper 不再直接改真实架构态，而是让 `Execute` 自己完成 `GPRID/IDGEN` 更新

这一步是 stage4 能落到 `MinorCPU` 的前提，否则 ISA helper 会提前污染架构态，无法回滚

### 7.4 `cpu/minor/execute.hh` 与 `cpu/minor/execute.cc`

职责：这是 stage4 的核心实现位置，负责 metadata 初始化、`GPRID` 前递、future `IDGEN` 管理、commit 写回和回滚同步

已实现内容如下：

1. 为每个线程维护 `sigriscvFutureState.idgen`
2. 增加 `isSigriscvSigMode()` 与 `isSigriscvUSigMode()` 两套判定
3. 增加 `needsRs1SigriscvId()` 与 `needsRs2SigriscvId()` 分类，并将结果缓存到 `MinorDynInst.needRs1Id/needRs2Id`
4. 增加 `initSigriscvInstMetadata()`
   1. 计算 `sigMode`
   2. 在 `sigMode=1` 时从架构态读取旧 `rs1Id/rs2Id`
   3. 在 `sigMode!=1` 时直接把 `rs1Id/rs2Id` 置零
   4. 对 `setnewid` 预分配 `futureIdgen`，把分配给 `rd.gprid` 的值写入 `rs1Id` 复用槽
   5. 把执行后的最新 `idgen` 写入 `newcsr/newcsridx`
5. 增加 `applySigriscvInstResults()`
   1. commit 时按 `writesId` 更新真实 `GPRID[rd]`
   2. commit 时按 `newcsridx` 更新真实 CSR，目前是 `IDCSR.idgen`
6. 增加 `forwardSigriscvIds()`
   1. 仅在 `sigMode=1` 且 `needRs1Id/needRs2Id` 为真时执行
   2. 从 `inFlightInsts` 逆序扫描最近的 older producer
   3. 只有 producer `writesId=true` 时才允许作为前递源
   4. 当前实现仅对 stage4 已覆盖、且 `rd` 与 `rd.gprid` 在同一时机 ready 的 producer 生效
7. 增加 `writesTrackedSigriscvCsr()`
   1. 遍历 `destRegIdx`
   2. 目前识别 `MISCREG_IDCSR`
   3. 当显式写 `IDCSR` 时，同步 `futureIdgen`
8. 增加 `syncFutureIdgenFromArch()` 和 `syncSigriscvFutureStateFromArch()`
   1. 初始化时同步
   2. 显式写 `IDCSR` 时同步对应 future
   3. 异常、访存 fault、中断提交时同步
   4. 真实 `stream change` 发生时同步，回滚 wrong-path 上 speculative `futureIdgen`
9. 在 `Execute::evaluate()` 中，当 `branch.isStreamChange()` 时，于本周期 issue 之前同步 future 状态

这一版的关键边界是：

1. `GPRID` 不维护 32 项 future file
2. `GPRID` 靠“架构态旧值 + issue 时前递 + commit 写回”实现
3. `IDGEN` 靠单寄存器 future 状态实现
4. 分支预测错误时，wrong-path 指令可能已经 issue，但其 `GPRID` 不会 commit，`futureIdgen` 则通过 stream-change 同步回架构态

### 7.5 `cpu/minor/SConscript`

职责：增加 stage4 bring-up 所需的独立调试开关。

已实现内容：

1. 新增 `DebugFlag('MinorSigriscv', 'Minor SIG-RISCV ID/future-state debug')`

### 7.6 `configs/sigriscv/baremetal.py`

职责：让 baremetal 运行脚本可以直接接受并启用 `MinorSigriscv` 等调试开关

已实现内容：

1. 新增 `--debug-flags`
2. 新增 `--debug-file`
3. 新增 `--debug-start`
4. 在脚本内部启用对应 gem5 debug flag，并支持把 trace 输出重定向到文件

这样用户可以直接运行：

1. `build/RISCV/gem5.opt configs/sigriscv/baremetal.py --cpu-type minor --kernel ... --debug-flags=MinorSigriscv`

而不需要把 debug 参数写在脚本路径之前

## 8. 调试与可观测性

除了功能实现，本次 stage4 还补了专门的调试观测能力，便于 bring-up 和定位前递/回滚问题

### 8.1 `MinorSigriscv` 调试日志

当前 `MinorSigriscv` 会输出两类信息：

1. `forwardSigriscvIds()` 发生前递时：
   1. producer 指令 `pc`
   2. producer 提供的 `ID`
   3. consumer 指令 `pc`
   4. 被前递到的是 `rs1` 还是 `rs2`
   5. 对应的源寄存器编号
2. `syncSigriscvFutureStateFromArch()` 发生同步时：
   1. 对应指令 `pc`
   2. 同步原因字符串，例如 `execute-init`、`stream-change`、`fault-inst-commit`

其中 `syncFutureIdgenFromArch()` 本身不单独打印，避免日志过于噪声

### 8.2 断言编号机制

为方便定位 baremetal 测试失败点，stage1 到 stage4 的测试已统一引入 `assert_id`

1. `TEST_CSR` / `ASSERT_CSR_EQ` 增加显式编号参数
2. 失败前把该编号写入 `t6`
3. `fail` / `except` 路径打印该编号

这样出现 `Test Failed` 时，可以直接知道是该测试文件里的第几个断言失败

## 9. 测试实现与覆盖

### 9.1 复用的既有测试

当前 stage4 仍然复用 stage2 的主测试集：

1. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_id_mode.S`
2. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_unid_mode.S`

它们继续承担：

1. 用户态 `use=1` 的主功能回归
2. 用户态 `use=0` 的降级路径回归
3. 基本 `idcsr/gprid/pcid` 语义检查

### 9.2 新增的小型 Minor 时序测试

本次 stage4 额外补入两份更聚焦 `MinorCPU` 时序边界的测试：

1. `benchmark/simple-sigriscv-test/gem5_test/tests/stage4_minor_forward.S`
   1. 覆盖固定前递间隔 `1/2/3/4/5`
   2. 覆盖连续前递链 `1/2/3/4/5`
   3. 覆盖混合 producer/gap 的前递组合
   4. 同时检查整数值链路与 `GPRID` 链路的一致性
2. `benchmark/simple-sigriscv-test/gem5_test/tests/stage4_minor_flush.S`
   1. 覆盖 branch taken
   2. 覆盖 branch untaken
   3. 覆盖 `jalr` 跳转错误路径
   4. 覆盖 `ecall` trap
   5. 覆盖 illegal instruction trap
   6. 覆盖 load misaligned trap
   7. 覆盖 branch/jalr/异常嵌套场景
   8. 每个 flush/trap phase 后都布置至少 5 条可能污染 `GPRID/IDGEN` 的错误路径指令，用来验证回滚深度

### 9.3 已改动的测试基础设施

本次同时修改了测试基础设施，使 stage4 bring-up 更容易定位：

1. `benchmark/simple-sigriscv-test/gem5_test/tests/test_macros.inc`
2. `benchmark/simple-sigriscv-test/gem5_test/init.S`
3. `benchmark/simple-sigriscv-test/gem5_test/Makefile`

其中 `Makefile` 已记录如何通过 `--debug-flags=MinorSigriscv` 打开调试输出

## 10. 本次实际修改过的文件

截至当前版本，stage4 直接相关、已经修改过的文件包括：

1. `repo/gem5/src/cpu/minor/dyn_inst.hh`
2. `repo/gem5/src/cpu/minor/execute.hh`
3. `repo/gem5/src/cpu/minor/execute.cc`
4. `repo/gem5/src/cpu/minor/exec_context.hh`
5. `repo/gem5/src/cpu/exec_context.hh`
6. `repo/gem5/src/cpu/minor/SConscript`
7. `repo/gem5/src/arch/riscv/isa.hh`
8. `repo/gem5/src/arch/riscv/isa.cc`
9. `repo/gem5/configs/sigriscv/baremetal.py`
10. `benchmark/simple-sigriscv-test/gem5_test/tests/stage1_csr.S`
11. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_id_mode.S`
12. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_unid_mode.S`
13. `benchmark/simple-sigriscv-test/gem5_test/tests/stage4_minor_forward.S`
14. `benchmark/simple-sigriscv-test/gem5_test/tests/stage4_minor_flush.S`
15. `benchmark/simple-sigriscv-test/gem5_test/tests/test_macros.inc`
16. `benchmark/simple-sigriscv-test/gem5_test/init.S`
17. `benchmark/simple-sigriscv-test/gem5_test/Makefile`

## 11. 当前已知边界与后续衔接

当前这版 stage4 已经完成“整数类 ID 伴随数据流 + future `IDGEN` + stream-change/异常同步”的主路径，但仍有清晰边界：

1. `LS/SS/LS_MAP/SS_ID` 还没有进入这一版实现
2. `encmap/puse/use/exitraw` 还未真正接入对应的 future CSR 状态
3. 目前 `writesTrackedSigriscvCsr()` 只真正处理 `IDCSR`
4. `GPRID` 前递当前依赖“`rd` 与 `rd.gprid` 同时 ready”这一契约；后续若要把 load 也纳入 producer，必须继续保证这个契约成立

## 12. 与阶段 5/6 的衔接点

这版阶段 4 完成后，后续阶段会更顺：

1. 阶段 5 的 `LS/SS` 可以直接消费 `rs1Id/rs2Id`。
2. 阶段 6 的 `ENCMAP` 可以直接复用 `future CSR + newcsr/newcsridx` 的处理框架。
3. `switchs` 到来时，`PUSE/USE/EXITRAW` 也可以按相同模式接入，不需要重新发明回滚协议。

换句话说，这版阶段 4 的真正价值是把体系结构切成了两类：

1. 大规模伴随数据状态，如 `GPRID`，走“旧值读取 + 前递 + commit”
2. 小规模控制/分配状态，如 `IDGEN/ENCMAP/PUSE/USE/EXITRAW`，走“future CSR + newcsridx + commit 同步”

这个切分比我上一版更符合你的设计初衷，也更适合作为后续阶段的统一模板。

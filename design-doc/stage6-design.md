# 阶段 6 设计计划：MinorCPU 的 LS/SS 时序化

## 1. 阶段 6 的目标

阶段 6 的目标是在不改变阶段 5 功能语义的前提下，把 `LS/SS` 第一次接入 `MinorCPU` 的 timing 模型。

这一阶段的重点不是重新设计 `LS/SS` 的 ISA 语义，而是回答四个问题：

1. `LS/SS` 在 `MinorCPU` 现有 `issue/FU/LSQ/commit` 框架里，额外延迟应该挂在哪些点。
2. `LS` 的“比普通 `LD` 多 3 周期”到底应该建模在 `scoreboard`、`completeAcc()`，还是两边都建。
3. `SS` 的 3 周期加密延迟应该怎样与地址翻译、排队和 store buffer 并行，而不是粗暴地完全串行。
4. 这套阶段 6 方案是否足够可靠，偏差主要来自哪里。

本阶段默认沿用当前项目基线：

1. `LS/SS` 的功能语义已经在阶段 5 固定。
2. `MinorCPU` 仍然采用阶段 4 的基本原则，即功能最终落在 commit/`completeAcc()` 路径上，而不是为 `LS/SS` 重做一整套独立 companion pipeline。
3. `SWITCHS`、`ENCMAP`、`LS_MAP/SS_ID` 仍不在本阶段真正实现，只为后续阶段预留接口和时序空间。

## 2. 当前 MinorCPU 基线与阶段 6 的约束

### 2.1 `MinorCPU` 当前真实时序边界

从 `cpu/minor` 当前实现看，`LS/SS` 必须适配的是下面这条通路：

1. `Execute::issue()` 决定一条指令何时能发射，并在这里通过 `scoreboard.markupInstDests()` 估计目标寄存器的返回时间。
2. 普通 mem 指令先进入 Mem FU，FU 结束后由 `Execute::commitInst()` 调用 `initiateAcc()`，把访存请求送入 LSQ。
3. 请求在 LSQ 内完成地址翻译、进入 `requests/transfers/store buffer` 队列，并最终发往 memory system。
4. 对 load，真正的数据后处理发生在响应返回后，由 `Execute::handleMemResponse()` 调用 `completeAcc()` 完成。
5. 对 store，真正写入 memory system 的发送点不在 FU，而在 LSQ/store buffer。

因此：

1. `LS` 的“额外等待”天然有两个候选落点：
   1. issue 侧的 `scoreboard` 返回时间。
   2. response 返回后的 `completeAcc()` 可见时间。
2. `SS` 的“额外等待”天然也有两个候选落点：
   1. 进入 LSQ 后立即启动的加密流水线完成时间。
   2. LSQ 真正发射到 memory system 的允许时间。

### 2.2 当前 mem timing 的已有近似

`MinorCPU` 默认 mem FU 配置已经给普通 `LD/SD` 做了一层近似：

1. Mem FU `opLat=1`。
2. 默认 mem timing 带 `extraAssumedLat=2`。
3. 因而普通 `LD` 的目标寄存器在 scoreboard 视角下，大致等价于“发射后 3 周期可用”。

这意味着阶段 6 不应该把 `LS` 当成完全新的流水线类型去重做，而更适合在现有 `LD` 基线之上附加一个固定增量。

### 2.3 当前阶段不宜做的事情

本阶段不建议：

1. 修改 `Scoreboard` 的基本抽象，把 `LS/SS` 变成一套独立资源类型。
2. 给 `MinorCPU` 引入完整的 crypto FU、crypto queue、crypto bypass 网络。
3. 让 `LS/SS` 脱离现有 `Load/Store` 指令模板，重写整个 mem path。

原因不是这些完全不能做，而是它们会把阶段 6 从“时序补丁”扩大成“Minor 后端重构”。

## 3. 阶段 6 的最终 timing 方案

基于前面的分析，阶段 6 最终采用下面这套方案。

### 3.1 功能放置

1. `LS` 的功能语义仍然放在 commit 侧模拟，也就是 load 响应返回后，在 `completeAcc()` 路径完成解密与 `rd/GPRID[rd]` 更新。
2. `SS` 的功能语义仍然放在 commit 侧准备，也就是在 store 进入 LSQ 前就已经得到最终要写入内存的 64 位密文。

这条原则保持和阶段 5 一致，不在本阶段把 QARMA 真正拆成多个 gem5 子阶段。

### 3.2 issue 阶段的 timing 规则

在 issue 阶段维护一个 `future use` shadow，并以该 shadow 为准决定 `LS` 的寄存器结果等待时间：

1. 若指令是 `LS`，且 `future use = 1`，则其 `RD` 的等待时间比普通 `LD` 额外增加 3 周期。
2. 若指令是 `LS`，且当前特权级是 `M/S`，则其 `RD` 的等待时间也比普通 `LD` 额外增加 3 周期。
3. 若指令是 `LS`，且 `future use = 0`，则其 `RD` 等待时间与普通 `LD` 完全相同。
4. `SS` 在 issue 阶段不额外影响 `scoreboard`。

也就是说，本阶段只把 `LS` 的“解密结果何时可被依赖消费者观察到”反映到 scoreboard；`SS` 因为没有整数目的寄存器写回，所以不在 scoreboard 增加额外目标延迟。

### 3.3 commit/load-response 阶段的 timing 规则

`LS` 除了在 issue 时对 scoreboard 额外增加 3 周期外，在 load response 返回后，还要在提交侧再额外等待 3 周期，才允许真正完成 `completeAcc()`。

这样做的原因是：

1. `scoreboard +3` 主要解决 dependent consumer 何时可以 issue 的问题。
2. response 返回后的额外 `+3` 主要解决指令自身 completion/commit 何时真正可见的问题。
3. 这两者观测量不同，因此不是重复收费，而是分别约束“依赖唤醒”和“指令完成”。

因此本阶段对 `LS` 的约束是：

1. issue 侧：`RD.ready = LD.ready + 3`。
2. response 侧：`completeAcc()` 也要在响应到达后再等 3 周期。

### 3.4 LSQ/store 阶段的 timing 规则

`SS` 采用 `crypto_ready_cycle` 设计，而不是“在真正发射前固定再串行等待 3 周期”。

规则如下：

1. 当 `SS` 进入 LSQ 时，如果它走扩展路径，则立刻启动 store-side crypto pipeline 的 timing 计时。
2. 为该请求记录一个 `crypto_ready_cycle`。
3. 在 U 态且 `use=0` 时，`SS` 退化为普通 `SD`，不设置 `crypto_ready_cycle` 约束。
4. 在 S/M 态时，`SS` 始终走扩展路径，必须设置 `crypto_ready_cycle`。
5. LSQ 允许这条 store 真正发往 memory system 的条件，是下面几者同时满足：
   1. 地址翻译完成。
   2. store 顺序约束满足。
   3. store buffer / memory port 条件满足。
   4. `curCycle >= crypto_ready_cycle`。

因此，`SS` 的真正发射时间应理解为：

1. `send_cycle = max(translation_done_cycle, ordering_ready_cycle, crypto_ready_cycle, port_ready_cycle)`。

这就实现了“加密与翻译并行，而不是完全串行”。

## 4. `future use` 的设计

### 4.1 为什么这里需要 `future use`

如果只读当前架构态 `IDCSR.use`，issue 阶段会有一个天然问题：

1. older 指令可能已经在流水线中决定了后续区域的 `use`，
2. 但它还没有真正 commit 到架构 CSR，
3. younger `LS` 若仍只看架构态，就可能按错误的 timing 发射。

因此，阶段 6 至少要在 `Execute` 侧给 `use` 一份 timing-visible shadow。

### 4.2 阶段 6 的 `future use` 边界

本阶段对 `future use` 的态度要比最初版本更保守一些。

基于 `cpu/minor/execute.cc` 当前代码路径，`Execute::evaluate()` 每个周期的顺序是：

1. 先 `commit()`。
2. commit 中对普通非 mem 指令执行 `staticInst->execute()`。
3. 随后用 `tryToBranch()/updateBranchData()` 更新 branch data，并在真实 stream change 时立刻递增 `executeInfo[tid].streamSeqNum`。
4. 最后才进入 `issue()`。

这意味着对 `switchs/jalr/ret` 这类“在 commit 的 `execute()` 中修改 `use`，随后立刻切换 stream”的指令，情况和一般 CSR 写并不完全一样：

1. 这类指令一旦提交，其对 `use` 的修改已经先落到架构可见状态。
2. 紧接着 streamSeqNum 会被更新。
3. 同周期后续 `issue()` 时，老 stream 上的 younger 指令会因为 stream 不匹配而被直接丢弃。
4. 真正存活的新路径指令，会在 branch 之后重新取指，因此能看到最新提交后的 `use`。

因此本阶段的结论是：

1. 若 `use` 的修改只来自 `switchs/jalr/ret` 这类“commit 后立刻切 stream”的指令，则即使不真正实现 `future use`，也可以保证功能正确。
2. 这里仍可能存在老路径 younger `LS/SS` 先按旧 `use` 做少量投机推进甚至进入 LSQ 的情况，但它们最终会因 stream 不匹配被 discard，不会污染最终架构结果。
3. 这类被回滚路径上的 crypto/LSQ 开销，应视为一种 speculative 微架构开销近似，而不是功能错误。
4. 只有当未来出现“修改 `use` 但不立刻切 stream，后继同 stream 指令必须立即看到新 `use`”的场景时，`future use` 才会从“可选优化”变成“功能正确所必需的 shadow state”。

换句话说：

1. 阶段 6 文档保留 `future use` 作为可扩展接口是有价值的。
2. 但对当前以 `switchs/jalr/ret` 为主的 `use` 更新路径，它不是功能正确的必要条件，而主要是后续做更精细 timing 时的扩展口。

## 5. 模块级设计

### 5.1 `cpu/minor/execute.hh` 与 `cpu/minor/execute.cc`

这是阶段 6 的第一改动点。

建议职责如下：

1. 在 `issue()` 中识别当前指令是否为 `LS`。
2. 若是 `LS`，则根据：
   1. 当前 privilege。
   2. 当前可见的 `use` 判定来源。对 `switchs/jalr/ret` 主导的场景，可直接复用提交后架构态；若未来加入“同 stream 立即可见”的 `use` 更新，再扩展为 `future use`。
   3. `LS`/`LD` 指令类型。
   计算是否在当前 mem timing 基线上额外加 3 周期 `extraAssumedLat`。
3. 在处理 load response 的完成路径时，若该指令为扩展路径 `LS`，则在 response 返回后再增加 3 周期 completion hold，才允许真正调用 `completeAcc()` 完成提交。
4. `SS` 在这里不改目的寄存器 ready time。

建议的实现原则：

1. 不去改默认 mem FU 的全局配置。
2. 只对匹配到的 `LS` 动态指令在 issue 与 response-complete 两个时点做局部 timing patch。
3. 普通 `LD/SD` 不受影响。

### 5.2 `cpu/minor/lsq.hh` 与 `cpu/minor/lsq.cc`

这是阶段 6 的第二改动点。

建议在 `LSQRequest` 上新增最小化 timing 元数据，例如：

1. 该请求是否属于扩展路径 `SS`。
2. 该请求的 `crypto_ready_cycle`。
3. 若后续想把 `LS` 的 response 侧也统一放进 LSQ request 元数据，还可预留 `response_ready_cycle`。

本阶段 `SS` 的最小实现应是：

1. 在 `pushRequest()` 或 request 初始化时，识别 `SS` 是否处于扩展路径。
2. 若处于扩展路径，则在请求进入 LSQ 的那个时点启动 store-side crypto timing，并计算 `crypto_ready_cycle`。
3. 在 `tryToSendToTransfers()` 与 store buffer issue 路径中，只有当：
   1. translation 完成，且
   2. `curCycle >= crypto_ready_cycle`，且
   3. 其他原有 LSQ 条件满足
   时，才允许真正发射到 memory system。

这一定义的核心不是“额外再串行等 3 周期”，而是“与翻译并行推进，最终由最晚 ready 的条件决定发射时刻”。

### 5.3 `arch/riscv/isa.hh` 与 `arch/riscv/isa.cc`

本阶段不建议再把时序判断继续写进 `.isa` 模板，而是继续复用阶段 5 已有 helper。

需要的只是把下面这些 helper 当成阶段 6 的功能基线：

1. `shouldApplyLsSsSemantics()`。
2. `readLsSsKeyLow/High()`。
3. `buildLsSsTweak()`。
4. `packLsSsPlain()`。
5. `finishLsResult()`。

也就是说：

1. ISA helper 仍只负责功能正确。
2. `MinorCPU` 自己决定“什么时候算结果 ready、什么时候允许送内存”。
3. 不把 stage6 的 timing policy 反向塞回 ISA 层。

## 6. 为什么 `completeAcc()` 不能替代 scoreboard

这是阶段 6 的一个关键设计结论。

`completeAcc()` 与 `scoreboard` 解决的是两类不同问题：

1. `scoreboard` 负责 issue 侧的“后续依赖指令何时可以把 `rd` 当成 ready 来消费”。
2. `completeAcc()` 负责提交侧的“这条 load 自己何时真正完成并对架构态生效”。

如果只在 `completeAcc()` 额外增加 3 周期，而不在 `scoreboard` 上加：

1. 后续依赖 `rd` 的 consumer 仍会按普通 `LD` 的 timing 被允许 issue。
2. 等到 `completeAcc()` 那里再补 3 周期时，consumer 往往已经提前进入流水线。
3. 功能上虽然不一定出错，因为 `MinorCPU` 还会靠 in-order commit 兜底，
4. 但 `LS` 相对 `LD` 的 load-use latency 会被系统性低估。

所以：

1. 如果只允许在一个地方建模 `LS` 的额外 3 周期，应优先放在 `scoreboard`。
2. 如果还想让指令自身 completion 也更真实，则应再在 response/commit 侧补一段显式等待。
3. 这不是重复计费，而是分别建模“依赖可见时间”和“提交完成时间”。

## 7. 普通 `LD` 的不确定延迟如何处理，以及它对 `LS` 的启示

一个自然的问题是：普通 `LD` 本来就可能因为 cache miss、TLB miss 等原因，真实返回时间远大于默认假设的 3 周期，那么为什么 `MinorCPU` 还能正常工作？

答案是：`MinorCPU` 本来就采用了“assumed latency + commit 兜底”的近似模型。

具体来说：

1. issue 侧，普通 `LD` 用 `scoreboard` 上的估计 ready time 控制 dependent 指令的最早发射时机。
2. 若实际是 hit，这个估计通常接近真实情况。
3. 若实际是 miss，结果会更晚回来，但 younger 指令仍然不能越过 older load 提前错误提交，因为 commit 仍按程序顺序推进。

因此 `MinorCPU` 对 mem ref 的 timing 建模本来就不是“精确预测每次 response 回来的绝对时刻”，而是：

1. 先给一个近似 ready time，用于性能建模。
2. 再由 response/commit 路径保证功能正确。

这对阶段 6 的启示是：

1. `LS` 也完全可以沿用同样的哲学。
2. 普通 `LD` 默认假设为 3 周期，则 `LS` 可以假设为 `LD + 3`。
3. 若还想更接近你的硬件设定，则在 response/commit 侧再补一段固定 3 周期解密等待。

因此，`LS` 采用“issue 侧 `scoreboard +3`，提交侧再 `+3`”是和 `MinorCPU` 现有风格相容的。

## 8. 这套模拟方案的可靠性评估

### 8.1 结论

结论是：这套方案可以作为阶段 6 的第一版 timing 近似，而且在当前约束下已经比“单纯把 `SS` 串行延后 3 周期”更合理。

更具体一点：

1. 对 `LS` 的 load-use 延迟，这个模型是一阶近似上合理的。
2. 对 `LS` 的 completion/commit 可见时间，这个模型也给出了明确的固定增量。
3. 对 `SS`，`crypto_ready_cycle` 让加密与翻译并行，比单纯串行等待更接近你假设的 3 级流水 crypto 模块。
4. 但它仍没有真正建模 crypto 资源竞争、与 cache miss 的深度耦合、以及不同 `LS/SS` 之间更细粒度的共享冲突，因此还不算最终高置信时序模型。

### 8.2 偏差大不大

总体上我认为偏差通常是“小到中等”，比原先那版“`SS` 纯串行多等 3 周期”的偏差更小。

原因如下：

1. 对 `LS`，你现在同时建模了 consumer ready time 和 completion time，因此比只改一个时点更一致。
2. 对 `SS`，由于 `crypto_ready_cycle` 与 translation 可以并行，所以不再系统性把 3 周期全部串在 store 发射之后。
3. 一旦存在 TLB miss、cache miss、store forwarding、barrier、back-pressure，仍然会有误差，但误差方向比旧方案更可控。

### 8.3 偏多还是偏少

#### 对 `LS`

`LS` 现在同时有 issue 侧 `+3` 和 response 侧 `+3`，这在你的目标模型里是合理的，因为它们分别对应：

1. 解密结果何时能被 dependent consumer 观察。
2. 解密模块自身 3 级流水完成后，这条 load 何时真正完成。

在这种解释下，这不是“加多”，而是建模了两个不同观测量。

若未来你发现某些统计口径只关心其中一个观测量，再回头裁剪其中一个时点即可。

#### 对 `SS`

`SS` 的 `crypto_ready_cycle` 方案整体上比旧方案更少偏保守。

原因：

1. 旧方案把 3 周期完全串行挂在发射前，通常偏慢。
2. 新方案允许 translation、排队、部分端口等待与 crypto 同时推进。
3. 因此最终发射时间是多个 ready 条件中的最大者，而不是人为再加一段固定串行时间。

### 8.4 误差来源总结

这套方案的主要误差来自四点：

1. 用固定 3 周期代替真正的 crypto pipeline/resource occupancy 细节。
2. 还没有把“每周期最多接受 1 条 `SS`”的流水线入口竞争完全做细。
3. `LS` 的 response 侧额外 3 周期仍是固定值，没有进一步区分 hit/miss 后端重叠关系。
4. 当前文档虽然保留了 `future use` 扩展口，但对 `switchs/jalr/ret` 主导的 `use` 更新路径，功能正确性已经可以由“commit 后更新架构态 + stream change 丢弃旧路径”来保证；真正需要 `future use` 的，是未来同 stream 立即可见的 `use` 更新场景。

## 9. 有没有更好的修改方案

有，但它们都比当前阶段 6 更重。

### 9.1 更好的“小改版”

当前文档这版其实已经是我认为最合适的小改版：

1. `LS`：issue 侧 `scoreboard +3`，response/commit 侧再 `+3`。
2. `SS`：进入 LSQ 时启动 crypto timing，记录 `crypto_ready_cycle`，发射时取所有 ready 条件的最大值。

这个版本的优点是：

1. `LS` 的 dependent wakeup 和 instruction completion 两边都更一致。
2. `SS` 的可见时间不再被粗暴串行化。
3. 改动仍局限在 `Execute + LSQRequest + LSQ`，不会扩面到整个 FU 框架。

### 9.2 更好的“中改版”

如果愿意在阶段 6 稍微多做一点，可以把 `SS` 的 crypto pipeline 再做得更像真实硬件：

1. store-side crypto 模块显式建成 3 级流水。
2. 每周期最多接受 1 条新的 `SS`。
3. 为每条 `SS` 计算：
   1. `crypto_start_cycle = max(curCycle, last_crypto_accept_cycle + 1)`。
   2. `crypto_ready_cycle = crypto_start_cycle + 3`。
4. 这样不仅有固定 3 周期延迟，还能表达连续多条 `SS` 的吞吐限制。

这个方案的优点是：

1. 更贴近你当前“3 级流水、不存在 load/store race”的硬件设定。
2. 能开始建模多条 `SS` 的入口竞争。
3. 后续扩展到 `SS_ID` 也更自然。

缺点是实现量明显高于本阶段的最小 patch。

## 10. 本阶段推荐的实施顺序

如果阶段 6 只做设计，不急着一次写满所有代码，我建议按下面顺序落地：

1. 先实现 issue 侧 `LS` 额外 3 周期 ready time。
2. 再实现 `LS` response 返回后的额外 3 周期 completion hold。
3. 对 `SS` 实现 `crypto_ready_cycle`，并把它与 translation/store ordering 并行组合。
4. 如果后续真的引入“修改 `use` 但不切 stream”的场景，再补 `future use` shadow。
5. 如果本阶段还能再做一步，再把 store-side crypto 入口做成“每周期最多接受 1 条”的 3 级流水近似。
6. 最后补 baremetal timing 测试，验证：
   1. `LS` consumer 链比 `LD` 多 3 周期。
   2. `LS` 的 completion 也比普通 `LD` 晚 3 周期。
   3. `SS` 的真正发射时间受 `crypto_ready_cycle` 约束，而不是简单串行等待。
   4. `use=0` 时退化回 `LD/SD` 路径。
   5. S/M 态固定走扩展 timing。

## 11. 阶段 6 的验收标准

本阶段建议使用下面这组验收标准：

1. `LS` 在 U 态 `use=1` 时，其 `RD` 的 timing-ready 比普通 `LD` 晚 3 周期。
2. `LS` 在 U 态 `use=0` 时，其 `RD` 的 timing-ready 与普通 `LD` 相同。
3. `LS` 在 S/M 态时，其 `RD` 的 timing-ready 固定比普通 `LD` 晚 3 周期。
4. `LS` 在扩展路径下，response 返回后还要再等待 3 周期才允许真正完成提交。
5. `SS` 在 U 态 `use=1` 与 S/M 态时，必须满足 `curCycle >= crypto_ready_cycle` 才允许发往 memory system。
6. `SS` 的 `crypto_ready_cycle` 可以与 translation 并行推进，最终发射时间由多个 ready 条件的最大值决定。
7. `SS` 在 U 态 `use=0` 时，与普通 `SD` 同时序。
8. 普通 `LD/SD` 的既有 timing 不被污染。
9. 对 `switchs/jalr/ret` 序列，真正存活路径上的 `LS/SS` 能看到最新提交后的 `use`；旧 stream 上的 younger 指令即使先按旧 `use` 投机推进，也会被 discard。

## 12. 本文档的最终建议

如果只问一句“阶段 6 应该按哪版方案做”，我的建议是：

1. `LS` 采用“issue 侧 `scoreboard +3`，response/commit 侧再 `+3`”的双时点建模。
2. `SS` 采用 `crypto_ready_cycle` 方案，而不是“发射前固定串行再等 3 周期”。
3. 对当前仅由 `switchs/jalr/ret` 修改 `use` 的场景，可以先不真正实现 `future use`，仍能保证功能正确。
4. 若实现预算允许，再把 store-side crypto 入口补成“每周期最多接收 1 条”的 3 级流水近似。

这版方案的好处是：

1. 与 `MinorCPU` 当前的 mem timing 风格相容。
2. 对 `LS` 的依赖可见时间与完成时间都更一致。
3. 对 `SS` 能自然表达“加密与翻译并行”。
4. 后续扩展到 `LS_MAP/SS_ID/SWITCHS` 时，不需要推倒重来。

## 13. 阶段 6 开发总结

说明：第 1 至第 12 节记录的是阶段 6 设计讨论和方案收敛过程，其中部分内容保留了早期关于 `future use` 和按 `use/priv` 决定 `LS` issue 延迟的分析。第 13 节给出的是最终实际落地到代码中的实现总结；若两者存在差异，应以第 13 节描述的代码现状为准。

### 13.1 本阶段实际落地的模块

阶段 6 最终实际改动落在以下模块：

1. `src/cpu/minor/execute.cc`
2. `src/cpu/minor/lsq.hh`
3. `src/cpu/minor/lsq.cc`
4. `benchmark/simple-sigriscv-test/gem5_test/tests/` 下的 baremetal 测试程序

其中：

1. `execute.cc` 负责 issue 侧的 `LS` 预测延迟建模。
2. `lsq.hh/lsq.cc` 负责 `LS/SS` 请求进入 LSQ 后的额外 timing 状态。
3. 测试程序用于覆盖功能正确性、简单时序现象以及更真实的数据流场景。

### 13.2 最终落地版本与原始设计讨论的差异

阶段 6 在讨论过程中，设计曾考虑过按 `use/priv` 动态决定 `LS` 的 issue 侧额外延迟，并保留 `future use` 作为 timing 判定输入。最终代码没有按这一路径完全落地，而是收敛成更稳妥的一版：

1. `LS` 在 issue 侧默认固定比普通 `LD` 多预测 3 周期。
2. 这一版不再依赖“基于旧 `use` 猜测是否需要 +3”的判定。
3. `future use` 没有在阶段 6 中真正实现为新的 shadow state。
4. 对 `switchs/jalr/ret` 这类“commit 后立刻切 stream”的 `use` 更新场景，功能正确性仍由 `commit + stream change + discard old stream` 保证。

这样收敛的原因是：

1. 当前项目里 `LS` 基本可以视为稳定需要额外 3 周期的路径。
2. 用旧 `use` 去猜测反而更容易把 issue 侧 ready time 预测错。
3. 阶段 6 的重点是先把 `MinorCPU` 的 `LS/SS` timing 路径打通，而不是一次把所有控制态旁路都补齐。

### 13.3 本阶段最终实现的时序语义

#### `LS`

`LS` 最终采用双时点建模：

1. issue 侧：在 `scoreboard` 视角下默认比普通 `LD` 额外多 3 周期。
2. response/commit 侧：load 响应返回后再额外等待 3 周期，才允许真正把结果交回提交路径。

因此，阶段 6 实际代码表达的是：

1. `LS` 的 dependent consumer 比普通 `LD` 更晚被唤醒。
2. `LS` 自身的完成时间也比普通 `LD` 更晚。
3. 这两个 `+3` 分别对应“结果何时可被消费”和“解密何时真正完成”，不是同一观测量上的重复收费。

#### `SS`

`SS` 最终采用 `crypto_ready_cycle` 建模：

1. `SS` 请求进入 LSQ 时记录额外的 crypto ready 时间。
2. store 真正发往 memory system 之前，除了满足原有的 translation、ordering、port/store buffer 条件，还必须满足 `curCycle >= crypto_ready_cycle`。
3. 因此 `SS` 的额外 3 周期不是被简单串行挂在发射点前，而是允许与 translation 和排队阶段并行。

这个实现与阶段 6 的最终设计目标一致，也比“发射前固定硬等 3 周期”的模型更接近预期硬件。

### 13.4 开发过程中遇到的关键问题

本阶段开发中最关键的一个问题，出现在 `LS` 的 response 侧延迟实现上。

问题现象是：

1. 当程序的第一个 LSQ 请求就是 `LS` 时，内存响应虽然已经返回，
2. 但由于 response 还要再等待额外 3 周期，`findResponse()` 会暂时不把它交回 `Execute`，
3. 如果此时流水线里没有别的活动，`MinorCPU` 可能直接 idle，导致这条已经“完成但未到期”的 `LS` 永远没人再次检查，看起来像卡死。

最后采用的修复方法是：

1. 当 head load 已经完成、但 `responseReadyCycle` 还没到时，
2. 显式调用 activity 记录逻辑，让 `Execute` 继续被唤醒，
3. 直到这条 `LS` 的额外 3 周期等待到期后，再正常把响应交回提交路径。

这个修复说明，阶段 6 不仅补上了 `LS/SS` 的额外 timing，还顺带补齐了 `MinorCPU` 在“延后可见但已完成 response”场景下的推进细节。

### 13.5 开发与验证过程总结

本阶段开发过程大致分为四步：

1. 先阅读 `execute.cc`、`lsq.hh`、`lsq.cc`，确认 `MinorCPU` 中 issue、commit、memory issue、load response 的真实边界。
2. 然后把 `LS` 的额外延迟拆成 issue 侧和 response 侧两个时点实现。
3. 再把 `SS` 的额外延迟收敛成 `crypto_ready_cycle`，并与 translation/queue/port 条件并行组合。
4. 最后用 baremetal 测试反复做功能回归和简单 timing 现象检查，并修复首条 `LS` 卡住的问题。

配套测试主要覆盖了三类场景：

1. 基础 `LS/SS` 功能序列，验证 S/U 模式与 `use=0/use=1` 下的语义。
2. `dummy.S` 一类的小程序，用于复现和确认“首条 `LS` 卡住”问题已经修复。
3. `stage5_sort.S` 指针冒泡排序测试，用真实数据流反复触发 `ls/ss/lw` 组合，验证更长链路下的行为。

### 13.6 本阶段得到的结果

阶段 6 开发完成后，可以得到下面这些结果：

1. `MinorCPU` 已经能够对 `LS/SS` 提供第一版可运行的 timing 模型，而不再只是阶段 5 的纯功能语义。
2. `LS` 已具有比普通 `LD` 更晚的依赖可见时间和更晚的 completion 可见时间。
3. `SS` 已具有与 translation 并行的额外加密等待，而不是完全串行的人工停顿。
4. 对当前由 `switchs/jalr/ret` 主导的 `use` 更新路径，即使没有真正实现 `future use`，功能正确性仍然可保。
5. 首条 `LS` 作为 LSQ 头请求时的挂起问题已经定位并修复。

从工程角度看，这意味着阶段 6 的目标已经达成：

1. `LS/SS` 的 timing 模型已经接入 `MinorCPU` 主路径。
2. 设计讨论中最关键的几个边界问题，都已经落成代码并经过 baremetal 验证。
3. 后续如果要继续推进阶段 7，可以在当前基础上继续细化 `future use`、crypto pipeline 吞吐限制或更多 `LS_MAP/SS_ID` 相关语义，而不需要推翻阶段 6 的实现。

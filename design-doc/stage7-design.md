# 阶段 7 设计计划：MinorCPU 的 `LS.MAP/SS.ID` 时序化

## 1. 阶段 7 的目标

阶段 7 的目标是在当前 stage6 `LS/SS` timing 基线之上，把 `LS.MAP/SS.ID` 接入 `MinorCPU`，并保持下面三条原则：

1. 正确路径的功能语义必须正确。
2. 修改范围尽量收敛在 `arch/riscv/isa/decoder.isa`、`cpu/minor/execute.*`、`cpu/minor/lsq.*`。
3. timing 采用“对当前软件使用模式足够可信”的近似，而不为罕见交错序列重做一套完整 future `ENCMAP` 体系。

本阶段采用的额外前提来自当前软件约束，而不是任意指令交错：

1. 函数入口按组执行 `ss.id s0/s1/...` 与 `ss.encmap`。
2. 函数退出按组执行 `ls.encmap` 与 `ls.map s0/s1/...`。
3. 不要求支持任意交错的 `ss.id/ls.map/ss.encmap/ls.encmap` 序列。

在这个约束下，阶段 7 不追求“任意未提交 `ENCMAP` 更新都能被 younger 指令精确观察”，而追求：

1. `ss.id` 和 `ss.encmap` 最终对内存与架构 `ENCMAP` 的效果正确。
2. `ls.encmap` 能在恢复序列中向后续 `ls.map` 提供正确的位图来源。
3. `ls.map` 的额外解密延迟只在真正需要解密时出现。

### 2.1 ss.id rs2, imm(rs1)

扩展语义生效时：

1. 若 rs2 == x0，执行 ss.encmap，把当前 ENCMAP 写入内存。
2. 若 rs2 != x0 且 GPRID[rs2] == 0，退化为原始 SD，并把 ENCMAP[rs2] 清零。
3. 若 rs2 != x0 且 GPRID[rs2] != 0，执行 SS 加密 store，并把 ENCMAP[rs2] 置 1。

### 2.2 ls.map rd, imm(rs1)

扩展语义生效时：

1. 若 rd == x0，执行 ls.encmap，把内存中的原始 64 位值写入 ENCMAP。
2. 若 rd != x0 且 ENCMAP[rd] == 0，退化为原始 LD，并在 U 态清除 GPRID[rd]。
3. 若 rd != x0 且 ENCMAP[rd] == 1，执行 LS 解密 load。

### 2.3 扩展语义生效条件

沿用阶段 5/6：

1. U 态仅在 IDCSR.use = 1 时生效。
2. M/S 态始终生效。
3. 不生效时，LS.MAP/SS.ID 完全退化为 LD/SD，不得额外读写 ENCMAP/GPRID

## 2. 当前代码基线

### 2.1 ISA 侧现状

当前 `decoder.isa` 已经具备 `ls/ls.map/ss/ss.id` 的功能语义，但 `ls.map/ss.id` 仍然在 `.isa` 执行代码里直接修改架构 `ENCMAP`：

1. `ls.map` 在 load 模板中直接 `writeEncmap(...)` 或读取 `readEncmapBit(...)`。
2. `ss.id` 在 store 模板中直接 `updateEncmapBit(...)`，或把 `readEncmap()` 作为 `ss.encmap` 的 store payload。

这条路径对 `SimpleCPU` 功能模型是自然的，但对 `MinorCPU` 不合适，因为这些修改发生得太早，会绕过 `Minor` 的真实 mem response/commit 可见性边界。

因此阶段 7 的设计要求不是“把 `.isa` 的 `ENCMAP` 语义整体删掉”，而是：

1. 对功能 CPU，`ENCMAP` 语义仍必须完整。
2. 对 `MinorCPU` timing 路径，把过早的 `ENCMAP` 副作用改为由 `cpu/minor/*` 在更晚的时点补齐。
3. 最终目标是：功能 CPU 和 `MinorCPU` 都能正确执行，只是 `MinorCPU` 的 `ENCMAP` 生效位置更贴近其 timing 模型。

源码依据：

1. `repo/gem5/src/arch/riscv/isa/decoder.isa`
2. 其中 `ls.map` 位于 `Load` format 的 `0x4` 分支。
3. 其中 `ss.id` 位于 `Store` format 的 `0x6` 分支。

### 2.2 Minor 侧现状

当前 `MinorCPU` 对 mem 指令的关键边界已经在 stage6 中固定：

1. 指令在 `Execute::commitInst()` 末端调用 `initiateAcc()`，把请求送入 LSQ。
2. load/store 的真正 mem response 处理发生在 `Execute::handleMemResponse()`，并由这里调用 `staticInst->completeAcc(...)`。
3. load 是否已经可以交给 `Execute`，由 `LSQ::findResponse()` 判定。
4. bufferable store 不会在 `completeAcc` 后立刻结束，而是还要经过 `LSQ::sendStoreToStoreBuffer()` 和后续 store buffer 发射。

源码依据：

1. `repo/gem5/src/cpu/minor/execute.cc`
2. `repo/gem5/src/cpu/minor/lsq.cc`
3. `Execute::handleMemResponse()` 调用 `completeAcc()`。
4. `LSQ::findResponse()` 控制 response 何时对 `Execute` 可见。
5. `LSQ::sendStoreToStoreBuffer()` 说明了 store 在 `completeAcc` 后仍可能继续留在 store buffer 中。

### 2.3 stage6 已有 timing 补丁

stage6 已经为 `LS/SS` 建好了最重要的 timing 接口：

1. `LS` 在 issue 侧通过 `scoreboard` 额外增加 3 周期。
2. `LS` 在 response 侧通过 `responseReadyCycle` 再额外等待 3 周期。
3. `SS` 在 store-side 通过 `cryptoReadyCycle` 控制最早发射时间。

源码依据：

1. `repo/gem5/src/cpu/minor/execute.cc` 中 `isSigriscvLsIssuePath()` 和 issue 阶段的 `extra_assumed_lat += SigriscvCryptoDelay`。
2. `repo/gem5/src/cpu/minor/lsq.hh` 中 `responseReadyCycle/cryptoReadyCycle` 字段。
3. `repo/gem5/src/cpu/minor/lsq.cc` 中 `LSQ::findResponse()` 对 `responseReadyCycle` 的检查。

## 3. 阶段 7 的核心判断

### 3.1 metadata 只保留结构信息，不保留容易过时的状态值

阶段 7 的核心设计原则是：

1. 不新增 `pending_gprid/pending_use/pending_priv/pending_encmap_bit/pending_encmap_value` 这类容易过时的状态缓存。
2. 也不新增额外的寄存器编号副本。
3. 只在 LSQ request 上保留最少的结构信息，用来区分 request 类型和 timing 路径。

阶段 7 的结论是：

1. 只在 LSQ request 上保留最少的结构信息，例如：
   1. 这条 request 是否属于 `ls.map`
   2. 这条 request 是否属于 `ls.encmap`
   3. 这条 request 是否属于 `ss.id`
   4. 这条 request 是否属于 `ss.encmap`

理由有两个：

1. 指令类别在 response/store-completion 阶段仍然需要快速判定。
2. 当前 `StaticInst` 本身已经保留了足够的结构信息，后续仍可通过 `srcRegIdx()/destRegIdx()` 等接口获得寄存器编号。
3. 相比之下，缓存 `GPRID/USE/PRIV/ENCMAP` 快照更容易和最终提交顺序脱节。

### 3.3 `ENCMAP` 分成两类可见性

阶段 7 需要区分两种 `ENCMAP`：

1. 架构 `ENCMAP`
   1. 只保存已经对软件可见、不会再被回滚的状态。
   2. `ss.id` 对架构 `ENCMAP` 的真正更新应发生在 store 对外不可撤销之后。
2. 恢复序列专用的 shadow `ENCMAP`
   1. 只服务于 `ls.encmap -> ls.map*` 这一类函数退出恢复序列。
   2. 它不需要泛化成完整 future CSR 体系。
   3. 它只需要保存“最近一次 memory return 的 `ls.encmap` 载入值”。

在当前软件约束下，`ls.map` 是否需要进入 decrypt pipeline 的判定，优先读取这份 shadow `ENCMAP` 就足够满足需要。

## 4. 阶段 7 的最终设计

### 4.1 步骤 1：保持 `.isa` 功能兼容，由 `MinorCPU` 撤回过早的 `ENCMAP` 副作用

本步骤的设计是：

1. 保留 `.isa` 中对 `ls.map/ss.id/ls.encmap/ss.encmap` 的数据语义分流。
2. 对功能 CPU，`ENCMAP` 的功能语义继续完整保留。
3. 对 `MinorCPU`，不直接删除 `.isa` 逻辑，而是在 `ss.id` 的 `initiateAcc()` 之后把这类过早的架构 `ENCMAP` 修改撤回，再由 `cpu/minor/*` 在后续阶段补齐。

具体含义：

1. `ls.map`
   1. 保留 `.isa` 中最终的功能语义。
   2. 对 `MinorCPU`，`ls.encmap` 的 shadow 更新和 `ls.map` 的 decrypt pipeline 判定由 response 路径补齐。
2. `ss.id`
   1. 保留 `.isa` 中原有功能语义，保证功能 CPU 兼容。
   2. 对 `MinorCPU`，在 `initiateAcc()` 之后立即恢复旧 `ENCMAP`，避免过早架构写回污染 timing。
   3. `ss.id` 的数据密文继续复用 `initiateAcc()` 已生成的结果，不在单路径点重复加密。
   4. `ss.encmap` 的最终 payload 不由过早的 `readEncmap()` 固定，而由后续单路径点重写。

源码设计依据：

1. 当前过早副作用出现在 `repo/gem5/src/arch/riscv/isa/decoder.isa`。
2. 这些副作用在 `SimpleCPU` 功能模型里成立，但不符合 `Minor` 的 mem response 时序边界。
3. 阶段 7 的要求是功能 CPU 与 `MinorCPU` 均正确运行，因此更稳的实现是保留 `.isa` 语义，再由 `MinorCPU` 自己撤回并重提早期副作用。

### 4.2 步骤 2：在 `findResponse` 放行前完成 `ls.encmap/ls.map` 的位图更新与 decrypt 判定

本步骤的设计是：

1. `ls.encmap` 的内存返回值一旦到达，就在 response-ready 之前先更新恢复序列专用 shadow `ENCMAP`。
2. `ls.map` 是否进入 decrypt pipeline 的判定，也在 response-ready 之前完成，而不是等到 `completeAcc()`。
3. `completeAcc()` 本身只负责沿用 `.isa` 最终完成 load 语义，把最终数据写入 `rd`/`GPRID[rd]`。

具体规则：

1. `ls.encmap`
   1. 在 memory return 到达后、`findResponse()` 放行前，读取返回的 64 位值。
   2. 把它写入恢复序列专用 shadow `ENCMAP`。
   3. 若需要同步真实架构 `ENCMAP`，也应由 `Minor` response 路径补齐，而不是依赖早期 `.isa` 副作用。
2. `ls.map`
   1. 在 memory return 到达后、`findResponse()` 放行前，读取最新 shadow `ENCMAP`。
   2. 决定自己是 raw `LD` 路径还是 `LS` 解密路径。
   3. 若需要解密，则在这里把 request 接入 decrypt pipeline，并计算最终 `responseReadyCycle`。
3. `ss.id`
   1. 在 `initiateAcc()` 时形成数据密文。
   2. 在 `completeAcc()` 时读取最新 `GPRID/USE/PRIV`。
   3. 只决定本次 store 最终对应的 `ENCMAP` 副作用，不重复生成密文 payload。
4. `ss.encmap`
   1. 在 `completeAcc()` 时读取最新 `ENCMAP`。
   2. 以这个值作为最终 store payload 的来源，而不是沿用 `initiateAcc` 时的旧值。

源码设计依据：

1. `LSQ::findResponse()` 是当前 Minor load response 的最后一道可见性门控点。
2. `Execute::handleMemResponse()` 只有在 `findResponse()` 已经放行后才会调用 `completeAcc()`。
3. 因而凡是“是否进入 decrypt pipeline”“何时 response-ready”的判断，都应在 `findResponse()` 放行前完成，而不是等到 `completeAcc()`。

### 4.3 步骤 3：LSQ request 只增加最小结构 metadata

本步骤的设计是：

1. metadata 挂在 `LSQ::LSQRequest` 上，而不是挂在 `MinorDynInst` 上。
2. metadata 只描述“这是什么 request”，不描述“这条 request 当时读到了什么状态值”。

建议保留的字段类型：

1. `isLsMap`
2. `isLsEncmap`
3. `isSsId`
4. `isSsEncmap`

建议不保留的字段类型：

1. `pendingEncmapValue`
2. `pendingEncmapBit`
3. `pendingGprId`
4. `pendingUse`
5. `pendingPriv`
6. `pendingRd/pendingRs2`

源码设计依据：

1. `LSQRequest` 天然贯穿 translation、request、transfer、response、store buffer 这些阶段，适合作为 mem timing 元数据载体。
2. `MinorDynInst` 和 `StaticInst` 本身已经保留了足够的结构信息，后续仍可通过 `srcRegIdx()/destRegIdx()` 等接口获得寄存器编号。
3. 当前 `decoder.isa` 已大量依赖 `srcRegIdx()/destRegIdx()`，不需要为 stage7 再复制一套编号。

### 4.4 步骤 4：`LS` 和 `LS.MAP` 的 3 周期延迟分开处理

阶段 7 对 `LS` 和 `LS.MAP` 的 timing 不应一刀切。

#### 4.4.1 `LS`

继续沿用 stage6 原则：

1. issue 侧仍然通过 `scoreboard` 额外增加 3 周期。
2. response 侧仍然需要等待解密 ready。

源码设计依据：

1. 当前 `execute.cc` 已经在 issue 阶段对名字为 `"ls"` 的 load 增加 `SigriscvCryptoDelay`。
2. 当前 `lsq.cc` 已经通过 `responseReadyCycle` 控制 `LS` response 的最早可见时间。

#### 4.4.2 `LS.MAP`

不在 issue 阶段固定增加 3 周期。

原因：

1. `ls.map` 是否需要解密，要等 mem 返回后看最新 `ENCMAP`。
2. 在 issue 阶段提前统一 `+3` 会把大量退化为 `LD` 的 `ls.map` 也误建模成解密 load。

因此阶段 7 对 `LS.MAP` 的 timing 规则是：

1. issue 侧按普通 `LD` 处理。
2. memory return 到达后，在 `findResponse()` 放行前读取最新 shadow `ENCMAP`。
3. 只有当它最终判定自己走 `LS` 解密路径时，才进入额外的 3 周期 decrypt pipeline 等待。
4. 若判定为 raw `LD` 路径，则不额外增加 decrypt 等待，随后直接由 `completeAcc()` 完成 `.isa` 语义。

### 4.5 步骤 5：response 侧的 3 周期等待按“crypto pipeline”建模

若阶段 7 希望更接近“3 级流水，而不是每条各等 3 周期”的模型，则不应简单把每条 `LS/LS.MAP` 都设为：

1. `responseReadyCycle = response_arrival + 3`

更合适的设计是增加一个全局 decrypt pipeline 可用时间，例如：

1. `decryptPipeNextIssueCycle`

规则如下：

1. 当一条需要解密的 `LS` 或 `LS.MAP` response 到达时：
   1. `start_cycle = max(curCycle, decryptPipeNextIssueCycle)`
   2. `done_cycle = start_cycle + 3`
   3. `decryptPipeNextIssueCycle = start_cycle + 1`
2. `LSQ::findResponse()` 继续用 `responseReadyCycle` 控制“这条 load 何时对 `Execute` 可见”。
3. `ls.encmap` 与 `ls.map` 的 shadow 更新、decrypt 判定和 `responseReadyCycle` 计算，都在 `findResponse()` 放行前完成。
4. `completeAcc()` 不再承担“是否进入 decrypt pipeline”的判定职责，而只负责在 response-ready 之后执行 `.isa` 最终语义。

这样建模的好处是：

1. 第一条需要灌满 3 周期。
2. 后续连续到达的解密 load 不会退化成 `3N` 周期串行等待。
3. 仍然能复用现有 `responseReadyCycle` 与 `LSQ::findResponse()` 的结构。

源码设计依据：

1. `LSQ::findResponse()` 已经天然提供了 response 可见性门控点。
2. `Execute::handleMemResponse()` 只会处理已经被 `findResponse()` 允许放行的请求。
3. 因此把 decrypt pipeline 的等待折叠到 `responseReadyCycle`，并在 `findResponse()` 放行前完成位图更新与路径判定，最符合当前 `Minor` 结构。

### 4.6 步骤 6：`ss.id` 采用单一路径点提交

这是阶段 7 最重要的 store-side判断。

结论是：

1. `ss.id` 和 `ss.encmap` 的最终语义判断可以放在 `completeAcc()`。
2. 对当前目标软件序列，只围绕普通 bufferable store 主路径设计，不把稀有 store 类型当成主约束。
3. `ss.id` 的 `ENCMAP` 架构更新采用单一路径点提交。

原因：

1. 你的 `ss.id/ss.encmap` 栈保存序列目标地址是普通栈内存，主路径就是普通 bufferable store。
2. 对这条主路径，`completeAcc()` 之后 request 会进入 `StoreToStoreBuffer`，再由 `sendStoreToStoreBuffer()` 进入 store buffer。
3. `non-bufferable store`、`local access`、直接 request/transfer 的 store 在当前目标序列下不是主场景，不作为阶段 7 主要设计约束。

阶段 7 的实现原则应是：

1. `completeAcc()` 负责根据最新架构态重新计算本条 store 的最终 `ENCMAP` 副作用。
2. 对当前主路径，在约定的单一路径点提交这个副作用到架构 `ENCMAP`。
3. `ss.id` 的数据密文继续沿用 `initiateAcc()` 结果，不在单路径点重复加密；只有 `ss.encmap` 需要在单路径点重读最新 `ENCMAP` 并覆盖 payload。

源码设计依据：

1. `LSQ::tryToSendToTransfers()` 中普通 bufferable store 会被置为 `StoreToStoreBuffer`。
2. `Execute::handleMemResponse()` 在 store 的 `completeAcc()` 后会调用 `lsq.sendStoreToStoreBuffer(response)`。
3. 这条路径正是当前 `ss.id/ss.encmap` 序列的主路径，因此阶段 7 直接围绕它设计即可。

## 5. 为什么不需要额外保存寄存器编号

阶段 7 不新增寄存器编号 metadata，理由如下：

1. 当前 `StaticInst` 本身已经保存了操作数结构。
2. `decoder.isa` 现有代码已经广泛使用 `srcRegIdx(1).index()`、`destRegIdx(0).index()` 获取编号。
3. 到 `completeAcc()` 阶段，`inst->staticInst` 仍然存在，因此后续仍可通过同样接口取出 `rd/rs2`。

因此：

1. 不需要在 `MinorDynInst` 增加额外 `rd/rs2` 字段。
2. 也不需要在 `LSQRequest` 再复制一份编号。

## 6. 为什么这版设计符合当前项目主线

这版 stage7 设计与前几阶段保持一致：

1. 和 stage4 一致：
   1. 能避免缓存容易过时的状态值，就不要提前建立快照副本。
2. 和 stage6 一致：
   1. timing 尽量复用现有 `issue/FU/LSQ/findResponse/handleMemResponse` 结构。
   2. 不新造完整 crypto companion pipeline。
3. 与当前用户约束一致：
   1. 不为任意稀有交错序列支付过高设计复杂度。
   2. 优先保证函数入口保存序列与函数退出恢复序列的正确性。

## 7. 实施顺序建议

建议按下面顺序推进 stage7：

1. 先修改 `decoder.isa` 设计，明确收回 `.isa` 中对架构 `ENCMAP` 的立即副作用。
2. 再在 `LSQRequest` 上加入最小结构 metadata，只标记 request 类型。
3. 然后在 LSQ response 路径、`findResponse()` 放行前完成：
   1. `ls.encmap` 导入 shadow/arch `ENCMAP`
   2. `ls.map` 读取最新 shadow `ENCMAP` 做路径判定
   3. 需要解密的 request 接入 decrypt pipeline 并计算 `responseReadyCycle`
4. 最后在 `completeAcc` 路径完成：
   1. `ls/ls.map` 按 `.isa` 最终语义写回 `rd/GPRID`
   2. `ss.id/ss.encmap` 读取最新架构态形成最终副作用
   3. `ss.id` 在单一路径点提交架构 `ENCMAP`

## 8. 当前结论

阶段 7 的最终结论是：

1. `LS.MAP/SS.ID` 不保存容易过时的状态快照；`ls.encmap/ls.map` 的位图更新与 decrypt 判定在 `findResponse()` 放行前完成，`ss.id` 的最终副作用判断在 `completeAcc()` 完成。
2. metadata 应尽量少，只保留 request 类型等结构信息，不保存容易过时的 CSR/GPRID/ENCMAP 快照，也不重复保存寄存器编号。
3. `LS` 与 `LS.MAP` 的 3 周期 timing 不应统一处理：
   1. `LS` 保持 stage6 的 issue+response 双时点建模。
   2. `LS.MAP` 只在 memory return 后、且确定真的需要解密时才进入 decrypt pipeline 等待。
4. 对当前 `ss.id/ss.encmap` 目标序列，只围绕普通 bufferable store 主路径设计，架构 `ENCMAP` 更新采用单一路径点提交。
5. 在当前固定保存/恢复序列约束下，只保留一份恢复序列专用 shadow `ENCMAP` 就足够，不需要构建完整 future `ENCMAP` 体系。

## 9. 当前代码落地情况

到目前为止，stage7 已经在 `MinorCPU` 主路径上完成了第一版实现，重点改动集中在 `execute.cc`、`lsq.hh`、`lsq.cc`。

### 9.1 `execute.cc`

当前实现已经补上了两类 `store-side` 行为：

1. 对 `MinorCPU` 的 `ss.id`，在 `initiateAcc()` 之后撤回 `.isa` 过早写入的架构 `ENCMAP`，避免 timing 路径被提前污染。
2. 在 `handleMemResponse()` 的 store 路径上，为 `ss.id/ss.encmap` 重新补齐后续副作用：
   1. `ss.id` 不再重复做密文重加密，而是直接复用 `initiateAcc()` 生成的 store payload。
   2. `ss.id` 在单路径点基于最新 `GPRID[rs2]` 更新架构 `ENCMAP` 对应 bit。
   3. `ss.encmap` 在单路径点重新读取最新 `ENCMAP`，并覆盖最终 store payload。

这一版实现对应的源码位置是：

1. `repo/gem5/src/cpu/minor/execute.cc`
2. `Execute::executeMemRefInst()`
3. `Execute::handleMemResponse()`

### 9.2 `lsq.hh`

当前实现已经在 `LSQRequest` 上加入最小结构 metadata，并补上了 stage7 所需的 LSQ 级状态：

1. request 级类型标记：
   1. `sigriscvLsMapPath`
   2. `sigriscvLsEncmapPath`
   3. `sigriscvSsIdPath`
   4. `sigriscvSsEncmapPath`
   5. `sigriscvResponsePrepared`
2. LSQ 级状态：
   1. `sigriscvShadowEncmap`
   2. `sigriscvShadowEncmapValid`
   3. `sigriscvDecryptPipeNextIssueCycle`

这一版实现对应的源码位置是：

1. `repo/gem5/src/cpu/minor/lsq.hh`
2. `LSQRequest` 定义
3. `LSQ` 成员定义

### 9.3 `lsq.cc`

当前实现已经把 `ls.encmap/ls.map/ls` 的 response-side 处理统一收口到 LSQ response 准备阶段：

1. 新增 `prepareSigriscvLoadResponse()`。
2. 在 `findResponse()` 放行前，如果 request 已经 complete 且尚未做过 response prepare，则统一进入这个函数。
3. 在这个统一点中完成：
   1. `ls.encmap` 读取返回的 64 位值，并更新 shadow `ENCMAP`。
   2. `ls.encmap` 同时补写真实架构 `ENCMAP`。
   3. `ls.map` 根据最新 shadow/arch `ENCMAP` 判断自己是否需要进入 decrypt pipeline。
   4. `ls` 无条件进入 decrypt pipeline。
   5. `ls/ls.map` 共用 `sigriscvDecryptPipeNextIssueCycle` 计算 `responseReadyCycle`。
4. stage6 中原本散落在多条 load 完成路径上的 response-side `+SigriscvCryptoDelay` 已经删除，统一改为在 `prepareSigriscvLoadResponse()` 中计算。

这一版实现对应的源码位置是：

1. `repo/gem5/src/cpu/minor/lsq.cc`
2. `LSQ::pushRequest()`
3. `LSQ::findResponse()`
4. `LSQ::prepareSigriscvLoadResponse()`

### 9.4 当前实现与设计的对应关系

目前代码实现与本文前述设计的对应关系如下：

1. `.isa` 语义没有被整体删掉，仍用于功能 CPU；`MinorCPU` 自己在 timing 路径上撤回并补齐早期副作用。
2. `ls.encmap/ls.map` 的位图更新与 decrypt 判定，已经前移到 `findResponse()` 放行前，而不是留到 `completeAcc()`。
3. `ls` 的 issue-side `scoreboard + SigriscvCryptoDelay` 仍然保留 stage6 方式。
4. `ls` 的 response-side `+SigriscvCryptoDelay` 已经和 `ls.map` 统一到同一个 prepare 路径。
5. `ss.id` 目前采用“密文复用 early payload、只晚提交 `ENCMAP` 副作用”的方式。

## 10. 当前测试情况

### 10.1 基础测试

基础功能测试文件：

1. `benchmark/simple-sigriscv-test/gem5_test/tests/stage7_ls_map_ss_id.S`

当前基础测试覆盖了下面几类场景：

1. U 态 `use=1` 下 `ss.id + ls.map` 的加密路径。
2. U 态 `use=1` 下 `GPRID=0` 时 `ss.id + ls.map` 的 raw 路径。
3. `ss.encmap` 把当前 `ENCMAP` 写入内存。
4. `ls.encmap` 从内存恢复 `ENCMAP`。
5. U 态 `use=0` 时 `ls.map/ss.id` 完全退化为 `ld/sd`。

### 10.2 复杂测试

复杂时序/结构测试文件：

1. `benchmark/simple-sigriscv-test/gem5_test/tests/stage7_ls_map_ss_id_complex.S`

当前复杂测试覆盖了三类更接近真实保存/恢复序列的场景：

1. `gap` case
   1. 先执行一整组 `ss.id s0..s6 + ss.encmap`。
   2. 中间插入大量普通整数指令。
   3. 再执行 `ls.encmap + ls.map s0..s6`。
   4. 用来验证保存/恢复序列在较长间隔下仍然正确。
2. `forward` case
   1. 先执行一整组 `ss.id s0..s6 + ss.encmap`。
   2. 中间不插任何额外存储相关操作。
   3. 直接执行 `ls.encmap + ls.map s0..s6`。
   4. 用来验证紧邻恢复路径以及内部前递相关场景。
3. `nested` case
   1. 外层 `nested_run` 先执行 `ss.id s0..s6 + ss.encmap`。
   2. 中间调用 `nested_func`。
   3. `nested_func` 内部再执行一套 `ss.id s0..s6 + ss.encmap`，插入普通整数指令后再执行 `ls.encmap + ls.map s0..s6`。
   4. 外层返回后再执行自己的 `ls.encmap + ls.map s0..s6`。
   5. 用来验证嵌套调用时内外两层保存/恢复结构是否互相兼容。

### 10.3 复杂测试的数据分布

复杂测试中的保存/恢复对象已经扩展到 `s0..s6`，并且混合了 `GPRID=0` 与 `GPRID!=0` 两类寄存器：

1. `s0/s2/s4/s6` 对应 `GPRID != 0`，用于覆盖加密 store / 解密 load 路径。
2. `s1/s3/s5` 对应 `GPRID == 0`，用于覆盖 raw store / raw load 路径。
3. 对 `GPRID != 0` 的寄存器，初始化值限制在 40 bit 范围内。
4. 对 `GPRID == 0` 的寄存器，初始化值允许为 64 bit。

当前测试检查项包括：

1. `s0..s6` 恢复后的寄存器值。
2. `encmap` 的最终 bit 分布。
3. `gprid8/gprid9/gprid18/gprid19/gprid20/gprid21/gprid22` 的最终值。

### 10.4 当前验证状态

当前已经完成的验证包括：

1. `stage7_ls_map_ss_id.S` 与 `stage7_ls_map_ss_id_complex.S` 的汇编与链接检查。
2. `make test-elf TEST=stage7_ls_map_ss_id`
3. `make test-elf TEST=stage7_ls_map_ss_id_complex`

两份测试目前都能成功生成：

1. `.o`
2. `.elf`
3. `.dump`
4. `.bin`

当前文档记录时，复杂测试已经覆盖：

1. 长间隔保存/恢复
2. 紧邻保存/恢复
3. 嵌套调用保存/恢复
4. 混合 `GPRID=0` / `GPRID!=0`
5. `s0..s6` 多寄存器组保存/恢复

尚未在本文档中记录完整的 `gem5 run-minor` 通过结果，因此当前测试状态应视为：

1. 汇编/链接通过
2. 结构性覆盖已补齐
3. 运行时功能验证仍需继续补完并记录

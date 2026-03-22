# 阶段 3 设计计划：增加 IDGEN 与 SETNEWID

## 1. 阶段 3 的目标

阶段 3 的目标是在阶段 2 已经打通的 `gprid/pcid/use` 功能语义之上，把“新 ID 生成”这条链路闭合起来。

这一阶段的交付重点有三个：

1. 为 `IDCSR` 的低 24 位 `idgen` 增加稳定的字段级 helper。
2. 实现 `SETNEWID` 的 decode、反汇编和 execute 语义。
3. 把“读当前 `idgen` 写入 `rd.gprid`，随后自增”这套规则通过 baremetal 测试固定下来。

这里的闭环仍然是功能模型闭环，而不是时序流水线闭环。也就是说，本阶段的正确性目标是：

1. `AtomicSimpleCPU` / `TimingSimpleCPU` 上语义正确。
2. `SETNEWID` 能复用阶段 2 已建立的整数写回与 GPRID helper 体系。
3. `DEBUG` 可以直接观察 `IDCSR.idgen` 的变化和 `rd.gprid` 的结果。

本阶段不要求：

1. `MinorCPU` 增加 `idgen` 的 shadow state、回滚或前递机制。
2. `LS/SS/LS_MAP/SS_ID` 已开始消耗 `idgen`。
3. `SWITCHS`、`encmap`、`exitraw` 的任何新行为已经接入。

## 2. 阶段 3 的边界

### 2.1 本阶段要完成的内容

1. 为 `IDCSR` 增加 `idgen` 字段读取、写回和“取值后递增”的 helper。
2. 在 `opcode=0b1011011` 下接入 `func3=0b011` 的 `SETNEWID`。
3. 明确并实现 `SETNEWID` 的低 24 位行为、递增行为和高位掩蔽行为。
4. 为 `DEBUG imm=0/4` 继续提供可观察的 `IDCSR` 字段输出，便于 bring-up。
5. 补齐阶段 3 的 baremetal 汇编测试计划与验收标准。

### 2.2 本阶段明确不做的内容

1. 不修改 `MinorCPU`。
2. 不实现 `LS/SS/LS_MAP/SS_ID`。
3. 不实现 `SWITCHS`。
4. 不改变阶段 2 已经接入的 `gprid/pcid` 传播规则范围。
5. 不在本阶段扩展 `IDCSR` 除 `idgen/use/puse` 之外的新字段语义。

## 3. 当前代码基线与阶段 3 的约束

在写阶段 3 设计之前，必须先把当前代码基线说清楚，否则 `SETNEWID` 的“何时生效”会和文档预期混淆。

截至当前仓库状态：

1. 阶段 2 已经在 `isa.hh/isa.cc` 中实现了 `readIdCsrUse()`、`readIdCsrPuse()`、`shouldApplyIntIdSemantics()` 以及整数 GPRID helper。
2. `IDCSR` 的 CSR mask 已经是“低 24 位 + bit31/bit30”，说明 `idgen/use/puse` 的位布局在代码中已经固定。
3. `SETDUMMYID` 与 `SETRAWID` 已经通过 `writeIntRegIdImmediate()` 走通。
4. 当前 `shouldApplyIntIdSemantics()` 的实现实际上只在“U 态且 `use=1`”时返回 true。
5. 当前 `stage2_s_mode.S` 也明确把 S 态下“扩展指令与普通指令不改变 GPRID”作为基线来验证。

因此，阶段 3 的推荐策略不是重新争论“理论上该不该在 S 态也生效”，而是先保持当前实现基线一致：

1. `SETNEWID` 是否更新 `rd.gprid` 与 `idgen`，沿用阶段 2 已存在的统一判定 helper。
2. 也就是默认按“仅 U 态且 `use=1` 才启用 ID 语义”实现。
3. 如果后续希望回到原始架构描述里“非 U 态也可生效”的更强语义，应单独作为阶段 2/3 语义修正任务处理，而不是在阶段 3 开发中隐式混入。

这是本阶段最重要的审查点之一。

## 4. 本阶段的核心设计决策

### 4.1 `SETNEWID` 复用阶段 2 的 helper 体系

`SETNEWID` 的数据结果与 `ADDI` 一致，和 `SETDUMMYID/SETRAWID` 属于同一类 custom I-type 指令。因此阶段 3 最稳妥的做法是：

1. 数据写回仍沿用 `IOp` 的普通整数写回路径。
2. 新增一条“分配新 ID 并写入 `rd`”的 ISA helper。
3. decoder 里像阶段 2 一样通过 `id_code` 钩子挂接，不重写更底层的执行模板。

这样可以把阶段 3 控制在一个很小的增量范围内。

### 4.2 `idgen` 的语义采用“先取旧值，再低 24 位递增”

`SETNEWID` 的建议语义如下：

1. 数据结果：`rd = rs1 + sext(imm)`。
2. 当 ID 语义生效时：
   1. 读取 `IDCSR.idgen` 当前低 24 位旧值。
   2. 将该旧值写入 `rd` 对应的 `GPRID`。
   3. 将 `IDCSR.idgen` 更新为下一个可分配值。
3. 当 ID 语义不生效时：
   1. 行为完全退化为 `ADDI`。
   2. 不改变 `rd.gprid`。
   3. 不改变 `IDCSR.idgen`。
4. `rd=x0` 时：
   1. 数据结果仍按 RISC-V 规则丢弃。
   2. 不更新 `GPRID0`。
   3. 但只要 ID 语义生效，`idgen` 仍然要递增。

这里特意把 `rd=x0` 的 `idgen` 行为单独固定下来，因为它是最容易在实现里被“顺手 return”误杀的边界。

这里还需要补一个阶段 3 的保留值约束：

1. `IDCSR.idgen` 永远不能分配出 `0/1/2`。
2. 这三个值保留给现有固定语义使用：
   1. `0` 对应 `SETRAWID`。
   2. `1` 对应 `SETDUMMYID`。
   3. `2` 作为 `PCID` 的专用保留值。
3. 因此 `SETNEWID` 在取值和递增过程中都必须跳过 `0/1/2`。
4. 如果软件通过 `csrw idcsr, ...` 人工把 `idgen` 写成 `0/1/2`，那么第一次 `SETNEWID` 也不能把这三个值分配给 `rd.gprid`，而是应先规范化到第一个可分配值再继续执行。

### 4.3 `idgen` 只对低 24 位建模，高位统一掩蔽

当前 CSR mask 已经限定了 `IDCSR` 的有效位，因此阶段 3 建议继续保持：

1. helper 对外暴露的 `idgen` 一律是低 24 位值。
2. 递增时按 24 位回绕，不产生额外 carry 语义。
3. 递增和回绕后的结果如果落到 `0/1/2`，必须继续前进到 `3`。
3. 任何 helper 写回 `IDCSR` 时都必须保留 `puse/use` 位，不污染 bit31/bit30。
4. `DEBUG` 输出时继续显示完整 `IDCSR` 值，并附带 `puse/use/idgen` 展开值。

换句话说，阶段 3 不重新定义 `IDCSR` 的存储格式，只把已有位布局真正用起来。

### 4.4 `SETNEWID` 与 `SETDUMMYID/SETRAWID` 的关系

这三条指令的实现结构应尽量统一：

1. 三者都使用 `IOp` 或同类 custom I-type 模板。
2. 三者的数据路径都等价于 `ADDI`。
3. 差异只体现在 `id_code` 钩子：
   1. `SETDUMMYID` 写常量 1。
   2. `SETRAWID` 写常量 0。
   3. `SETNEWID` 写 `idgen` 旧值，并推进 `idgen`。

这样阶段 3 的代码评审会比较简单，也能降低回归面。

## 5. 详细语义建议

### 5.1 `setnewid rd, rs1, imm`

建议语义：

1. 数据结果与 `ADDI` 相同，`rd = rs1 + sext(imm)`。
2. 当扩展语义生效时，`rd` 对应的 `GPRID` 被写为当前 `IDCSR.idgen`。
3. 当扩展语义生效时，`IDCSR.idgen` 在写完 `rd.gprid` 后前进到下一个可分配值，并限制在低 24 位。
4. 当扩展语义不生效时，其行为完全退化为 `ADDI`，不得改变 `rd` 的 GPRID，也不得改变 `IDCSR.idgen`。
5. `rd=x0` 时，不更新 `GPRID0`，但如果扩展语义生效，仍消耗并递增一次 `idgen`。

这里“当前 `IDCSR.idgen`”进一步约束为：

1. 可分配范围是低 24 位中的除 `0/1/2` 之外的值。
2. 正常软件初始化时，推荐把 `idgen` 设为 `3` 或更大值。
3. 如果当前 `idgen` 因软件显式写 CSR 落在 `0/1/2`，`SETNEWID` 应当自动跳过这些保留值，不把它们写入 `rd.gprid`。

### 5.2 `DEBUG` 观测要求

阶段 3 不需要新增新的 `DEBUG` 子命令，但要保证：

1. `debug imm=0` 可以直接看到 `IDCSR` 当前值及展开后的 `idgen`。
2. `debug imm=4` 在读取 `IDCSR` 时仍能显示 `puse/use/idgen` 三个字段。

这两点对 bring-up 很关键，因为 `SETNEWID` 的错误往往不是算术结果错，而是“拿到的是递增前还是递增后”“高位有没有被污染”。

## 6. 模块级实现计划

### 6.1 `arch/riscv/isa.hh`

职责：声明阶段 3 需要的 `idgen` helper。

本阶段计划改动：

1. 增加 `readIdCsrIdgen()` 或等价 helper。
2. 增加 `writeIdCsrIdgen(RegVal idgen)` 或等价 helper。
3. 增加 `allocateIntRegNewId(ExecContext *xc, RegIndex dst_reg_idx)` 或等价 helper。

设计注意点：

1. helper 名字要表达“分配新 ID 并递增”的语义，而不只是“写一个立即数”。
2. helper 内部统一处理“是否启用 ID 语义”的判断。
3. helper 内部统一处理 `rd=x0` 的特殊规则，避免 decoder 重复写条件分支。

### 6.2 `arch/riscv/isa.cc`

职责：实现阶段 3 的 `idgen` 后端语义。

本阶段计划改动：

1. 实现 `IDCSR.idgen` 的字段读取 helper。
2. 实现“保留 `puse/use` 位，仅更新低 24 位”的写回 helper。
3. 实现“读取旧 `idgen` -> 写 `rd.gprid` -> 递增 `idgen`”的一站式 helper。
4. 视需要补充小型辅助函数，例如 `nextIdgenValue(RegVal old_val)`。

设计注意点：

1. 不能直接覆盖整个 `IDCSR`，否则很容易把 `puse/use` 位写掉。
2. `rd=x0` 不能导致 helper 提前返回而遗漏 `idgen` 自增。
3. 必须显式做 `mask(24)`，不要依赖调用者总是传入干净值。
4. `SETNEWID` 的语义应继续完全收敛在 ISA helper，不把字段操作散到 `.isa` 模板里。

### 6.3 `arch/riscv/isa/decoder.isa`

职责：把 `SETNEWID` 接入 custom opcode decode。

本阶段计划改动：

1. 在 `opcode=0b1011011`、`func3=0b011` 下新增 `SETNEWID`。
2. 数据路径继续使用与 `SETDUMMYID/SETRAWID` 同类的 `IOp` 模板。
3. 在 `id_code` 中调用阶段 3 的新 helper。

设计注意点：

1. `func3=0b011` 当前尚未被占用，正好作为阶段 3 的自然增量。
2. 尽量把三条 `SET*ID` 写成并排结构，便于审阅。

### 6.4 `arch/riscv/regs/misc.hh`

职责：确认 `IDCSR` 位布局定义与阶段 3 语义一致。

本阶段计划改动：

1. 大概率不需要修改 CSR 编号或 mask，因为当前代码已经把 `IDCSR` 掩码定义为低 24 位加 `use/puse`。
2. 如果审阅时认为有必要提高可读性，可以补充少量字段常量或注释。

设计注意点：

1. 阶段 3 不应为了“看起来完整”而重构整块 CSR 表。
2. 只有在 helper 难以表达位含义时，才值得增加字段级常量。

## 7. 测试计划

阶段 3 测试直接复用阶段 2 已有的两个大文件：

1. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_u_mode.S`
2. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_s_mode.S`

做法是只在现有 phase 基础上追加少量 `setnewid` 指令和对应断言，不再新建新的 stage3 测试文件。

建议补充的检查点如下：

1. 在 U 态 `use=1` 的 phase 中：
   1. 预先把 `IDCSR.idgen` 设为已知值。
   2. 连续执行两到三条 `setnewid`。
   3. 检查目标寄存器对应的 `gprid` 是否拿到递增前的旧值序列。
   4. 检查 `IDCSR.idgen` 是否按预期递增，并且不会落到 `0/1/2`。
2. 在 U 态 `use=0` 的 phase 中：
   1. 执行同样的 `setnewid`。
   2. 检查目标 `gprid` 与 `IDCSR.idgen` 都保持不变。
3. 在当前 S 态基线测试中：
   1. 追加 `setnewid`。
   2. 继续检查数据结果正常，但 `gprid` 和 `IDCSR.idgen` 保持不变。

另外建议顺手补一组“跳过保留值”的检查：

1. 把 `idgen` 设为 `3`，验证后续递增不会产生 `0/1/2`。
2. 把 `idgen` 人工设为 `0/1/2`，验证第一条 `setnewid` 就会跳过这三个保留值。
3. 如果实现时一起覆盖回绕边界，也要验证从 `0xFFFFFF` 回绕后会直接跳到 `3`，而不是停在 `0/1/2`。

## 8. 预估改动范围

如果按上述策略推进，阶段 3 预计会修改 4 到 6 个模块，仍主要集中在 `arch/riscv` 与 baremetal 测试：

1. `repo/gem5/src/arch/riscv/isa.hh`
2. `repo/gem5/src/arch/riscv/isa.cc`
3. `repo/gem5/src/arch/riscv/isa/decoder.isa`
4. 视情况少量调整 `repo/gem5/src/arch/riscv/regs/misc.hh`
5. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_u_mode.S`
6. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_s_mode.S`

从实现难度看，本阶段整体属于“低到中等”：

1. 难点不在 decode，而在于把 `idgen` 的旧值/新值顺序、24 位回绕和 `x0` 边界一次性定义清楚。
2. 还要把 `0/1/2` 这三个保留值的跳过规则统一收敛到 helper 里。
3. 一旦 helper 设计干净，代码改动量会明显小于阶段 2。

## 9. 与阶段 4/5 的衔接点

阶段 3 完成后，后续阶段可以直接复用这一轮成果：

1. 阶段 4 在 `MinorCPU` 引入 `idgen` shadow state 时，可以直接复用阶段 3 已固定的“先取旧值再自增”语义。
2. 阶段 5 的 `LS/SS` 若需要消费由 `SETNEWID` 生成的 ID，功能线语义已经有稳定来源。
3. 阶段 7/8 如果要让 `puse/use/idgen` 进入更复杂的时序更新，也能复用阶段 3 的字段 helper 和测试断言模式。

换句话说，阶段 3 的价值不在于新增了一条很复杂的指令，而在于把“ID 从哪里来”这件事正式固定下来。

## 10. 阶段 3 当前实施总结

截至当前版本，阶段 3 已经完成了第一轮代码接入和编译级验证，整体实现遵循了本阶段“只补 `IDGEN/SETNEWID` 功能线，不碰 `MinorCPU`”的边界。

### 10.1 已完成的代码实现

1. 在 `arch/riscv/isa.hh` 中补入了阶段 3 所需的 helper 声明：
   1. `readIdCsrIdgen()`
   2. `writeIdCsrIdgen()`
   3. `allocateIntRegNewId()`
2. 在 `arch/riscv/isa.cc` 中补入了 `IDCSR.idgen` 的字段级后端语义：
   1. `readIdCsrIdgen()` 只读取 `IDCSR` 的低 24 位。
   2. `writeIdCsrIdgen()` 只更新 `IDCSR` 的低 24 位，保留 `puse/use` 位不变。
   3. `allocateIntRegNewId()` 统一实现了 `SETNEWID` 的分配逻辑：
      1. 只有在当前阶段 2 已建立的 ID 语义生效条件下才真正执行。
      2. 当前 `idgen` 如果是 `0/1/2`，会先规范化到 `3`。
      3. 分配给 `rd.gprid` 的始终是当前可分配的旧值。
      4. 分配完成后会推进到下一个可分配值。
      5. 推进和回绕过程中都会跳过 `0/1/2`。
      6. `rd=x0` 时不会改写 `GPRID0`，但仍会推进 `idgen`。
3. 在 `arch/riscv/isa/decoder.isa` 中完成了 `SETNEWID` 的 decode 与执行接入：
   1. 在 `opcode=0b1011011`、`func3=0b011` 下新增 `setnewid`。
   2. 数据路径与 `ADDI` 保持一致。
   3. ID 路径通过 `id_code` 钩子调用 `allocateIntRegNewId()`。

### 10.2 已完成的测试修改

阶段 3 没有新建测试文件，而是直接在阶段 2 的两份大测试上增量补充 `setnewid` 检查：

1. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_u_mode.S`
   1. 在 U 态 `use=1` 的 phase 中追加多条 `setnewid`。
   2. 验证 `rd.gprid` 拿到的是递增前旧值序列。
   3. 验证 `idgen` 会推进，并且会跳过 `0/1/2`。
   4. 验证 `setnewid x0, ...` 不改 `GPRID0`，但仍推进 `idgen`。
   5. 在 U 态 `use=0` 的 phase 中验证 `setnewid` 不改变 `gprid` 和 `idcsr`。
2. `benchmark/simple-sigriscv-test/gem5_test/tests/stage2_s_mode.S`
   1. 在当前 S 态基线测试中追加 `setnewid`。
   2. 继续验证数据结果正常，但 `gprid` 和 `idcsr` 保持不变。
   3. 在 U 态 `use=0` 的回归段中也验证 `setnewid` 不生效。

### 10.3 当前实现效果

按当前仓库语义基线，阶段 3 现在已经实现了下面这些效果：

1. gem5 可以识别并执行 `SETNEWID`。
2. `SETNEWID` 的整数结果与 `ADDI` 一致。
3. 当 ID 语义生效时，`SETNEWID` 会把当前 `IDCSR.idgen` 的可分配旧值写入目标寄存器对应的 `GPRID`。
4. `IDCSR.idgen` 在每次成功分配后都会推进到下一个可分配值。
5. `0/1/2` 不会被 `SETNEWID` 分配出去，递增和回绕时都会被跳过。
6. `setnewid x0, ...` 不会改写 `GPRID0`，但仍会消耗一次 `idgen`。
7. 在当前阶段 2 已落地的语义基线下，S 态以及 U 态 `use=0` 时，`SETNEWID` 不会改变 `gprid` 或 `idcsr`。

### 10.4 当前验证状态

已完成的验证：

1. `scons build/RISCV/arch/riscv/generated/decoder.o build/RISCV/arch/riscv/decoder.o build/RISCV/arch/riscv/isa.o -j4` 已通过。
2. `make test-elf TEST=stage2_u_mode` 已通过。
3. `make test-elf TEST=stage2_s_mode` 已通过。

当前还没有完成的验证：

1. 还没有完成完整 `build/RISCV/gem5.opt` 的端到端链接。
2. 还没有在 gem5 仿真里实际运行 `stage2_u_mode` 与 `stage2_s_mode`，验证运行时结果和日志输出。

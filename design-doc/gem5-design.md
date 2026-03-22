# gem5 RISC-V 指令集扩展可行性评估与开发计划

## 1. 结论

这套扩展在 gem5 上是可行的，但必须分成两条线开发：

1. 功能线：先在 `AtomicSimpleCPU` / `TimingSimpleCPU` 上实现正确的 ISA 语义、CSR 行为、调试输出和加解密功能。
2. 时序线：在 `MinorCPU` 上实现和流水线相关的 ID 伴随数据流、`use/puse/exitraw` 伴随控制流、`encmap` 的时序更新，以及异常回滚时的一致性。

如果目标是“原有指令时序不变”，推荐的工程解释是：

1. 对未启用扩展的程序，原有指令延迟、功能单元配置、cache 路径保持不变。
2. 对启用扩展但未执行扩展指令的程序，新增状态只做旁路携带，不给普通指令增加额外固定延迟。
3. 只有 `LS/SS/LS_MAP/SS_ID/SWITCHS` 等扩展指令引入新增延迟或 stall。

这个目标对 `SimpleCPU` 很容易做到，对 `MinorCPU` 可做到，对 `O3CPU` 难度很高，不建议第一阶段纳入。

## 2. 可行性与难度评估

### 2.1 总体可行性

从 gem5 现有结构看，这个扩展有明确落点：

1. ISA 解码和指令语义位于 `repo/gem5/src/arch/riscv/isa/*.isa`。
2. CSR 编号、名字、掩码和物理映射位于 `repo/gem5/src/arch/riscv/regs/misc.hh`。
3. CSR 读写、权限检查和架构态存储位于 `repo/gem5/src/arch/riscv/isa.hh` 与 `repo/gem5/src/arch/riscv/isa.cc`。
4. 功能模型主要走 `repo/gem5/src/cpu/simple/*`。
5. 时序准确顺序流水线主要走 `repo/gem5/src/cpu/minor/*`。

因此它不是“能不能做”的问题，而是“哪些状态放在 ISA 层，哪些状态必须下沉到 MinorCPU 动态流水线层”。

### 2.2 难度分级

1. 低难度：新增 CSR、`DEBUG`、`SETDUMMYID`、`SETRAWID`、`SETNEWID` 的功能语义。
2. 中难度：`gprid/pcid` 的功能语义、普通指令的 ID 传播规则、`LS/SS` 的功能版加解密。
3. 高难度：`MinorCPU` 中 `gprid/pcid/use` 的流水传递、scoreboard 和回滚一致性。
4. 很高难度：`LS/SS/LS_MAP/SS_ID` 在时序流水线中插入新延迟而不污染普通 load/store 的原始路径。
5. 很高难度：`SWITCHS` 提前影响前端控制流，并与异常、返回、flush 协同。

### 2.3 最大设计风险

1. gem5 标准 ISA 框架默认只跟踪 GPR/CSR/PC，不天然有“GPR 绑定的附属 ID 状态”。
2. 如果把 `gprid` 全当成普通 CSR，在 `MinorCPU` 中会形成隐藏依赖，导致大量错误 stall 或错误前递。
3. 如果把 `use/puse/encmap/exitraw/idgen` 只放在 CSR 架构态，异常回滚时容易和流水线中间状态不一致。
4. 如果一开始就碰 `O3CPU`，rename、ROB、LSQ、commit/squash 都要同步改，开发量会膨胀。

## 3. 推荐架构切分

### 3.1 建议的状态归属

1. 架构态 CSR：`MKEYH/MKEYL/SKEYH/SKEYL/GPRID0-31/PCID/IDCSR/ENCMAP/EXITRAW`。
2. 功能执行辅助状态：QARMA helper、debug helper、扩展状态读写 helper。
3. Minor 前端控制态镜像：`use/puse/exitraw/pcid`。
4. Minor 数据流镜像：每条动态指令携带 `rs1_id/rs2_id/rd_id`，必要时再带 `pc_id`。
5. Minor 临时可回滚状态：`idgen/use/puse/encmap/exitraw` 的 pipeline-visible shadow state。

### 3.2 设计原则

1. CSR 负责软件可见性，shadow state 负责流水线立即可见性。
2. `pcid/gprid` 采用“伴随 PC/GPR 传递”的设计，不把它们当成普通 CSR 依赖来调度。
3. `encmap/use/puse/exitraw/idgen` 采用“前台临时态 + commit 写回架构 CSR”的设计。
4. QARMA 实现先做纯函数 helper，再接入时序延迟模型。

## 4. 分阶段开发计划

我建议把原来的 6 步改成 8 步，原因是“功能正确”和“时序正确”需要穿插推进，尤其是 `LS/SS` 与 `SWITCHS`。

### 阶段 0：设计基线与测试骨架

详细设计与实施产物见 `design-doc/stage0-design.md`。


目标：
1. 固化 CSR 编号、指令编码、权限规则、回滚规则。
2. 建立 baremetal 自测程序、日志格式和 golden output。

开发：
1. 补充设计文档。
2. 在测试目录新增最小汇编测试框架，区分 functional 和 timing 两套运行脚本。

测试：
1. 反汇编正确。
2. 非扩展程序回归不变。

改动模块数估计：4 到 6 个。

### 阶段 1：CSR 骨架与 DEBUG 指令

目标：
1. 先把调试能力打通。
2. 让软件能够读写新 CSR，便于后续自举。

功能：
1. 新增 9 组扩展 CSR。
2. 实现 `DEBUG` 指令的 decode、disassembly、execute。
3. 提供统一 dump helper。

开发模块：
1. `arch/riscv/regs/misc.hh`：新增 `MISCREG_*`、CSR 编号、metadata、mask。
2. `arch/riscv/isa.cc`：新增 CSR 读写、权限检查、序列化。
3. `arch/riscv/isa/decoder.isa`：挂入 `opcode=0b1011011`。
4. `arch/riscv/isa/formats/*`：增加 custom I/S/R format 或复用现有 format。
5. `arch/riscv/insts/` 或 helper 文件：debug 输出与仿真退出。

测试：
1. `csrr/csrw` 访问新 CSR。
2. `debug imm=0..5` 行为正确。
3. checkpoint/restore 后 CSR 不丢失。

难度：低。
改动模块数估计：5 到 8 个。

### 阶段 2：功能线 ID 语义与基础扩展算术

目标：
1. 在 simple CPU 上先跑通 `gprid/pcid/use` 的功能语义。
2. 实现 `SETDUMMYID/SETRAWID`，并覆盖普通指令 ID 传播规则。

功能：
1. `gprid0-31`、`pcid` 的软件可见语义。
2. `add/sub/addi` 传递 `rs1` 的 ID。
3. `auipc/jal/jalr` 传递 `pcid`。
4. 其他写 GPR 的指令默认清空目标 ID。

开发模块：
1. `arch/riscv/isa.cc`：增加读写 GPRID/PCID helper。
2. `arch/riscv/isa/*.isa`：在相关指令 execute 模板中插入 ID 更新逻辑。
3. `cpu/simple/exec_context.hh` 或相关执行路径：保证 helper 可访问线程态。

测试：
1. 算术链条 ID 传播。
2. 分支/跳转后返回地址 ID。
3. `use=0` 时 U 态任何普通指令不得影响 ID。

难度：中。
改动模块数估计：6 到 10 个。

### 阶段 3：IDCSR 低 24 位与 SETNEWID

目标：
1. 在功能线上闭合 ID 生成链路。
2. 为后续 LS/SS 提供稳定 ID 来源。

功能：
1. `IDCSR` 的 `use/puse/idgen` 分域访问。
2. `SETNEWID` 从 `idgen` 取值，写入 `rd` 的 GPRID，再自增。

开发模块：
1. `arch/riscv/regs/misc.hh`：补掩码和字段定义。
2. `arch/riscv/isa.cc`：`IDCSR` 字段 helper。
3. `arch/riscv/isa/*.isa`：`SETNEWID` 语义。

测试：
1. `idgen` 递增。
2. `use=0` 降级为 `ADDI`。
3. 越界和高位屏蔽行为。

难度：低到中。
改动模块数估计：3 到 5 个。

### 阶段 4：MinorCPU 的 ID 伴随数据流

目标：
1. 把功能语义下沉到时序顺序流水线。
2. 不影响普通指令时序，只在流水寄存器旁带 metadata。

功能：
1. `rs1_id/rs2_id/rd_id/pc_id/use_snapshot` 进入动态指令。
2. decode、scoreboard、execute、writeback 路径携带 ID。
3. flush/branch recovery 时 GPRID 与 PCID 可回滚。

开发模块：
1. `cpu/minor/dyn_inst.hh`：新增 ID 字段。
2. `cpu/minor/pipe_data.hh`：在 stage 间增加 metadata。
3. `cpu/minor/decode.*`：生成目标 ID 更新意图。
4. `cpu/minor/execute.*`：在写回时提交 GPRID/PCID。
5. `cpu/minor/scoreboard.*`：必要时扩展对 ID-ready 的跟踪。
6. `cpu/minor/fetch1.*`、`fetch2.*`：携带 `pcid/use` 控制态。

测试：
1. 前递路径下 ID 不丢。
2. squash 后 ID 不污染。
3. 普通程序 CPI 不应明显变化。

难度：高。
改动模块数估计：8 到 12 个。

### 阶段 5：LS/SS 功能版与 QARMA helper

目标：
1. 先在 simple CPU 跑通 `LS/SS` 语义。
2. 确认密钥、ID 选择、40-bit 地址重建规则无歧义。

功能：
1. `LS` 在 `use=0` 时退化为 `LD`，否则执行解密并拆出 `rd` 与 `rd.gprid`。
2. `SS` 在 `use=0` 时退化为 `SD`，否则执行拼接和加密。
3. S 态使用 `mkey`，U 态使用 `skey + rs1/rs2 对应 id`。

开发模块：
1. `arch/riscv/isa/formats/mem.isa` 或新增 custom mem format。
2. `arch/riscv/isa/*.isa`：新增 `LS/SS` decode 和 execute。
3. `arch/riscv/` 下新增 QARMA helper。

测试：
1. 退化路径与 `LD/SD` 完全一致。
2. S/U 态密钥和 ID 选择正确。
3. 解密结果的 24/40 bit 拆装正确。

难度：中到高。
改动模块数估计：5 到 8 个。

### 阶段 6：MinorCPU 的 LS/SS 时序化

目标：
1. 在时序线插入加解密延迟。
2. 保持普通 `LD/SD` 路径不变。

功能：
1. `LS` 读出后进入解密子阶段，必要时 stall 到结果可写回。
2. `SS` 在地址翻译或 cache pipeline 邻近阶段并行触发加密。
3. 新延迟只对扩展指令生效。

开发模块：
1. `cpu/minor/execute.*`：新增扩展 load/store 流程控制。
2. `cpu/minor/lsq.*`：扩展请求完成后的后处理。
3. `cpu/minor/func_unit.*`：如采用显式功能单元，可建 crypto 延迟模型。
4. 必要时补 `MinorDynInst` 状态位表示“等待解密完成”。

测试：
1. load-use hazard 下 stall 正确。
2. store 访存顺序不乱。
3. 普通 `LD/SD` 时序回归不变。

难度：很高。
改动模块数估计：6 到 10 个。

### 阶段 7：ENCMAP、LS_MAP、SS_ID

目标：
1. 打通按寄存器位图选择是否加解密的机制。
2. 将 `encmap` 纳入可回滚的时序状态。

功能：
1. `LS_MAP` 根据 `ENCMAP[rd]` 选择 `LS` 或 `LD`，并在命中时清零该位。
2. `SS_ID` 根据 `rs2.gprid==0` 选择 `SD` 或 `SS`，并同步更新 `ENCMAP`。
3. `encmap` 具有 shadow state 与 commit 写回。

开发模块：
1. `arch/riscv/isa.cc`：`ENCMAP` helper。
2. `arch/riscv/isa/*.isa`：`LS_MAP/SS_ID`。
3. `cpu/minor/decode.*` 与 `execute.*`：shadow `encmap` 的读改写。

测试：
1. 位图命中与清零行为。
2. 异常回滚后 `encmap` 一致。
3. 连续 `LS_MAP/SS_ID` 的竞争条件。

难度：高。
改动模块数估计：5 到 8 个。

### 阶段 8：EXITRAW、PUSE/USE、SWITCHS

目标：
1. 完成控制流模式切换。
2. 解决 `use` 对后续 ID/加解密行为的统领作用。

功能：
1. `SWITCHS` 在 `use=1` 时改变 `PC`、`RD`、`EXITRAW`、`PUSE`、`USE`。
2. 当前端再次遇到 `PC==EXITRAW` 时自动恢复 `USE=PUSE`、`PUSE=0`。
3. `use` 快照随指令进入流水线，避免中途切换污染已发射指令。

开发模块：
1. `arch/riscv/isa/*.isa`：`SWITCHS` 功能语义。
2. `cpu/minor/fetch1.*`、`fetch2.*`：前端检测 `exitraw` 与 use 切换。
3. `cpu/minor/decode.*`、`execute.*`：shadow `use/puse/exitraw` 和 commit。

测试：
1. `switchs` 嵌套与返回。
2. 分支预测/flush 下 `use` 恢复。
3. 异常进入和返回时的状态一致性。

难度：很高。
改动模块数估计：7 到 11 个。

## 5. 模块级改动清单

### 5.1 必改公共模块

1. `repo/gem5/src/arch/riscv/regs/misc.hh`
   作用：定义新 CSR 编号、物理索引、metadata、mask。
2. `repo/gem5/src/arch/riscv/isa.hh`
   作用：在 ISA 对象里增加扩展状态 helper 接口。
3. `repo/gem5/src/arch/riscv/isa.cc`
   作用：CSR 读写、权限检查、序列化、调试输出、扩展 helper。
4. `repo/gem5/src/arch/riscv/isa/decoder.isa`
   作用：接入 `opcode=0b1011011`。
5. `repo/gem5/src/arch/riscv/isa/formats/*.isa`
   作用：补 custom I/S/R 指令格式和执行模板。

### 5.2 功能线模块

1. `repo/gem5/src/cpu/simple/base.*`
2. `repo/gem5/src/cpu/simple/exec_context.hh`
3. 必要时 `repo/gem5/src/cpu/simple/atomic.*`
4. 必要时 `repo/gem5/src/cpu/simple/timing.*`

职责：保证 simple CPU 能访问扩展 CSR/helper，并完成功能语义验证。

### 5.3 时序线模块

1. `repo/gem5/src/cpu/minor/dyn_inst.hh`
2. `repo/gem5/src/cpu/minor/pipe_data.hh`
3. `repo/gem5/src/cpu/minor/decode.*`
4. `repo/gem5/src/cpu/minor/execute.*`
5. `repo/gem5/src/cpu/minor/lsq.*`
6. `repo/gem5/src/cpu/minor/fetch1.*`
7. `repo/gem5/src/cpu/minor/fetch2.*`
8. `repo/gem5/src/cpu/minor/scoreboard.*`

职责：实现 metadata 传递、shadow state、时序化加解密、控制流切换和回滚。

## 6. 测试与验证安排

每个阶段都建议同时做 4 类测试：

1. 指令级单测：单条或短序列汇编，验证寄存器、CSR 和内存结果。
2. 降级一致性测试：`use=0` 时与原生 `LD/SD/ADDI/CALL` 一致。
3. 异常与回滚测试：页故障、非法指令、branch flush、interrupt 后状态一致。
4. 非扩展回归：原始 baremetal 程序性能和行为不变。

建议测试资产分三层：

1. `tests/asm/`：最小化汇编。
2. `tests/check/`：golden log 对比。
3. `benchmark/simple-sigriscv-test/gem5_test/`：集成测试与回归脚本。

## 7. 实施建议

1. 第一里程碑只承诺 `DEBUG + CSR + SET*ID + simple CPU`。
2. 第二里程碑承诺 `MinorCPU` 的 ID 传播，不碰加解密时序。
3. 第三里程碑再做 `LS/SS` 功能与时序。
4. 第四里程碑做 `ENCMAP` 与 `SWITCHS`。
5. 第一阶段明确不支持 `O3CPU`，否则开发量会从十几个模块迅速扩大到二十多个模块。

## 8. 最终建议

最稳妥的顺序不是“直接把六步同时压到两个 CPU 模型上”，而是：

1. 先把 ISA/CSR/调试和 simple CPU 功能线做扎实。
2. 再把 `MinorCPU` 的 ID 元数据通路打通。
3. 再逐步接入 `LS/SS`、`ENCMAP`、`SWITCHS` 三个高风险特性。

这样做的好处是：

1. 每一阶段都有可运行、可验证的结果。
2. 每一阶段都能缩小 bug 范围。
3. 不会因为过早进入复杂时序回滚而失去调试抓手。

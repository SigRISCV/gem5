/home/zyy/sigriscv/sigriscv-doc/gem5-design.md：我现在希望对RISCV的GEM5做一个指令集扩展，直接在RISCV的处理器上做，并且不影响原来指令的时序。

完整的指令集描述如下：

特权寄存器扩展：
1. MKEYH，M 态特权寄存器，用于存储主密钥的高 64 位，编号 7F1
2. MKEYL，M 态特权寄存器，用于存储主密钥的低 64 位，编号 7F0
3. SKEYH，S 态特权寄存器，用于存储次密钥的低 64 位，编号 5F1
4. SKEYL，S 态特权寄存器，用于存储次密钥的低 64 位，编号 5F0
5. GPRID0-GPRID31，S 态特权寄存器，每个 GPR 对应有一个，用于存储指针 ID，编号 5D0-5EF
6. PCID，S 态特权寄存器，PC 对应的 ID 寄存器，存储 PC 的 ID，编号 5F2
7. IDCSR，S 态特权寄存器，最高为是 puse，次高位是 use，低 24 位是 ID 生成器，编号 5F3
8. ENCMAP，S 态特权寄存器，仅低 32 位有用，编号 5F4
9. EXITRAW，S 态特权寄存器，用于记录 switchs 指令的返回地址，编号 5F5

1. 指令扩展，opcode 全部都是 0b1011011
2. 指令 LS，I 类型指令，FUNC3 的值是 0b011，格式为 ls rd，rs，imm。将 RS1+IMM 地址的 64 位数据读出，然后用 QARMA 算法做一个解密，在 S 态解密的密钥是 mkey，ID 是 0；在 U 态解密的密钥是 skey， ID 是 RS 寄存器对应的 GPRID 特权寄存器的值。解密后的结果的高 24 位存入 RD 寄存器对应的 GPRID 特权寄存器中，余下的 40 位地址高位扩展，然后存入 RD 寄存器。use=1 才工作，如果 use=0，则执行效果等价于 LD。
3. 指令 SS，S 类型指令，FUNC3 的值是 0b101，格式是 ss rs2, rs1, imm。将 RD 寄存器的低 40 位和对应的 GPRID 寄存器的低 24 位结合到一起，然后用 QARMA 算法做一个加密，在 S 态解密的密钥是 mkey，ID 是 0；在 U 态解密的密钥是 skey， ID 是 RS 寄存器对应的 GPRID 特权寄存器的值。加密的结果存入 RS1+IMM 的地址中。use=1 才工作，如果 use=0，则执行效果等价于 SD。
4. 指令 LS_MAP，I 类型指令，FUNC3 的值是 0b100，格式为 ls.map rd，rs，imm。在写入寄存器 RD 的时候检查 ENCMAP 的第 RD 位，如果是 1,则执行 LS 的操作，然后将 ENCMAP 寄存器对应位设置为 0；如果是 0 则执行 LD 的操作。use=1 才工作，如果 use=0，则执行效果等价于 LD。
5. 指令 SS_ID，S 类型指令，FUNC3 的值是 0b110，格式是 ss.id rs2, rs1, imm。在执行 store 前检查 RS2 对应的 GPRID 是不是 0，如果是 0 的话就把对应的 ENCMAP 设置为 0,然后执行 SD 操作；不然的话，就将对应的 ENCMAP 设置位 1,然后执行 SS 操作。use=1 才工作，如果 use=0，则执行效果等价于 SD。
6. 指令 SETDUMMYID，I 类型指令，FUNC3 的值是 0b010，格式是 setdummyid rd, rs, imm。将 RS+IMM 的值写入 RD，将 RD 对应的 GPRID 设置为 1。use=1 才工作，如果 use=0，则执行效果等价于 ADDI。
7. 指令 SETRAWID，I 类型指令，FUNC3 的值是 0b110，格式是 setrawid rd, rs, imm。将 RS+IMM 的值写入 RD，将 RD 对应的 GPRID 设置为 0。use=1 才工作，如果 use=0，则执行效果等价于 ADDI。
8. 指令 SETNEWID，I 类型指令，FUNC3 的值是 0b011，格式是 setnewid rd, rs, imm。将 RS+IMM 的值写入 RD，将 RD 对应的 GPRID 设置为特权寄存器 IDCSR 的低 24 位， IDCSR 的低 24 位递增。use=1 才工作，如果 use=0，则执行效果等价于 ADDI。
9. 指令 DEBUG，I 类型指令，FUNC3 的值是 0b111, 格式是 debug rd, rs, imm。这是一条用于模拟器模拟的值，不是真的需要严格实现的指令，只是为了起到一些输出调式的作用。当 imm=0 的时候输出所有的 GPR 和 扩展特用寄存器的值；当 imm=1 的时候将 RS1 的值当作字符 %c 输出；当 imm=2 的时候将 RS1 的值当作整数 %d 输出；当 imm=3 的时候将 RS1 的值当作指针 %p 输出，同时输出对应的 GPRID 的值；当 imm=4 的时候，将 RS1 对应的特权寄存器编号的特权寄存器的值输出；当 imm=5 的时候直接退出仿真。这里的输出就是当作一种串口的替代实现来使用。
10. 指令 SWITCHS，R 类型指令，FUNC3 是 0b100，格式是 switchs rd，rs1，rs2。执行之后 PC 等于 RS1，RD 等于 RS1+4，EXITRAW 等 RS1+4，PUSE=use，use=0。等 PC 再次等于 EXITRAW 的时候，use=PUSE，PUSE=0。use=1 才工作，如果 use=0，则执行效果等价于 CALL。
11. 在 u 态如果 use=0，任何指令不能影响 ID；如果 use=1，除了上述指令对 id 的影响外：add/sub/addi 会将 rs1 的 id 传递给 rd 的 id；auipc/jal/jalr 会把 pc 的 id 传递给 rd 的 id；其他的操作则会把寄存器的 id 清空。因为只有指针数据才会有 id，所以只有指针在相关的操作才会保留 id。

现在我们介绍微架构的设计建议，这里分为前端（pc更新），中端（获得regfile的值，更新 scoreboard，发射等），后端（内存操作、数据计算等）
1. 对于 keyl/keyh 寄存器放在什么地方都可以，因为 u 态不会影响他们，s 态修改了他们也不会马上使用，但是最好是靠近后端，因为要给 alu 计算
2. gprid0-gprid31 最好位于中端的 regfile 旁边，然后有一个 gpridfile，在每次 regfile 去指令或者提交的时候获得对应的 id。原来的流水线在传递 rs1、rs2 的线路旁边，中间寄存器里对应的增加 rs1 id 和 rs2 id 的位置，方便他们在流水线传输、使用、前递
3. encmap 最好位于 decode 区域，这样在 ls.MAP 和 ss.ID 发射的时候就可以及时从 encmap 中得到结果并且更新 encmap 的值
4. pcid 位于 pc 旁边，在传递 pc 的时候顺便传递，不过 pcid 不会变，所以问题不大
5. idcsr 的 puse 和 use 位于前端或者中端，这样一旦 switchs 执行或者 exitraw 匹配就可以立刻修改
6. idcsr 的 id 生成部分位于中端，这样 setnewid 取 rs1 的 id 的时候可以直接用低 24 位，然后高位递增
7. exitraw 最好位于前端，然后当 switchs 在前端被发射的时候可以立刻修改 puse 和 use，然后后续指令发射的时候，流水线携带的 use 信号可以关闭后续的加解密操作和 id 操作，可以看到 use、pcid 伴随控制流传递，而 gprid 伴随数据流传递
8. setnewid、setdummyid、setrawid 在 addi 的基础上选择使用 idcsr 低 24 位、0、1 作为 rs1 的 gpid，然后传递给 rd 的 gprid，而不是原来的值
9. switchs 在发射的时候更新 puse 和 use，其他和 call 一样
10. ls/ss/ls.MAP/ss.ID 和 ld/sd 基本相似，但是会根据 priv，use 和 encmap 选择自己的行为。对于加密操作，在 alu 操作结束之后，在 cache pipeline 的页表翻译阶段并行处理，加密 1-3 周期，然后写入 cache；对于解密操作，在 cache 读出数据之后开始和后续的流水线并行解密，解密 1-3 周期，这里估计要 stall，不然无法隐藏，然后写入 regfile 或者前递
11. 对于指令对于特权寄存器的更新需要考虑提交和回滚的问题，如果我们在指令发射和执行的中间就修改了 csr，那么后续指令如果回滚了，csr 就不能回滚了；但是如果 csr 要等指令提交了才更新，那么后续的指令就必须等待 csr 的更新，出现隐式的数据流依赖，这样会导致巨大的额外开销。因此这里引入一个设计，对于 pcid、gprid 因为是和 pc/gpr 绑定的，所以二者共轭传输、修改、回滚就可以了，可以借用 pc 和 gpr 的回滚机制的；对于 keyl/keyh 因为在使用的模式内部是不会修改的，所以不需要专门的同步保证，也不用考虑；但是 puse/use/idgen/encmap/exitraw 是要不断修改的，对这五个寄存器我们这么做：每个寄存器除了 csr 对应的寄存器之外还有一个内部的临时寄存器，在 csrw 操作的时候同时修改 csr 寄存器和内部的临时寄存器；在指令传输的流水线中，除了传递当前指令的 pc，返回值，中间结果等信息，还有对这些 csr 的返回值，这里引入一个 44 位的字段，低 42 位存修改的寄存器的值，2 位指示这个特权寄存器是哪一个，一旦异常中断发生，异常指令前发射的指令可以将自己存的特权寄存器提交值写回到 csr，异常指令后发射的指令那么就可以避免回滚引发的不一致了；在指令操作的时候，指令操作到对应的阶段就立刻读写临时寄存器，但是直到指令提交才真的修改 csr 对应的真实寄存器，这样就不需要将后续流水线中的修改值前递过来了。setnewid 在中端修改 idgen 字段（24 位），编号是 0；switchs 和 pc 的写入在前端或者中端修改 puse/use/exitraw 字段（分别是 1 位 + 1 位 + 40 位），编号是 1；ls.MAP，ss.ID 在中端发射的时候修改 encmap 字段（32 位），编号是 2；

实现顺序：
1. 首先实现 debug，确保可以输出信息用于调试
2. 实现 pcid 和 gprid，流水线中的 id 传递，然后实现 auipc/add/addi/sub/jalr/jal/setdummyid/setrawid 等操作
3. 实现 idcsr 的低 24 位实现 setnewid
4. 实现加解密流水线，实现 ls/ss
5. 加入 encmap，实现 ls.MAP/SS.ID
6. 加入 exitraw 和 puse/use，实现 switchs

我们首先在时序准确的顺序流水线和不要求时序准确的模拟器上，按照这六个步骤双线开发。
开发的时候除了满足上面说的功能要求之外，还要考虑数据竞争、控制竞争之类的同步问题。

再正式开始开发之前，请思考这个设计在 gem5 上实现的可行性如何？各个步骤的可行性和难易程度怎么样？基于他的可行性和难易程度应该怎么设计开发计划，分为多少个步骤，每个步骤的实现哪些功能，做哪些开发、设计、测试？在开发的时候，每个步骤大概要增加或者修改多少个模块，每个模块是谁，加在那里，和其他的模块之间的关系要怎么调整？请列出你的计划、设计和安排，并写入文档

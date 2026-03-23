import argparse

import m5
from m5 import (
    debug,
    event,
    trace,
)
from m5.objects import *

CPU_TYPES = {
    "atomic": AtomicSimpleCPU,
    "timing": TimingSimpleCPU,
    "minor": MinorCPU,
    "o3": O3CPU,
}


class L1Cache(Cache):
    assoc = 2
    tag_latency = 2
    data_latency = 2
    response_latency = 2
    mshrs = 4
    tgts_per_mshr = 20


class L1ICache(L1Cache):
    size = "16KiB"


class L1DCache(L1Cache):
    size = "64KiB"


class L2Cache(Cache):
    size = "256KiB"
    assoc = 8
    tag_latency = 20
    data_latency = 20
    response_latency = 20
    mshrs = 20
    tgts_per_mshr = 12


def build_system(args):
    cpu_cls = CPU_TYPES[args.cpu_type]

    system = System(mmap_using_noreserve=True)
    system.mem_mode = cpu_cls.memory_mode()
    system.mem_ranges = [AddrRange(0x0, size=args.mem_size)]

    system.workload = m5.objects.RiscvBareMetal()
    system.workload.bootloader = args.kernel
    system.workload.remote_gdb_port = args.remote_gdb_port
    system.workload.wait_for_remote_gdb = args.wait_gdb

    system.clk_domain = SrcClockDomain()
    system.clk_domain.clock = args.sys_clock
    system.clk_domain.voltage_domain = VoltageDomain()

    system.cpu_voltage_domain = VoltageDomain()
    system.cpu_clk_domain = SrcClockDomain()
    system.cpu_clk_domain.clock = args.cpu_clock
    system.cpu_clk_domain.voltage_domain = system.cpu_voltage_domain

    system.cpu = cpu_cls(clk_domain=system.cpu_clk_domain, cpu_id=0)

    system.l2cache = L2Cache()
    system.l2bus = L2XBar()
    system.membus = SystemXBar()
    system.system_port = system.membus.cpu_side_ports

    system.cpu.icache = L1ICache()
    system.cpu.dcache = L1DCache()

    system.l2bus.mem_side_ports = system.l2cache.cpu_side
    system.l2cache.mem_side = system.membus.cpu_side_ports
    system.cpu.icache.mem_side = system.l2bus.cpu_side_ports
    system.cpu.dcache.mem_side = system.l2bus.cpu_side_ports

    system.cpu.icache_port = system.cpu.icache.cpu_side
    system.cpu.dcache_port = system.cpu.dcache.cpu_side
    system.cpu.mmu.itb.walker.port = system.membus.cpu_side_ports
    system.cpu.mmu.dtb.walker.port = system.membus.cpu_side_ports

    system.cpu.createInterruptController()
    system.cpu.createThreads()

    system.mem_ctrl = MemCtrl()
    system.mem_ctrl.dram = DDR3_1600_8x8()
    system.mem_ctrl.dram.range = system.mem_ranges[0]
    system.mem_ctrl.dram.device_size = "2048MiB"
    system.mem_ctrl.port = system.membus.mem_side_ports

    return system


def main():
    parser = argparse.ArgumentParser(
        description="RISC-V bare-metal runner with the fast-forward.py memory hierarchy"
    )
    parser.add_argument(
        "--kernel", required=True, help="Bare-metal ELF to load"
    )
    parser.add_argument(
        "--cpu-type",
        choices=sorted(CPU_TYPES),
        default="timing",
        help="CPU model to use",
    )
    parser.add_argument(
        "--sys-clock", default="1GHz", help="Top-level system clock"
    )
    parser.add_argument("--cpu-clock", default="1GHz", help="CPU clock")
    parser.add_argument(
        "--mem-size", default="32GiB", help="Physical memory size"
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=100000,
        help="Simulation ticks to run; 0 means no limit",
    )
    parser.add_argument(
        "--remote-gdb-port",
        type=int,
        default=7000,
        help="Remote GDB port for the FS bare-metal workload",
    )
    parser.add_argument(
        "--debug-flags",
        default="",
        help="Comma-separated debug flags, e.g. MinorSigriscv,MinorExecute",
    )
    parser.add_argument(
        "--debug-file",
        default="",
        help="Write debug output to this file instead of the default sink",
    )
    parser.add_argument(
        "--debug-start",
        type=int,
        default=0,
        help="Enable debug output starting at this tick",
    )
    parser.add_argument(
        "--wait-gdb",
        action="store_true",
        help="Wait for a remote GDB connection before starting execution",
    )
    args = parser.parse_args()

    system = build_system(args)
    Root(full_system=True, system=system)
    m5.instantiate()

    debug_flags = [
        flag.strip() for flag in args.debug_flags.split(",") if flag.strip()
    ]
    for flag in debug_flags:
        if flag not in debug.flags:
            print(f"invalid debug flag '{flag}'")
            continue
        debug.flags[flag].enable()

    if args.debug_file:
        trace.output(args.debug_file)

    if args.debug_start > 0:
        trace.disable()
        e = event.create(trace.enable, event.Event.Debug_Enable_Pri)
        event.mainq.schedule(e, args.debug_start)

    if args.wait_gdb:
        print(
            "Waiting for remote GDB connection on port {}".format(
                args.remote_gdb_port
            )
        )

    if args.max_ticks > 0:
        exit_event = m5.simulate(args.max_ticks)
    else:
        exit_event = m5.simulate()

    print(
        "Simulation exiting @ tick {} because {}".format(
            m5.curTick(), exit_event.getCause()
        )
    )


main()

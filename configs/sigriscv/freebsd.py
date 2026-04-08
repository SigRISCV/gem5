import argparse
import subprocess
from pathlib import Path

import m5

from gem5.components.boards.riscv_board import RiscvBoard
from gem5.components.cachehierarchies.classic.private_l1_private_l2_walk_cache_hierarchy import (
    PrivateL1PrivateL2WalkCacheHierarchy,
)
from gem5.components.memory import SingleChannelDDR3_1600
from gem5.components.processors.cpu_types import get_cpu_type_from_str
from gem5.components.processors.simple_processor import SimpleProcessor
from gem5.isas import ISA
from gem5.resources.resource import (
    BootloaderResource,
    DiskImageResource,
    KernelResource,
)
from gem5.simulate.exit_event import ExitEvent
from gem5.simulate.exit_event_generators import save_checkpoint_generator
from gem5.simulate.simulator import Simulator
from gem5.utils.requires import requires

DEFAULT_ROOT_MOUNTFROM = "ufs:/dev/ufs/root"
DEFAULT_ROOTDEVNAME = r"ufs:/dev/ufs/root"
RED_HERRING_INTERPRETER = "/red/herring"
VIRTIO_BLK_IRQ = 0x8
VIRTIO_RNG_IRQ = 0x9


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def default_path(*parts: str) -> str:
    return str(default_repo_root().joinpath(*parts))


class FreebsdRiscvBoard(RiscvBoard):
    """RISC-V FreeBSD board with explicit virt-style boot defaults."""

    def __init__(
        self,
        clk_freq,
        processor,
        memory,
        cache_hierarchy,
        root_mountfrom,
        rootdevname,
        serial_device,
        boot_verbose,
    ):
        self._freebsd_root_mountfrom = root_mountfrom
        self._freebsd_rootdevname = rootdevname
        self._freebsd_serial_device = serial_device
        self._freebsd_boot_verbose = boot_verbose
        super().__init__(clk_freq, processor, memory, cache_hierarchy)

    def get_default_kernel_args(self):
        args = [
            f"console={self._freebsd_serial_device}",
            "boot_serial=YES",
            f"vfs.root.mountfrom={self._freebsd_root_mountfrom}",
            f"ROOTDEVNAME={self._freebsd_rootdevname}",
        ]
        if self._freebsd_boot_verbose:
            args.append("boot_verbose=YES")
        return args


def parse_kernel_args(raw_args: str):
    if not raw_args:
        return []
    return [arg for arg in raw_args.split() if arg]


def prepare_kernel_image(kernel_path: str, bootloader_path: str) -> str:
    patched_kernel = Path(m5.options.outdir) / "freebsd-kernel.gem5"
    interp = subprocess.run(
        ["patchelf", "--print-interpreter", kernel_path],
        check=False,
        capture_output=True,
        text=True,
    )
    if interp.returncode != 0:
        return kernel_path

    if interp.stdout.strip() != RED_HERRING_INTERPRETER:
        return kernel_path

    subprocess.run(
        [
            "patchelf",
            "--set-interpreter",
            bootloader_path,
            "--output",
            str(patched_kernel),
            kernel_path,
        ],
        check=True,
    )
    return str(patched_kernel)


def ensure_platform_devices(board: FreebsdRiscvBoard):
    if not hasattr(board.platform, "clint"):
        raise RuntimeError("RISC-V platform is missing CLINT")
    if not hasattr(board.platform, "rtc"):
        raise RuntimeError("RISC-V platform is missing RTC")
    if not hasattr(board.platform, "uart"):
        raise RuntimeError("RISC-V platform is missing UART")
    if not hasattr(board, "disk"):
        raise RuntimeError("RISC-V platform is missing virtio disk")
    if not hasattr(board, "rng"):
        raise RuntimeError("RISC-V platform is missing virtio rng")
    if int(board.disk.interrupt_id) == int(board.rng.interrupt_id):
        raise RuntimeError(
            "virtio disk and rng share the same PLIC interrupt ID "
            f"({int(board.disk.interrupt_id)})"
        )


def build_board(args):
    cpu_type = get_cpu_type_from_str(args.cpu_type)

    cache_hierarchy = PrivateL1PrivateL2WalkCacheHierarchy(
        l1d_size="64KiB",
        l1i_size="16KiB",
        l2_size="256KiB",
    )

    memory = SingleChannelDDR3_1600(size=args.mem_size)
    processor = SimpleProcessor(
        cpu_type=cpu_type, isa=ISA.RISCV, num_cores=args.num_cores
    )

    board = FreebsdRiscvBoard(
        clk_freq=args.sys_clock,
        processor=processor,
        memory=memory,
        cache_hierarchy=cache_hierarchy,
        root_mountfrom=args.root_mountfrom,
        rootdevname=args.rootdevname,
        serial_device=args.serial_device,
        boot_verbose=args.boot_verbose,
    )

    kernel_args = board.get_default_kernel_args()
    kernel_args.extend(parse_kernel_args(args.kernel_args))
    print(f"kernel_args:{kernel_args}")
    kernel_path = prepare_kernel_image(args.kernel, args.bootloader)
    checkpoint = (
        Path(args.restore_checkpoint) if args.restore_checkpoint else None
    )

    board.set_kernel_disk_workload(
        bootloader=BootloaderResource(args.bootloader, architecture=ISA.RISCV),
        kernel=KernelResource(kernel_path, architecture=ISA.RISCV),
        disk_image=DiskImageResource(args.disk_image),
        readfile=args.readfile or None,
        kernel_args=kernel_args,
        exit_on_work_items=False,
        checkpoint=checkpoint,
    )
    board.disk.interrupt_id = VIRTIO_BLK_IRQ
    board.rng.interrupt_id = VIRTIO_RNG_IRQ
    ensure_platform_devices(board)
    board.platform.terminal.outfile = "stdoutput"
    return board


def main():
    requires(isa_required=ISA.RISCV)

    parser = argparse.ArgumentParser(
        description="Boot FreeBSD on gem5 RISC-V with OpenSBI, CLINT/RTC/UART, and a virtio root disk."
    )
    parser.add_argument(
        "--bootloader",
        default=default_path(
            "build",
            "opensbi",
            "platform",
            "generic",
            "firmware",
            "fw_jump.elf",
        ),
        help="Path to the OpenSBI fw_jump bootloader",
    )
    parser.add_argument(
        "--kernel",
        default=default_path(
            "rootfs", "freebsd_sysroot", "boot", "kernel", "kernel"
        ),
        help="Path to the FreeBSD kernel image",
    )
    parser.add_argument(
        "--disk-image",
        default=default_path("rootfs", "freebsd_sysroot.img"),
        help="Path to the FreeBSD disk image",
    )
    parser.add_argument(
        "--cpu-type",
        choices=("atomic", "timing", "minor", "o3"),
        default="timing",
        help="CPU model to use",
    )
    parser.add_argument(
        "--num-cores", type=int, default=1, help="Number of RISC-V cores"
    )
    parser.add_argument(
        "--sys-clock", default="1GHz", help="Top-level system clock"
    )
    parser.add_argument(
        "--mem-size", default="2GiB", help="Physical memory size"
    )
    parser.add_argument(
        "--serial-device",
        default="ttyS0",
        help="FreeBSD serial console device name",
    )
    parser.add_argument(
        "--root-mountfrom",
        default=DEFAULT_ROOT_MOUNTFROM,
        help="FreeBSD vfs.root.mountfrom/rootdev value",
    )
    parser.add_argument(
        "--rootdevname",
        default=DEFAULT_ROOTDEVNAME,
        help=(
            "FreeBSD ROOTDEVNAME value. Use a literal \\\\n between candidates, "
            "e.g. 'ufs:/dev/ufs/root\\\\nufs:/dev/vtbd0'"
        ),
    )
    parser.add_argument(
        "--boot-verbose",
        action="store_true",
        help="Enable FreeBSD verbose boot messages",
    )
    parser.add_argument(
        "--kernel-args",
        default="",
        help="Extra FreeBSD bootargs/env assignments appended after the defaults",
    )
    parser.add_argument(
        "--max-ticks",
        type=int,
        default=0,
        help="Simulation ticks to run; 0 means no limit",
    )
    parser.add_argument(
        "--checkpoint-dir",
        default="",
        help="Directory where guest-triggered checkpoints are saved",
    )
    parser.add_argument(
        "--restore-checkpoint",
        default="",
        help="Restore simulation state from the given checkpoint directory",
    )
    parser.add_argument(
        "--readfile",
        default="",
        help="Host-side script path exposed through m5 readfile",
    )
    args = parser.parse_args()

    for path_name in (args.bootloader, args.kernel, args.disk_image):
        if not Path(path_name).is_file():
            raise FileNotFoundError(f"required file not found: {path_name}")
    if args.readfile and not Path(args.readfile).is_file():
        raise FileNotFoundError(f"readfile not found: {args.readfile}")
    if args.restore_checkpoint and not Path(args.restore_checkpoint).is_dir():
        raise FileNotFoundError(
            f"checkpoint directory not found: {args.restore_checkpoint}"
        )

    board = build_board(args)
    on_exit_event = None
    if args.checkpoint_dir:
        on_exit_event = {
            ExitEvent.CHECKPOINT: save_checkpoint_generator(
                Path(args.checkpoint_dir)
            )
        }
    simulator = Simulator(
        board=board,
        on_exit_event=on_exit_event,
        max_ticks=m5.MaxTick if args.max_ticks == 0 else args.max_ticks,
    )

    print(
        "Configured devices: CLINT @ 0x{:x}, UART @ 0x{:x}, virtio-blk @ 0x{:x} irq {}, virtio-rng @ 0x{:x} irq {}".format(
            int(board.platform.clint.pio_addr),
            int(board.platform.uart.pio_addr),
            int(board.disk.pio_addr),
            int(board.disk.interrupt_id),
            int(board.rng.pio_addr),
            int(board.rng.interrupt_id),
        )
    )
    print(
        "FreeBSD root selection: rootdev={}, ROOTDEVNAME={}".format(
            args.root_mountfrom, args.rootdevname
        )
    )
    if args.restore_checkpoint:
        print(f"Restoring FreeBSD checkpoint from {args.restore_checkpoint}")
    if args.checkpoint_dir:
        print(
            "Guest-triggered checkpoints will be saved under "
            f"{args.checkpoint_dir}"
        )
    if args.readfile:
        print(f"Guest readfile source: {args.readfile}")
    print("Beginning FreeBSD boot simulation!")
    simulator.run()
    print(
        "Simulation exiting @ tick {} because {}".format(
            simulator.get_current_tick(),
            simulator.get_last_exit_event_cause(),
        )
    )


if __name__ in ("__m5_main__", "__main__"):
    main()

#
# This file is part of LiteX.
#
# Copyright (c) 2018-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2019-2020 David Shah <dave@ds0.me>
# Copyright (c) 2018 William D. Jones <thor0505@comcast.net>
# SPDX-License-Identifier: BSD-2-Clause

import os
import subprocess
import sys
from shutil import which

from migen.fhdl.structure import _Fragment

from litex.build.generic_platform import *
from litex.build import tools
from litex.build.lattice import common
from litex.build.lattice.radiant import _format_constraint, _format_ldc, _build_pdc

import math

# Yosys/Nextpnr Helpers/Templates ------------------------------------------------------------------

_yosys_template = [
    "verilog_defaults -push",
    "verilog_defaults -add -defer",
    "{read_files}",
    "verilog_defaults -pop",
    "attrmap -tocase keep -imap keep=\"true\" keep=1 -imap keep=\"false\" keep=0 -remove keep=0",
    "synth_nexus -flatten {nwl} {abc} -json {build_name}.json -top {build_name}",
]

def _yosys_import_sources(platform):
    includes = ""
    reads = []
    for path in platform.verilog_include_paths:
        includes += " -I" + path
    for filename, language, library in platform.sources:
        # yosys has no such function read_systemverilog
        if language == "systemverilog":
            language = "verilog -sv"
        reads.append("read_{}{} {}".format(
            language, includes, filename))
    return "\n".join(reads)

def _build_yosys(template, platform, nowidelut, abc9, build_name):
    ys = []
    for l in template:
        ys.append(l.format(
            build_name = build_name,
            nwl        = "-nowidelut" if nowidelut else "",
            abc        = "-abc9" if abc9 else "",
            read_files = _yosys_import_sources(platform)
        ))
    tools.write_to_file(build_name + ".ys", "\n".join(ys))

# Script -------------------------------------------------------------------------------------------

_build_template = [
    "yosys -l {build_name}.rpt {build_name}.ys",
    "nextpnr-nexus --json {build_name}.json --pdc {build_name}.pdc --fasm {build_name}.fasm \
    --device {device} {timefailarg} {ignoreloops} --seed {seed}",
    "prjoxide pack {build_name}.fasm {build_name}.bit"
]

def _build_script(source, build_template, build_name, device, timingstrict, ignoreloops, seed):
    if sys.platform in ("win32", "cygwin"):
        script_ext = ".bat"
        script_contents = "@echo off\nrem Autogenerated by LiteX / git: " + tools.get_litex_git_revision() + "\n\n"
        fail_stmt = " || exit /b"
    else:
        script_ext = ".sh"
        script_contents = "# Autogenerated by LiteX / git: " + tools.get_litex_git_revision() + "\nset -e\n"
        fail_stmt = ""

    for s in build_template:
        s_fail = s + "{fail_stmt}\n"  # Required so Windows scripts fail early.
        script_contents += s_fail.format(
            build_name      = build_name,
            device          = device,
            timefailarg     = "--timing-allow-fail" if not timingstrict else "",
            ignoreloops     = "--ignore-loops" if ignoreloops else "",
            fail_stmt       = fail_stmt,
            seed            = seed,
        )
    script_file = "build_" + build_name + script_ext
    tools.write_to_file(script_file, script_contents, force_unix=False)

    return script_file

def _run_script(script):
    if sys.platform in ("win32", "cygwin"):
        shell = ["cmd", "/c"]
    else:
        shell = ["bash"]

    if which("yosys") is None or which("nextpnr-nexus") is None:
        msg = "Unable to find Yosys/Nextpnr toolchain, please:\n"
        msg += "- Add Yosys/Nextpnr toolchain to your $PATH."
        raise OSError(msg)

    if subprocess.call(shell + [script]) != 0:
        raise OSError("Error occured during Yosys/Nextpnr's script execution.")

# LatticeOxideToolchain --------------------------------------------------------------------------

class LatticeOxideToolchain:
    attr_translate = {
        "keep": ("keep", "true"),
    }

    special_overrides = common.lattice_NX_special_overrides_for_oxide

    def __init__(self):
        self.yosys_template   = _yosys_template
        self.build_template   = _build_template
        self.clocks      = {}
        self.false_paths = set() # FIXME: use it

    def build(self, platform, fragment,
        build_dir      = "build",
        build_name     = "top",
        run            = True,
        nowidelut      = False,
        abc9           = False,
        timingstrict   = False,
        ignoreloops    = False,
        seed           = 1,
        es_device      = False,
        **kwargs):

        # Create build directory
        os.makedirs(build_dir, exist_ok=True)
        cwd = os.getcwd()
        os.chdir(build_dir)

        # Finalize design
        if not isinstance(fragment, _Fragment):
            fragment = fragment.get_fragment()
        platform.finalize(fragment)

        # Generate verilog
        v_output = platform.get_verilog(fragment, name=build_name, **kwargs)
        named_sc, named_pc = platform.resolve_signals(v_output.ns)
        top_file = build_name + ".v"
        v_output.write(top_file)
        platform.add_source(top_file)

        # Generate design constraints file (.pdc)
        _build_pdc(named_sc, named_pc, self.clocks, v_output.ns, build_name)

        # Generate Yosys script
        _build_yosys(self.yosys_template, platform, nowidelut, abc9, build_name)

        # N.B. Radiant does not allow a choice between ES1/production, this is determined
        # solely by the installed Radiant version. nextpnr/oxide supports both, so we
        # must choose what we are dealing with
        device = platform.device
        if es_device:
            device += "ES"

        # Generate build script
        script = _build_script(False, self.build_template, build_name, device,
                               timingstrict, ignoreloops, seed)
        # Run
        if run:
            _run_script(script)

        os.chdir(cwd)

        return v_output.ns

    # N.B. these are currently ignored, but will be supported very soon
    def add_period_constraint(self, platform, clk, period):
        clk.attr.add("keep")
        period = math.floor(period*1e3)/1e3 # round to lowest picosecond
        if clk in self.clocks:
            if period != self.clocks[clk]:
                raise ValueError("Clock already constrained to {:.2f}ns, new constraint to {:.2f}ns"
                    .format(self.clocks[clk], period))
        self.clocks[clk] = period

    def add_false_path_constraint(self, platform, from_, to):
        from_.attr.add("keep")
        to.attr.add("keep")
        if (to, from_) not in self.false_paths:
            self.false_paths.add((from_, to))

def oxide_args(parser):
    parser.add_argument("--yosys-nowidelut", action="store_true",
                        help="pass '-nowidelut' to yosys synth_nexus")
    parser.add_argument("--yosys-abc9", action="store_true",
                        help="pass '-abc9' to yosys synth_nexus")
    parser.add_argument("--nextpnr-timingstrict", action="store_true",
                        help="fail if timing not met, i.e., do NOT pass '--timing-allow-fail' to nextpnr")
    parser.add_argument("--nextpnr-ignoreloops", action="store_true",
                        help="ignore combinational loops in timing analysis, i.e. pass '--ignore-loops' to nextpnr")
    parser.add_argument("--nextpnr-seed", default=1, type=int,
                        help="seed to pass to nextpnr")
    parser.add_argument("--nexus-es-device", action="store_true",
                        help="device is a ES1 Nexus part")

def oxide_argdict(args):
    return {
        "nowidelut":    args.yosys_nowidelut,
        "abc9":         args.yosys_abc9,
        "timingstrict": args.nextpnr_timingstrict,
        "ignoreloops":  args.nextpnr_ignoreloops,
        "seed":         args.nextpnr_seed,
        "es_device":    args.nexus_es_device,
    }

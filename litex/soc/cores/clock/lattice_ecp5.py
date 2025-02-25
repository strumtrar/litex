#
# This file is part of LiteX.
#
# Copyright (c) 2018-2021 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2021 George Hilliard <thirtythreeforty@gmail.com>
# SPDX-License-Identifier: BSD-2-Clause

from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer

from litex.soc.cores.clock.common import *

# Lattice / ECP5 -----------------------------------------------------------------------------------

class ECP5PLL(Module):
    nclkouts_max    = 4
    clki_div_range  = (1, 128+1)
    clkfb_div_range = (1, 128+1)
    clko_div_range  = (1, 128+1)
    clki_freq_range = (    8e6,  400e6)
    clko_freq_range = (3.125e6,  400e6)
    vco_freq_range  = (  400e6,  800e6)
    pfd_freq_range  = (   10e6,  400e6)

    def __init__(self):
        self.logger = logging.getLogger("ECP5PLL")
        self.logger.info("Creating ECP5PLL.")
        self.reset      = Signal()
        self.locked     = Signal()
        self.stdby      = Signal()
        self.clkin_freq = None
        self.vcxo_freq  = None
        self.dpa_en     = False
        self.nclkouts   = 0
        self.clkouts    = {}
        self.config     = {}
        self.params     = {}

    def register_clkin(self, clkin, freq):
        (clki_freq_min, clki_freq_max) = self.clki_freq_range
        assert freq >= clki_freq_min
        assert freq <= clki_freq_max
        self.clkin = Signal()
        if isinstance(clkin, (Signal, ClockSignal)):
            self.comb += self.clkin.eq(clkin)
        else:
            raise ValueError
        self.clkin_freq = freq
        register_clkin_log(self.logger, clkin, freq)

    def create_clkout(self, cd, freq, phase=0, margin=1e-2, with_reset=True, uses_dpa=True):
        (clko_freq_min, clko_freq_max) = self.clko_freq_range
        assert freq >= clko_freq_min
        assert freq <= clko_freq_max
        assert self.nclkouts < self.nclkouts_max
        clkout = Signal()
        self.clkouts[self.nclkouts] = (clkout, freq, phase, margin, uses_dpa)
        if with_reset:
            self.specials += AsyncResetSynchronizer(cd, ~self.locked)
        self.comb += cd.clk.eq(clkout)
        create_clkout_log(self.logger, cd.name, freq, margin, self.nclkouts)
        self.nclkouts += 1

    def compute_config(self):
        config = {}
        # Iterate on CLKI dividers...
        for clki_div in range(*self.clki_div_range):
            # Check if in PFD range.
            (pfd_freq_min, pfd_freq_max) = self.pfd_freq_range
            if not (pfd_freq_min <= self.clkin_freq/clki_div <= pfd_freq_max):
                continue
            config["clki_div"] = clki_div
            # Iterate on CLKO dividers... (to get us in VCO range)
            for clkofb_div in range(*self.clko_div_range):
                # Iterate on CLKFB dividers...
                for clkfb_div in range(*self.clkfb_div_range):
                    vco_freq = (self.clkin_freq/clki_div)*clkfb_div*clkofb_div
                    (vco_freq_min, vco_freq_max) = self.vco_freq_range
                    all_valid = True
                    # If in VCO range, find dividers for all outputs.
                    if vco_freq_min <= vco_freq <= vco_freq_max:
                        config["clkfb"] = None
                        for n, (clk, f, p, m, dpa) in sorted(self.clkouts.items()):
                            valid = False
                            for d in range(*self.clko_div_range):
                                clk_freq = vco_freq/d
                                # If output is valid, save config.
                                if abs(clk_freq - f) <= f*m:
                                    config["clko{}_freq".format(n)]  = clk_freq
                                    config["clko{}_div".format(n)]   = d
                                    config["clko{}_phase".format(n)] = p
                                    valid = True
                                    # Check if ouptut can be used as feedback, if so use it.
                                    # (We cannot use clocks with dynamic phase adjustment enabled)
                                    if (d == clkofb_div) and (not (dpa and self.dpa_en)):
                                        config["clkfb"] = n
                                    break
                            if not valid:
                                all_valid = False
                    else:
                        all_valid = False
                    if all_valid:
                        # If no output suitable for feedback, create a new output for it.
                        if config["clkfb"] is None:
                            # We need at least a free output...
                            assert self.nclkouts < self.nclkouts_max
                            config["clkfb"] = self.nclkouts
                            self.clkouts[self.nclkouts] = (Signal(), 0, 0, 0, 0)
                            config[f"clko{self.nclkouts}_div"] = int((vco_freq*clki_div)/(self.clkin_freq*clkfb_div))
                        config["vco"]       = vco_freq
                        config["clkfb_div"] = clkfb_div
                        compute_config_log(self.logger, config)
                        return config
        raise ValueError("No PLL config found")

    def expose_dpa(self):
        self.dpa_en     = True
        self.phase_sel  = Signal(2)
        self.phase_dir  = Signal()
        self.phase_step = Signal()
        self.phase_load = Signal()

        # # #

        self.params.update(
            p_DPHASE_SOURCE = "ENABLED",
            i_PHASESEL0     = self.phase_sel[0],
            i_PHASESEL1     = self.phase_sel[1],
            i_PHASEDIR      = self.phase_dir,
            i_PHASESTEP     = self.phase_step,
            i_PHASELOADREG  = self.phase_load
        )

    def do_finalize(self):
        config = self.compute_config()
        locked = Signal()
        n_to_l = {0: "P", 1: "S", 2: "S2", 3: "S3"}
        self.params.update(
            attr=[
                ("FREQUENCY_PIN_CLKI",     str(self.clkin_freq/1e6)),
                ("ICP_CURRENT",            "6"),
                ("LPF_RESISTOR",          "16"),
                ("MFG_ENABLE_FILTEROPAMP", "1"),
                ("MFG_GMCREF_SEL",         "2")],
            i_RST           = self.reset,
            i_CLKI          = self.clkin,
            i_STDBY         = self.stdby,
            o_LOCK          = locked,
            p_FEEDBK_PATH   = f"INT_O{n_to_l[config['clkfb']]}",
            p_CLKFB_DIV     = config["clkfb_div"],
            p_CLKI_DIV      = config["clki_div"]
        )
        self.comb += self.locked.eq(locked & ~self.reset)
        for n, (clk, f, p, m, dpa) in sorted(self.clkouts.items()):
            div    = config[f"clko{n}_div"]
            cphase = int(p*(div + 1)/360 + div - 1)
            self.params[f"p_CLKO{n_to_l[n]}_ENABLE"] = "ENABLED"
            self.params[f"p_CLKO{n_to_l[n]}_DIV"]    = div
            self.params[f"p_CLKO{n_to_l[n]}_FPHASE"] = 0
            self.params[f"p_CLKO{n_to_l[n]}_CPHASE"] = cphase
            self.params[f"o_CLKO{n_to_l[n]}"]        = clk
        self.specials += Instance("EHXPLLL", **self.params)

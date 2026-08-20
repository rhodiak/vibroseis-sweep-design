#!/usr/bin/env python3
"""
Sweep Design
============
A small desktop tool to define one or more vibroseis-style sweeps, overlay
them, and compare: time-domain signal, instantaneous frequency vs time,
amplitude spectrum, autocorrelation, and autocorrelation envelope.

Run:
    python3 sweep_design.py

Requires: numpy, scipy, matplotlib (tkinter backend), and a display.
See sweep_engine.py for the signal-generation math and its assumptions.

DISPLAY NORMALIZATION: spectrum / autocorrelation / envelope panels are
normalized ONCE using a single reference shared across all sweeps
currently on the plot (not per-trace), so relative amplitude differences
between sweeps (duration, drive level, boost) stay visible and meaningful.
See sweep_engine.py's module docstring for why.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import html
import json
import os
import pathlib
import re
import sys
import tempfile
import textwrap
import time
import webbrowser

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

from sweep_engine import (
    SweepParams, SWEEP_TYPES, TAPER_TYPES,
    generate_sweep, compute_spectrum, compute_autocorrelation, compute_envelope,
    correlate_signals, stack_sweep, describe_params,
    compute_sweep_metrics, METRIC_COLUMNS, metric_values, format_metrics,
    VibratorParams, VIBRATOR_PRESETS, DEFAULT_PRESET, UNIT_SYSTEMS,
    KG_PER_LB, M_PER_IN, force_limits, apply_force_model,
    energy_per_octave, energy_per_octave_db, vibrator_summary,
    describe_vibrator, vibrator_to_dict, vibrator_from_dict, VIB_ATTRS,
    VIB_UNIT_FACTORS, VIB_FILE_KEYS, vib_unit_note,
)
from sweep_manual import render_manual
from sweep_export import (
    SEGY_SAMPLE_FORMATS, SEGY_MAX_SAMPLES, SEGY_SAFE_SAMPLES,
    sweep_type_code, taper_type_code, write_segy, write_ascii, read_segy,
    write_petrel_wavelet, read_petrel_wavelet, ieee_to_ibm, ibm_to_ieee,
)

def _app_dir() -> str:
    """Where the program keeps its state file.

    Running from source that is simply the script's own directory, so a
    project folder stays self-contained: copy or move it and the saved
    settings travel with it.

    Frozen into an executable (PyInstaller and friends) __file__ is useless
    for this. In a one-file build it points inside a temporary extraction
    directory that is DELETED on exit, so settings written there are gone
    before the next launch; in a one-folder build it points into the bundle's
    internal directory, which is the wrong place to write user data and is
    read-only when the program is installed somewhere like Program Files.
    Use the directory holding the executable instead -- the visible folder
    the user unpacked -- and fall back to a per-user config directory when
    that is not writable.
    """
    if getattr(sys, "frozen", False):
        beside_exe = os.path.dirname(os.path.abspath(sys.executable))
        if os.access(beside_exe, os.W_OK):
            return beside_exe
        base = (os.environ.get("APPDATA")
                or os.environ.get("XDG_CONFIG_HOME")
                or os.path.join(os.path.expanduser("~"), ".config"))
        fallback = os.path.join(base, "SweepDesign")
        os.makedirs(fallback, exist_ok=True)
        return fallback
    return os.path.dirname(os.path.abspath(__file__))


def manual_source_path():
    """Where README.md is, or None. Used by the Help button.

    Not _app_dir(): the README is BUNDLED DATA, not user data. A frozen
    build unpacks it into PyInstaller's own directory (sys._MEIPASS), which
    is where the spec's `datas` entry puts it -- that is a different place
    from the folder the settings are written to, and on a one-file build it
    is a temporary directory that only exists while the program runs.
    Falling back to the app directory covers a user who kept the README
    beside the executable, and the script directory covers running from
    source.
    """
    seen = []
    for base in (getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(sys.executable))
                 if getattr(sys, "frozen", False) else None,
                 os.path.dirname(os.path.abspath(__file__))):
        if not base or base in seen:
            continue
        seen.append(base)
        candidate = os.path.join(base, "README.md")
        if os.path.isfile(candidate):
            return candidate
    return None


APP_DIR = _app_dir()
STATE_FILE = os.path.join(APP_DIR, "sweep_design_state.json")
STATE_VERSION = 1

# Saved vibrators live in their OWN file, not in the settings.
#
# The settings file is one session's working state and is rewritten on every
# exit; the vibrator library is a set of real machines that took effort to
# get right (a spec sheet read carefully, a mass velocity derived from pump
# flow and piston area) and must outlive a settings reset, survive being
# copied to another machine, and be readable and hand-editable on its own.
# Two different lifetimes, so two different files.
VIB_LIBRARY_FILE = os.path.join(APP_DIR, "sweep_design_vibrators.json")


def display_path(path: str) -> str:
    """A path fit to be shown on screen or pasted into a bug report.

    An absolute path is somebody's directory layout. In a screenshot of the
    About box, in a status line during a demo, or in a self-test report
    mailed to someone else, it says a great deal about the machine and
    nothing about the program -- and this program is meant to be passed
    around. So: a file in the program's own folder is shown as a bare
    filename, anything else under the user's home as ~/..., and anything
    else at all as a bare filename too.

    That last case is the important one. It looks like a place to fall back
    on the real path, but the paths that land there are the ones worth
    showing least -- a roaming %APPDATA% on a network share, for instance,
    which would print the file server's name into the About box. Nothing
    displayed is ever absolute; app_dir_kind() says which folder in words,
    which is the part a bug report actually needs.
    """
    try:
        full = os.path.abspath(path)
        if os.path.dirname(full) == APP_DIR:
            return os.path.basename(full)
        home = os.path.abspath(os.path.expanduser("~"))
        if full.startswith(home + os.sep):
            return "~" + full[len(home):]
        return os.path.basename(full)
    except (OSError, ValueError):
        return os.path.basename(str(path))


def app_dir_kind() -> str:
    """Which of _app_dir()'s branches was taken, in words not paths.

    This is the part of the location a bug report actually needs -- whether
    settings landed beside the executable or were pushed into the per-user
    fallback because the install folder is read-only. Saying it in words
    answers that without printing anyone's directory tree.
    """
    if not getattr(sys, "frozen", False):
        return "the source folder"
    beside_exe = os.path.dirname(os.path.abspath(sys.executable))
    return ("beside the program" if APP_DIR == beside_exe
            else "per-user config folder (program folder not writable)")
# 2: each entry is written in the units it was entered in and tagged with
#    which those were. 1 wrote everything in raw SI under the attribute
#    names; those files still load, and are rewritten as 2 on the next save.
VIB_LIBRARY_VERSION = 2


def load_vib_library(path: str = None):
    """Saved vibrators as ({name: VibratorParams}, [problems]).

    A bad entry is skipped and reported rather than taken as fatal: one
    mistyped number in a hand-edited file must not cost the user the other
    nine machines in it.

    `path` defaults to VIB_LIBRARY_FILE, but is resolved at call time rather
    than bound as a default argument, so a test can point the pair of these
    functions somewhere harmless. A default argument would capture the real
    path at import and quietly write over the user's own library.
    """
    path = path or VIB_LIBRARY_FILE
    if not os.path.exists(path):
        return {}, []
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{os.path.basename(path)}: {exc}"]
    entries = data.get("vibrators") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}, [f"{os.path.basename(path)}: no 'vibrators' section"]
    lib, problems = {}, []
    for name, entry in entries.items():
        try:
            lib[str(name)] = vibrator_from_dict(str(name), entry)
        except ValueError as exc:
            problems.append(str(exc))
    return lib, problems


def save_vib_library(lib: dict, path: str = None):
    """Write the library out. Raises OSError, which the caller reports."""
    path = path or VIB_LIBRARY_FILE
    # A units legend per system, so the file explains its own numbers to
    # anyone opening it in a text editor without this program to hand.
    payload = {"version": VIB_LIBRARY_VERSION,
               "units_legend": {u: vib_unit_note(u) for u in UNIT_SYSTEMS},
               "vibrators": {name: vibrator_to_dict(vib)
                             for name, vib in sorted(lib.items())}}
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)

# ------------------------------------------------------------------ HiDPI
# Everything this program measures is in pixels at 100 dpi: the margin
# budgets in _build_plot_panel, the table constants below, the initial
# window size. That is the right way to lay out constant-size text, and it
# holds as long as one pixel means one thing. On a display scaled above
# 100% it stops holding, so the whole set gets multiplied by one factor --
# see _detect_ui_scale() for how the factor is found and BASE_DPI for how
# it is applied.
BASE_DPI = 100.0          # the dpi every pixel constant here was tuned at
UI_SCALE_ENV = "SWEEP_DESIGN_SCALE"   # manual override, any platform
UI_SCALE_MAX = 4.0        # beyond this a "scale factor" is a bad reading


def _declare_dpi_aware() -> None:
    """Tell Windows this process draws at the real pixel resolution.

    Without it, a process on a display scaled to 125% or 150% is handed a
    fictitious smaller screen, draws into it, and lets the compositor
    stretch the result -- so every plot line and every axis label comes out
    softened. Declaring awareness turns the blur off; the size that then
    has to be corrected is handled by _detect_ui_scale().

    SYSTEM_DPI_AWARE (1), not PER_MONITOR (2), on purpose: Tk 8.6 does not
    rescale itself when a window is dragged between monitors of different
    scale, so claiming per-monitor awareness would promise something the
    toolkit cannot deliver and would look worse than the compositor's blur.
    No-op off Windows, and on any Windows too old for the call.
    """
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _detect_ui_scale(root) -> float:
    """How many real pixels the display uses per nominal pixel.

    Read from Windows once awareness is declared, because that is where the
    problem is. Deliberately NOT auto-detected elsewhere: an X11 server can
    report any DPI it likes, and honouring that would resize this program on
    machines where nothing was ever wrong. Set SWEEP_DESIGN_SCALE to force
    it anywhere -- '1.5', or '1' to opt out of the correction on Windows.
    """
    override = os.environ.get(UI_SCALE_ENV, "").strip()
    if override:
        try:
            return min(max(float(override), 1.0), UI_SCALE_MAX)
        except ValueError:
            pass
    if sys.platform != "win32":
        return 1.0
    dpi = None
    try:
        import ctypes
        dpi = float(ctypes.windll.user32.GetDpiForSystem())   # Win 10 1607+
    except (AttributeError, OSError, ValueError):
        try:
            dpi = float(root.winfo_fpixels("1i"))
        except Exception:
            dpi = None
    if not dpi or not (48.0 <= dpi <= 96.0 * UI_SCALE_MAX):
        return 1.0
    return max(dpi / 96.0, 1.0)       # 96 dpi is Windows' unscaled 100%


def _apply_tk_scaling(root, scale: float) -> None:
    """Match Tk's own point-to-pixel conversion to the display scale.

    Only ever raises it, and only when Tk has not already worked the scale
    out for itself -- which it usually does on Windows once the process is
    DPI-aware, but not on every Tk build. Setting it unconditionally would
    double-apply the correction on the builds that got it right.
    """
    want = 96.0 * scale / 72.0
    try:
        current = float(root.tk.call("tk", "scaling"))
    except Exception:
        return
    if want > current * 1.05:
        try:
            root.tk.call("tk", "scaling", want)
        except Exception:
            pass

COLOR_CYCLE = [
    "#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e",
    "#17becf", "#e377c2", "#8c564b", "#bcbd22", "#7f7f7f",
]

# Two views share one 2x3 figure, one visible at a time (see _set_view).
# "Sweep design" is the original, dimensionless view: a sweep as a designer
# specifies it. "Field model" is the same sweeps as a particular vibrator can
# actually radiate them, in newtons, under its stroke/flow/hold-down limits.
# Splitting them by view rather than mixing them on one set of axes keeps the
# units unambiguous -- a +/-1 waveform and a 200 kN one do not belong on a
# shared y-axis -- and leaves every existing panel behaving exactly as before.
PANELS = ["signal", "freq", "spec", "corr", "env"]
PANELS_FIELD = ["flimit", "fsig", "fspec", "foct", "fcorr"]
PANEL_TITLES = {
    "signal": "Signal", "freq": "Frequency vs Time", "spec": "Amplitude Spectrum",
    "corr": "Autocorrelation", "env": "Correlation Envelope",
    "flimit": "Vibrator Force Limits", "fsig": "Ground Force",
    "fspec": "Force Spectrum", "foct": "Energy per Octave",
    "fcorr": "Force Autocorrelation",
}
VIEWS = (("sweep", "Sweep design"), ("field", "Field model"))
VIEW_PANELS = {"sweep": PANELS, "field": PANELS_FIELD}

# Metrics table drawn across the bottom of the figure, under the plots. The
# measured numbers used to ride along in the legend, in brackets after each
# sweep's parameter line; that made every legend entry two long lines and, worse,
# left the numbers unaligned -- comparing SLL across three sweeps meant hunting
# for it inside three different sentences. Tabulated, the columns line up and
# can be read down. Like the footer and the key it is a FIGURE artist, so it is
# part of every exported SVG/PNG. It grows downward only: one row per sweep,
# never a second block of columns, so adding sweeps never widens the canvas.
#
# Rows are identified by a colour swatch alone, not by the sweep's name: the
# names are already spelled out in the legend directly above, and printing
# them twice on one figure buys nothing but width. Colour is the link between
# the two, exactly as it is between the legend and the curves.
TABLE_FS = 9.0            # nominal point size; only ever shrinks from here,
TABLE_FS_MIN = 5.0        # and never below this, on a very narrow canvas.
                          # Small, deliberately: this is the last step before
                          # the table runs off the canvas edge, and a cramped
                          # number still reads where a clipped one does not.
                          # It only bites below roughly 800 px of nominal
                          # canvas width -- eight sweeps on a 1080p screen
                          # scaled to 200%, say, where the effective desktop
                          # is 960x540 and everything is cramped anyway.
TABLE_ROW_MULT = 1.6      # row pitch as a multiple of the point size: tight,
                          # but leaving the gridded cells room to breathe
TABLE_PAD_MULT = 0.85     # cell padding either side of the text, in point-size
                          # units -- also sets how far apart the columns sit
TABLE_SWATCH_PX = 26      # colour key: a short line in the sweep's own colour,
                          # the same cue the legend handle gives
TABLE_PAD_TOP_PX = 12     # clearance below the bottom row's x-axis labels
TABLE_PAD_BOT_PX = 8      # clearance above the footer line
TABLE_MAX_W_FRAC = 0.96   # widest the table may get before the font steps down
TABLE_GRID_COLOR = "#dcdcdc"   # inner cell separators: present, not loud
TABLE_RULE_COLOR = "#bdbdbd"   # outer frame and the line under the header

# Metrics key drawn on the canvas itself, top-right of row 1. The column
# headings of that table are not self-explanatory, and an exported PNG/SVG
# travels on its own -- whoever opens it has no About dialog to consult, so the
# key has to be part of the picture. Kept terse: it has to fit a panel the same
# width as the plots beside it.
GLOSSARY_TITLE = "Metrics key (table columns)"
GLOSSARY_ROWS = [
    ("pk",    "acorr. peak, dB rel. strongest"),
    ("SLL",   "1st side lobe, dB below peak"),
    ("P/T",   "deepest trough, dB below peak"),
    ("MLW",   "main-lobe width @ -6 dB, ms"),
    ("ISLR",  "integrated side-lobe ratio, dB"),
    ("T40dB", "ringing: env. above -40 dB, ms"),
    ("decay", "side-lobe fall, dB / 100 ms"),
    ("BW",    "achieved -6 dB band, Hz"),
]
# Whole sentences, not pre-broken lines: each is re-wrapped to the panel's
# actual width on every resize (see _fit_glossary), so a wide window reads as
# prose rather than as stubs broken at fixed points. One entry per sentence,
# so the second always starts on a new line.
GLOSSARY_NOTE = (
    "Lower is better, except pk and BW.",
    "T40dB and decay are measured against each sweep's own peak, "
    "so read them together with pk.",
)
# The key's text is sized to fill its panel rather than set once -- see
# _fit_glossary. The ceiling keeps it from outgrowing the panel titles (13 pt)
# on a large display; the floor is the point where it stops being readable.
GLOSSARY_FS_MAX = 12.0
GLOSSARY_FS_MIN = 5.5

# The field view's top-right panel, in the slot the metrics key occupies in the
# sweep view. It shows the vibrator CURRENTLY set on the Vibrator tab -- not one
# per sweep on the plot, which would not fit -- so editing a field updates it
# live, and the derived numbers (usable force, the knee frequencies) are worked
# out in front of the user instead of asserted. Same reasoning as the key: an
# exported figure has to carry the machine it describes.
VIBINFO_TITLE = "Vibrator (current settings)"
# Read this before quoting a kN figure from the plots. It is on the canvas, not
# only in the README, because the figure is what gets shared around.
# The Vibrator tab's editable fields. SI is what VibratorParams stores and the
# only thing the force laws ever see; these factors convert to whatever the tab
# is currently displaying, and back on the way in. Vibrator spec sheets are
# quoted in field units nearly everywhere Vibroseis is shot, so entering 57,320
# lb of hold-down has to be as direct as entering 26,000 kg -- but a unit
# conversion buried inside the physics is a bug waiting to happen, so it lives
# strictly at the widget edge.
# The conversion factors themselves live in the engine's VIB_UNIT_FACTORS,
# because the library file is written in display units too and the tab and
# the file must not be able to convert differently. Only the labels and the
# tooltips are here.
#   (attribute, SI label, field label, tooltip)
VIB_FIELDS = (
    ("hold_down_kg", "Hold-down weight (kg)",
     "Hold-down weight (lb)",
     "Mass bearing down on the baseplate. Sets the flat force ceiling: peak "
     "ground force above it lifts the plate off the ground (decoupling), so "
     "the usable rating is this weight times the decoupling margin below. "
     "This is the number that separates a heavy vibrator from a light one in "
     "the middle of the band. Take it from the sheet's HOLD-DOWN WEIGHT row, "
     "not its peak force rating -- a maker sizes the hold-down just above the "
     "peak force, so the two rows sit within a few percent of each other and "
     "are easy to confuse, but one is a mass and the other is a force."),
    ("reaction_mass_kg", "Reaction mass (kg)",
     "Reaction mass (lb)",
     "The moving mass whose acceleration generates the force. Appears in both "
     "low-frequency limits, so a heavier reaction mass lowers the frequency "
     "at which full force becomes reachable."),
    ("stroke_pp_m", "Stroke, pk-pk (mm)",
     "Stroke, pk-pk (in)",
     "Peak-to-peak travel available to the reaction mass, as spec sheets "
     "quote it. Sets the +12 dB/octave stroke limit at the bottom of the "
     "band: F = m * (2*pi*f)^2 * stroke/2."),
    ("mass_vel_pk_mps", "Peak mass velocity (m/s)",
     "Peak mass velocity (in/s)",
     "Ceiling on how fast the reaction mass can be driven. Sets the "
     "+6 dB/octave flow limit just above the stroke region: "
     "F = m * 2*pi*f * v. Rarely printed on a data sheet, but derivable "
     "from two values that are: v = (pi/2) * pump flow / mass piston area, "
     "the pi/2 because the mass moves sinusoidally and the pump need only "
     "supply the mean of |A*v(t)|. Watch the units -- 1 LPM is 1.017 in^3/s, "
     "so dividing LPM by in^2 lands 1.7% low and looks right. See README."),
    ("decouple_pct", "Decoupling margin (%)",
     "Decoupling margin (%)",
     "Fraction of the hold-down weight allowed as peak ground force before "
     "the baseplate decouples. 70-80% is normal practice; 100% is the "
     "theoretical limit and nobody runs there."),
)

VIBINFO_NOTE = (
    "Limits modelled: reaction-mass stroke (+12 dB/oct), hydraulic flow "
    "(+6 dB/oct), hold-down weight (flat).",
    "Not modelled: baseplate flexure, ground coupling, distortion, servo "
    "phase error. Absolute kN is order-of-magnitude; the DIFFERENCE between "
    "two vibrators driven alike is the trustworthy part.",
)

APP_VERSION = "1.1"

# Credit / licence line. Put your own name in AUTHOR if you want the credit;
# the licence wording below is the standard public-domain dedication from
# The Unlicense (see the LICENSE file) -- "no licence" as an intention still
# needs an explicit statement, because code with nothing said about it is
# copyrighted by default and nobody else may legally reuse it.
AUTHOR = "a vibroseis enthusiast, for anyone who finds it useful"
LICENSE_ID = "The Unlicense"
LICENSE_STATEMENT = (
    "This is free and unencumbered software released into the public domain. "
    "Anyone is free to copy, modify, publish, use, compile, sell, or "
    "distribute it, for any purpose, commercial or non-commercial, and by "
    "any means. No rights reserved, no strings attached, and no warranty of "
    "any kind -- see the LICENSE file for the full dedication."
)
# Deliberately short: one line that names the licence and disclaims the
# warranty, which is all a plot footer needs to carry. The full dedication
# and the credit live in About (and in the LICENSE file) -- and it doesn't
# say so, because the footer travels on exported images, where a pointer to
# a dialog the reader cannot open is just noise. Anyone running the app
# finds About on their own.
FOOTER_TEXT = (
    f"Sweep Design v{APP_VERSION}  ·  free, public-domain software "
    f"({LICENSE_ID}) — shared freely, no warranty"
)

ABOUT_TEXT = (
    "Sweep Design\n"
    f"Version {APP_VERSION}\n\n"
    "A tool for designing and overlay-comparing land Vibroseis sweep "
    "candidates -- Linear, dB/Octave, dB/Hz, T-power, Random and Pulse "
    "types -- across five panels, with the measured properties of each "
    "correlation wavelet tabulated underneath.\n\n"
    "Two views. Sweep design shows the sweeps as specified: dimensionless, "
    "the reference a correlator would use. Field model shows the same "
    "sweeps in kN, as a real vibrator can actually radiate them under its "
    "stroke, flow and hold-down limits, so a heavy machine and a light one "
    "can be compared directly.\n\n"
    "Click Help for the full manual: what every panel and metric means, "
    "the normalization and stacking conventions, the vibrator force model "
    "and its limits, and the physics notes behind each sweep type.\n\n"
    f"Files: {os.path.basename(STATE_FILE)} (settings) and "
    f"{os.path.basename(VIB_LIBRARY_FILE)} (saved vibrators), kept beside "
    "the program -- or in your per-user config folder, if the program's own "
    "folder is not writable.\n\n"
    f"Licence: {LICENSE_ID}. {LICENSE_STATEMENT}\n\n"
    f"Built and shared freely by {AUTHOR}."
)


# Export formats offered by the Export button: (format, backend module, tip).
#
# The backend module is named because matplotlib loads it LAZILY, inside
# savefig(), so a frozen build's dependency analysis never sees the import and
# leaves it out. That is not hypothetical: v1.0 shipped without
# backend_svg and SVG export -- the default -- failed on Windows with
# "No module named 'matplotlib.backends.backend_svg'" while PNG worked,
# because the Agg canvas that Tk already pulls in writes PNG itself.
# sweep_design.spec lists these as hiddenimports and --selftest exercises
# every one against a real file, so adding a format here is enough to keep
# all three in step.
EXPORT_FORMATS = (
    ("svg", "matplotlib.backends.backend_svg",
     "Vector format -- scales cleanly for print/slides."),
    ("png", "matplotlib.backends.backend_agg",
     "Raster format -- simplest to paste into documents or email directly."),
)

# ---------------------------------------------------------- trace export
# Exporting the SAMPLES, not the picture: the sweep to drive or verify a
# vibrator with, and the correlation wavelet to see what a reflection will
# actually look like after correlation. Everything here is (key, label,
# tooltip) so the dialog, the file headers and --selftest all read the same
# table and cannot describe the file differently from how it was written.
TRACE_CONTENTS = (
    ("sweep", "Sweep (time series)",
     "The sweep itself, sample by sample -- the pilot/reference trace. "
     "Starts at time zero and runs for the sweep length."),
    ("wavelet", "Autocorrelation wavelet",
     "The autocorrelation of the sweep: the wavelet every reflection "
     "arrives as once the record is correlated. This is what the sweep "
     "design actually buys you, so it is usually the trace worth "
     "exporting."),
)

TRACE_SOURCES = (
    ("sweep", "Sweep design (dimensionless)",
     "The sweep as specified, amplitude +/-1 times the drive level -- the "
     "reference signal a correlator uses. Independent of any vibrator."),
    ("field", "Field model (ground force, N)",
     "The same sweep as the vibrator recorded with it can actually "
     "radiate, in newtons, under its stroke, flow and hold-down limits. "
     "The wavelet is then the correlation wavelet of that real force."),
)

TRACE_AMPLITUDES = (
    ("raw", "As computed",
     "Leave the amplitudes as the program computes them, so a longer or "
     "harder-driven sweep really is bigger than a shorter one and the "
     "traces stay comparable. Correlation wavelets are large numbers this "
     "way (amplitude^2 x seconds); that is not a problem for either "
     "format."),
    ("norm", "Normalized to +/-1",
     "Divide the whole exported set by one shared maximum, so the loudest "
     "trace peaks at 1 and the rest keep their true size relative to it. "
     "Convenient scaling without throwing away the comparison -- the same "
     "convention the plot panels use."),
)

# The autocorrelation is an even function, so the negative lags repeat the
# positive ones exactly. Half is the compact form for anything that just
# wants the wavelet shape; full is what a correlation-domain reader expects,
# with zero lag at the middle and carried in the SEG-Y delay field.
TRACE_EXTENTS = (
    ("full", "Full, symmetric",
     "Both sides of zero lag. The trace is centred on the zero-lag peak, "
     "and SEG-Y records the negative half through a negative delay "
     "recording time so the peak still lands at time zero."),
    ("half", "Half, from zero lag",
     "Zero lag onward only. The autocorrelation is symmetric, so this "
     "throws nothing away and halves the file; the first sample is the "
     "peak."),
)

TRACE_FILE_TYPES = (
    ("ascii", "ASCII table (.txt)", ".txt",
     "One plain-text table: a comment block naming every trace, then a "
     "time column and one amplitude column per sweep. Reads with "
     "numpy.loadtxt, awk, a spreadsheet or any text editor."),
    ("segy", "SEG-Y rev 1 (.sgy)", ".sgy",
     "One SEG-Y file, one trace per sweep, big-endian, fixed trace "
     "length. The sweep-description fields in the trace headers (start "
     "and end frequency, sweep length, sweep type, taper lengths and "
     "type) are filled in from the sweep's own parameters."),
    ("petrel", "Petrel ASCII wavelet (.wlt)", ".wlt",
     "The keyword layout Petrel and its relatives import a wavelet from: "
     "WAVELET-NAME, WAVELET-TFS, SAMPLE-RATE, then EOH, a two-column "
     "table and EOD. It holds ONE wavelet per file, so several sweeps "
     "come out as several numbered files rather than as columns."),
)

# Default number of samples in an exported wavelet. 1001 at a 1 ms interval
# is a full second of lag, comfortably wider than any Klauder wavelet worth
# looking at while still being a round number to recognize in a header.
TRACE_WAVELET_SAMPLES = "1001"

def assemble_traces(items, content, amplitude, extent, n_samples):
    """Turn the sweeps on the plot into one rectangular block of samples.

    `items` is one dict per sweep -- label, params, the trace itself, its
    correlation and the lag axis that goes with it, plus the unit strings.
    The GUI picks those out of its result records (design signal or modelled
    ground force); everything from here down is common to both, and to both
    file formats, which is why it lives outside the app class and is what
    --selftest exercises.

    Returns the block plus everything a file header has to state about it:
    the sample interval, the time of the first sample, the per-trace SEG-Y
    header fields, and the divisor if the set was normalized.

    Two rules worth knowing because they show up in the written file:
    traces of different lengths are ZERO-PADDED to the longest rather than
    resampled or refused (SEG-Y requires one length per file, and silence
    after a short sweep ends is the honest continuation); and normalization
    uses ONE divisor for the whole set, never per trace, so the relative
    sizes that the whole program exists to compare survive the export.
    """
    if not items:
        raise ValueError("no sweeps to export")
    dts = sorted({round(it["params"].dt_ms, 9) for it in items})
    if len(dts) > 1:
        raise ValueError(
            "The sweeps on the plot use different sample intervals ("
            + ", ".join(f"{d:g} ms" for d in dts) + "), and one file can "
            "hold only one. Export them separately, or set the same "
            "sample interval on each.")
    dt_ms = dts[0]
    dt_s = dt_ms / 1000.0
    dt_us = int(round(dt_ms * 1000.0))
    if dt_us < 1:
        raise ValueError(f"a {dt_ms:g} ms sample interval is below SEG-Y's "
                          f"1 microsecond resolution")

    notes = []
    if abs(dt_us - dt_ms * 1000.0) > 1e-6:
        notes.append(f"Sample interval {dt_ms:g} ms rounded to {dt_us} us "
                      f"for the SEG-Y header field, which is an integer "
                      f"number of microseconds. The ASCII time column is "
                      f"not rounded.")

    rows, t0_s = [], 0.0
    if content == "sweep":
        raw = [np.asarray(it["trace"], dtype=float) for it in items]
        n = max(len(a) for a in raw)
        if any(len(a) != n for a in raw):
            notes.append(f"Traces of different lengths were zero-padded to "
                          f"the longest ({n} samples).")
        rows = [np.concatenate([a, np.zeros(n - len(a))]) if len(a) < n else a
                for a in raw]
    else:
        half = (extent == "half")
        zeros = [int(np.argmin(np.abs(np.asarray(it["lags"])))) for it in items]
        if n_samples is None:
            widest = max(len(it["corr"]) - z for it, z in zip(items, zeros))
            n = widest if half else 2 * widest - 1
        else:
            n = int(n_samples)
            if not half and n % 2 == 0:
                n += 1
                notes.append(f"A symmetric wavelet needs an odd sample count "
                              f"so the zero-lag peak has a sample of its own; "
                              f"{n_samples} was rounded up to {n}.")
        k = n - 1 if half else (n - 1) // 2
        for it, z in zip(items, zeros):
            ac = np.asarray(it["corr"], dtype=float)
            lo = z if half else z - k
            hi = z + k + 1
            seg = np.zeros(n)
            src_lo, src_hi = max(lo, 0), min(hi, len(ac))
            if src_hi > src_lo:
                seg[src_lo - lo:src_hi - lo] = ac[src_lo:src_hi]
            rows.append(seg)
        t0_s = 0.0 if half else -k * dt_s
        if n_samples is not None and any(
                (len(it["corr"]) - z) < k + 1 for it, z in zip(items, zeros)):
            notes.append("The requested window is wider than some sweeps' "
                          "autocorrelation; those traces are zero-padded, "
                          "which is what the autocorrelation is out there.")

    data = np.vstack(rows)
    scale = 1.0
    if amplitude == "norm":
        peak = float(np.max(np.abs(data))) if data.size else 0.0
        if peak > 0:
            scale = peak
            data = data / peak

    headers = []
    for it in items:
        p = it["params"]
        headers.append({
            # trid 6 is "sweep"; a correlation wavelet is ordinary seismic
            # data with the correlated flag set, which is what corr=2 says.
            "trid": 6 if content == "sweep" else 1,
            "corr": 1 if content == "sweep" else 2,
            "sfs": round(p.f1), "sfe": round(p.f2),
            "slen": round(p.length * 1000.0),
            "styp": sweep_type_code(p.sweep_type),
            "stas": round(p.taper_start_len * 1000.0),
            "stae": round(p.taper_end_len * 1000.0),
            "tatyp": taper_type_code(p.taper_type),
            # 8 = newton. Only true of the force trace itself: the design
            # sweep is dimensionless and a correlation wavelet is in
            # amplitude^2 x seconds, neither of which the standard lists.
            "tvmu": it.get("unit_code", 0) if content == "sweep" else 0,
        })

    return {
        "data": data, "dt_s": dt_s, "dt_us": dt_us, "dt_ms": dt_ms,
        "t0_s": t0_s, "n_samples": data.shape[1], "n_traces": data.shape[0],
        "labels": [it["label"] for it in items],
        "units": items[0]["units_corr" if content == "wavelet" else "units"],
        "scale": scale, "headers": headers, "notes": notes,
    }


ABOUT_WRAP = "6i"        # About dialog text width -- 2x Tk's 3i default
TOOLTIP_FONT_SIZE = 16   # help balloons: sized for comfortable reading
TOOLTIP_WRAP_PX = 520    # wider wrap, so bigger text doesn't become a
                          # tall narrow column of two words per line


class ToolTip:
    """Minimal hover tooltip ('help balloon') for any tk/ttk widget."""

    def __init__(self, widget, text, wraplength=TOOLTIP_WRAP_PX):
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.tipwindow = None
        widget.bind("<Enter>", self._show)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _show(self, event=None):
        if self.tipwindow or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        tw = self.tipwindow = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try:
            tw.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(tw, text=self.text, justify=tk.LEFT, background="#ffffe0",
                          relief=tk.SOLID, borderwidth=1, wraplength=self.wraplength,
                          font=("", TOOLTIP_FONT_SIZE))
        label.pack(ipadx=5, ipady=3)

    def _hide(self, event=None):
        if self.tipwindow:
            self.tipwindow.destroy()
            self.tipwindow = None


class SweepDesignApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Sweep Design")

        # Display scale, resolved once: the figure dpi, every pixel budget
        # and the window size below are all multiples of it. 1.0 on a normal
        # display, so nothing moves there.
        self.ui_scale = _detect_ui_scale(root)
        _apply_tk_scaling(root, self.ui_scale)

        # Taller than the width suggests: the metrics table under the plots
        # needs a strip of its own, and it is meant to come out of ADDED
        # height rather than out of the panels. Width is unchanged -- the
        # table only ever grows downward.
        #
        # Clamped to the screen, because the nominal size does not fit
        # everywhere: 1060 px of window plus a taskbar already overflows a
        # 1080p display at 100%, and the display scale multiplies it -- at
        # 125% on a 1080p laptop, a common Windows default, the raw request
        # is 1937x1325. An oversized window opens with its lower edge, and
        # so the whole metrics table, off the bottom of the screen. Every
        # margin is solved against the canvas actually granted (the layout
        # is exercised down to 800x620), so shrinking here costs nothing but
        # plot area.
        want_w, want_h = self.px(1550), self.px(1060)
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        avail_w = screen_w - self.px(40)
        avail_h = screen_h - self.px(90)                    # taskbar + title bar
        # The floor keeps a bad screen reading from opening a useless sliver,
        # but it is capped by the screen in turn: a minimum must never win
        # against the physical display, or the clamp above is undone.
        win_w = min(int(max(self.px(700), min(want_w, avail_w))), screen_w)
        win_h = min(int(max(self.px(560), min(want_h, avail_h))), screen_h)
        self.root.geometry(f"{win_w}x{win_h}")

        self.results = []      # list of dicts: raw generation + analysis
        self.next_color_idx = 0
        self.sweep_counter = 0
        self.range_vars = {}   # panel -> {'xmin':var,'xmax':var,'ymin':var,'ymax':var}
        self._last_auto_label = f"Sweep {self.sweep_counter + 1}"  # matches the
                                        # entry's initial default text, so the
                                        # very first auto-refresh recognizes it
        self._suspend_auto_label = False  # guards against label churn while
                                           # bulk-applying loaded state
        self._state_vars = {}  # populated in _build_sweep_tab: name -> tk var
        # Which of the two panel sets the figure is showing. Declared before
        # any UI is built: both the view switch and the plot panel read it.
        self.view = tk.StringVar(value="sweep")

        self._build_layout()
        self._build_param_panel()
        self._build_plot_panel()
        self._redraw()
        self._load_state(notify=False)   # silently restore last session, if any

    def px(self, n: float) -> float:
        """A TK length tuned at 100 dpi, in this display's real pixels.

        For widget geometry only -- the window size, a wraplength. Anything
        drawn on the figure must use fpx() instead, which follows the figure's
        own dpi rather than this independently detected factor.
        """
        return n * self.ui_scale

    def fpx(self, n: float) -> float:
        """A FIGURE length tuned at BASE_DPI, in the figure's device pixels.

        Every hard-coded pixel budget for text furniture goes through here.
        Derived from the live figure dpi, not from ui_scale, and that is the
        whole point: matplotlib's Tk backend sets the dpi itself from the
        display scale, so reading it back is self-consistent by construction.
        Scaling by a separately detected factor instead is how v1.0.2 came to
        apply the display scale twice. Text is sized in points and so already
        follows the dpi; this keeps the room reserved for it in step.
        """
        return n * self.fig.dpi / BASE_DPI

    def _scale_margins(self):
        """Refresh the pixel budgets against the figure's current dpi.

        Called on every layout pass, not once at construction: the Tk backend
        sets the figure dpi from the display scale when the canvas is mapped,
        which happens after these are first needed, and can change it again
        later. Cheap enough to redo, and wrong if it is not."""
        n = self._nominal_margins
        self._m_left_px = self.fpx(n["left"])
        self._m_right_px = self.fpx(n["right"])
        self._m_bottom_px = self.fpx(n["bottom"])
        self._m_footer_px = self.fpx(n["footer"])
        self._m_title_px = self.fpx(n["title"])
        self._gap_w_px = self.fpx(n["gap_w"])
        self._gap_h_px = self.fpx(n["gap_h"])
        self._legend_row_px = self.fpx(n["legend_row"])
        self._legend_pad_px = self.fpx(n["legend_pad"])

    # ------------------------------------------------------------------ UI
    def _build_layout(self):
        self.left = ttk.Frame(self.root, padding=8)
        self.left.pack(side=tk.LEFT, fill=tk.Y)
        self.right = ttk.Frame(self.root, padding=4)
        self.right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

    def _labeled_row(self, parent, label, widget_factory, default, row):
        lbl = ttk.Label(parent, text=label)
        lbl.grid(row=row, column=0, sticky="w", pady=2)
        w = widget_factory(parent, default)
        w.grid(row=row, column=1, sticky="ew", pady=2)
        w.row_label = lbl   # stash a direct reference -- avoids relying on
                             # grid_info()/grid_slaves() after grid_remove()
        return w

    def _entry(self, parent, default):
        v = tk.StringVar(value=str(default))
        e = ttk.Entry(parent, textvariable=v, width=12)
        e.var = v
        return e

    def _combo(self, parent, values, default):
        v = tk.StringVar(value=default)
        c = ttk.Combobox(parent, textvariable=v, values=values, width=10, state="readonly")
        c.var = v
        return c

    def _tip(self, widget, text):
        # The balloon's font is in points and rides on Tk's scaling, so its
        # wrap width -- which is in pixels -- has to be scaled to match, or a
        # scaled display gets the tall narrow column this width exists to
        # avoid. (ABOUT_WRAP is in inches and needs no such help.)
        ToolTip(widget, text, wraplength=int(self.px(TOOLTIP_WRAP_PX)))
        return widget

    def _build_param_panel(self):
        footer = ttk.Frame(self.left, padding=(0, 8, 0, 0))
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Separator(footer, orient="horizontal").pack(fill=tk.X, pady=(0, 6))

        state_row = ttk.Frame(footer)
        state_row.pack(fill=tk.X, pady=(0, 4))
        save_btn = ttk.Button(state_row, text="Save settings", command=self._save_state)
        save_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        load_btn = ttk.Button(state_row, text="Load settings",
                               command=lambda: self._load_state(notify=True))
        load_btn.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self._tip(save_btn, "Save the current sweep parameters and axis-range/"
                             "zoom settings to disk now. This also happens "
                             "automatically on Exit. File: "
                             f"{display_path(STATE_FILE)}")
        self._tip(load_btn, "Reload the most recently saved sweep parameters "
                             "and axis-range/zoom settings, discarding any "
                             "unsaved edits on the Sweep and Zoom tabs.")

        btn_row = ttk.Frame(footer)
        btn_row.pack(fill=tk.X)
        help_btn = ttk.Button(btn_row, text="Help", command=self._show_manual)
        help_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        about_btn = ttk.Button(btn_row, text="About", command=self._show_about)
        about_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        exit_btn = ttk.Button(btn_row, text="Exit", command=self._on_exit)
        exit_btn.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self._tip(help_btn, "Open the full manual in your web browser: every "
                             "panel and metric explained, the normalization "
                             "and stacking conventions, the vibrator force "
                             "model and what it does not model, and the "
                             "physics notes for each sweep type. Built from "
                             "the README bundled with this copy, so it always "
                             "matches the version you are running. To keep a "
                             "copy on paper or as a PDF, print it from the "
                             "browser -- the page has a print layout.")
        self._tip(about_btn, "About this application: what it is, which "
                              "version, where it keeps its files, and the "
                              "licence.")
        self._tip(exit_btn, "Close Sweep Design. "
                             "Auto-saves settings first; asks for "
                             "confirmation if you have unexported sweeps "
                             "on the plot.")

        # View switch, above the tabs rather than inside one: it governs the
        # whole figure, not the parameters of any single tab, and both tabs
        # stay meaningful in either view.
        view_row = ttk.Frame(self.left)
        view_row.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        ttk.Label(view_row, text="View", font=("", 10, "bold")).pack(
            side=tk.LEFT, padx=(0, 8))
        for value, text in VIEWS:
            rb = ttk.Radiobutton(view_row, text=text, variable=self.view,
                                  value=value, command=self._on_view_change)
            rb.pack(side=tk.LEFT, padx=(0, 6))
            self._tip(rb,
                       "Sweep design: the sweep as specified -- dimensionless, "
                       "the reference signal a correlator would use. Field "
                       "model: the same sweeps as the vibrator recorded with "
                       "each one can actually radiate them, in kN, under its "
                       "stroke, flow and hold-down limits. Both views draw the "
                       "same sweep set; switching does not regenerate anything.")

        self.notebook = ttk.Notebook(self.left)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        tab_sweep = ttk.Frame(self.notebook, padding=6)
        tab_vib = ttk.Frame(self.notebook, padding=6)
        tab_zoom = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(tab_sweep, text="Sweep")
        self.notebook.add(tab_vib, text="Vibrator")
        self.notebook.add(tab_zoom, text="Axis ranges / zoom")

        self._build_sweep_tab(tab_sweep)
        self._build_vibrator_tab(tab_vib)
        self._build_zoom_tab(tab_zoom)

    def _diagnostics(self) -> str:
        """The measured state of this session, for a bug report.

        Display scaling is the reason this exists. Whether the correction is
        working is not something you can judge from a screenshot -- v1.0.2
        applied the scale twice and looked merely 'cramped' -- so the numbers
        that decide it are put where a user can read them back. `figure`
        below is the one that matters: it is the dpi matplotlib's Tk backend
        actually settled on, and every layout budget is derived from it.
        """
        try:
            tk_scaling = float(self.root.tk.call("tk", "scaling"))
        except Exception:
            tk_scaling = float("nan")
        canvas_px = ""
        try:
            w = self.fig.get_figwidth() * self.fig.dpi
            h = self.fig.get_figheight() * self.fig.dpi
            canvas_px = f", canvas {w:.0f}x{h:.0f} px"
        except Exception:
            pass
        return (
            f"Sweep Design {APP_VERSION} on {sys.platform}"
            f"{' (packaged)' if getattr(sys, 'frozen', False) else ' (source)'}\n"
            f"Display: {self.root.winfo_screenwidth()}x"
            f"{self.root.winfo_screenheight()} px, "
            f"window {self.root.winfo_width()}x{self.root.winfo_height()} px\n"
            f"Scale: detected {self.ui_scale:g}x, Tk {tk_scaling:.3f}, "
            f"figure {self.fig.dpi:g} dpi = {self.fig.dpi / BASE_DPI:g}x nominal"
            f"{canvas_px}\n"
            f"Files: {display_path(STATE_FILE)}, "
            f"{display_path(VIB_LIBRARY_FILE)} "
            f"({len(getattr(self, 'vib_library', {}))} saved), in "
            f"{app_dir_kind()}"
        )

    def _show_manual(self):
        """Render the bundled README to HTML and open it in the browser."""
        src = manual_source_path()
        if src is None:
            messagebox.showerror(
                "Manual not found",
                "README.md was not found next to the program.\n\n"
                "It is normally bundled with the app; if this is a source "
                "checkout, the file should sit beside sweep_design.py.")
            return
        try:
            with open(src, encoding="utf-8") as f:
                md = f.read()
            html_text = render_manual(
                md, f"Sweep Design {APP_VERSION} - Manual",
                f"Version {APP_VERSION}. Generated from the README bundled "
                f"with this copy. Print this page to save it as a PDF.")
            # A stable filename rather than a fresh temp directory each
            # time: clicking Help twice should refresh one browser tab, not
            # scatter a new copy through the temp directory on every click.
            out = os.path.join(tempfile.gettempdir(),
                                f"SweepDesign-{APP_VERSION}-manual.html")
            with open(out, "w", encoding="utf-8") as f:
                f.write(html_text)
        except OSError as exc:
            messagebox.showerror("Could not open the manual", str(exc))
            return
        webbrowser.open(pathlib.Path(out).as_uri())
        self.status.config(
            text=f"Manual opened in your browser ({display_path(out)}).")

    def _show_about(self):
        # Tk's message dialog wraps its text at 3 inches, which turns this
        # much About text into a tall narrow column. Widen it to 6i for
        # this dialog only, then restore the default so error and confirm
        # popups (which are one or two short sentences) keep their normal
        # shape. The option database is global, hence the restore.
        self.root.option_add("*Dialog.msg.wrapLength", ABOUT_WRAP)
        try:
            messagebox.showinfo(
                "About Sweep Design",
                f"{ABOUT_TEXT}\n\n----\n{self._diagnostics()}")
        finally:
            self.root.option_add("*Dialog.msg.wrapLength", "3i")

    def _on_exit(self):
        if self.results:
            if not messagebox.askokcancel(
                    "Exit",
                    "Exit Sweep Design?\n\n"
                    "Any sweeps on the plot that you haven't exported "
                    "(figure or traces) will be lost. Sweep parameters and "
                    "axis-range/zoom settings will be saved for next time."):
                return
        self._save_state(notify=False)
        self.root.destroy()

    # ------------------------------------------------------------ state I/O
    def _collect_state(self):
        sweep = {}
        for key, var in self._state_vars.items():
            try:
                sweep[key] = var.get()
            except (tk.TclError, ValueError):
                pass
        axis_ranges = {
            panel: {field: var.get() for field, var in fields.items()}
            for panel, fields in self.range_vars.items()
        }
        return {"version": STATE_VERSION, "sweep": sweep, "axis_ranges": axis_ranges}

    def _apply_state(self, state):
        self._suspend_auto_label = True
        try:
            sweep = state.get("sweep", {})
            for key, val in sweep.items():
                var = self._state_vars.get(key)
                if var is None:
                    continue
                try:
                    var.set(val)
                except (tk.TclError, ValueError):
                    pass
            axis_ranges = state.get("axis_ranges", {})
            for panel, fields in axis_ranges.items():
                if panel not in self.range_vars:
                    continue
                for field, val in fields.items():
                    if field in self.range_vars[panel]:
                        self.range_vars[panel][field].set(val)
        finally:
            self._suspend_auto_label = False

        # Comboboxes don't fire <<ComboboxSelected>> from var.set(), and the
        # Nyquist label needs a manual refresh too -- update both explicitly.
        self._on_type_change()
        self._update_nyquist_label()
        # The vibrator fields were saved in whatever units were in use at the
        # time and restored verbatim, so only the LABELS need to catch up --
        # running the conversion here would apply the factor a second time.
        self._sync_vib_labels()
        self._refresh_vib_readout()
        self._refresh_vibinfo()
        self._set_view(self.view.get())
        # Treat the restored label as the current "auto" baseline, so future
        # field edits keep it live-updating just like a freshly typed value.
        # If it looks auto-generated (starts with a sweep type name) rather
        # than hand-written, regenerate it outright -- otherwise a label
        # saved by an older version keeps its old wording until the next
        # edit, and gets used verbatim for the next sweep added.
        restored = self.e_label.var.get()
        looks_auto = ("Hz" in restored
                       and any(restored.startswith(t + ",") for t in SWEEP_TYPES))
        if looks_auto:
            restored = self._try_auto_label()
            self.e_label.var.set(restored)
        self._last_auto_label = restored
        self._redraw()  # apply restored axis ranges to the plot immediately

    def _save_state(self, notify=True):
        try:
            data = self._collect_state()
            with open(STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
            if notify:
                self.status.config(
                    text=f"Settings saved to {display_path(STATE_FILE)}")
        except OSError as exc:
            if notify:
                messagebox.showerror("Save failed", str(exc))

    def _load_state(self, notify=True):
        if not os.path.exists(STATE_FILE):
            if notify:
                messagebox.showinfo("No saved settings",
                                     "No saved settings file found at "
                                     f"{display_path(STATE_FILE)}.")
            return
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            self._apply_state(data)
            if notify:
                self.status.config(
                    text=f"Settings loaded from {display_path(STATE_FILE)}")
        except (OSError, json.JSONDecodeError) as exc:
            if notify:
                messagebox.showerror("Load failed", str(exc))


    def _build_sweep_tab(self, p):
        p.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(p, text="Sweep Definition", font=("", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6)); row += 1

        self.e_label = self._labeled_row(p, "Label", self._entry,
                                          f"Sweep {self.sweep_counter + 1}", row); row += 1
        self._tip(self.e_label, "Shown in the plot legend. Auto-fills with the "
                                 "full parameter set from the fields below "
                                 "(e.g. 'Linear, 6-96 Hz, 12 s, tapers "
                                 "250/250 ms cosine, 100% DL, 1 ms') and stays "
                                 "live-updated until you type something "
                                 "custom over it.")
        self.c_type = self._labeled_row(p, "Type", lambda pp, d: self._combo(pp, SWEEP_TYPES, d),
                                         "Linear", row); row += 1
        self.c_type.bind("<<ComboboxSelected>>", self._on_type_change)
        self._tip(self.c_type, "Frequency-vs-time law for the sweep. Linear: "
                                "constant Hz/s. dB/Octave & dB/Hz: nonlinear, "
                                "boosts one end of the band via dwell time. "
                                "T-power: linear frequency + power-law amplitude "
                                "envelope. Random: smoothed random-walk "
                                "frequency. Pulse: short broadband tone burst, "
                                "not a true sweep.")

        self.e_f1 = self._labeled_row(p, "Freq start (Hz)", self._entry, 6.0, row); row += 1
        self._tip(self.e_f1, "Instantaneous frequency at the start of the sweep.")
        self.e_f2 = self._labeled_row(p, "Freq end (Hz)", self._entry, 96.0, row); row += 1
        self._tip(self.e_f2, "Instantaneous frequency at the end of the sweep.")
        self.e_len = self._labeled_row(p, "Length (s)", self._entry, 12.0, row); row += 1
        self._tip(self.e_len, "Sweep duration in seconds (for Pulse, the total "
                               "window the envelope sits inside).")
        self.e_phase = self._labeled_row(p, "Start phase (deg)", self._entry, 0.0, row); row += 1
        self._tip(self.e_phase, "Initial phase of the sweep waveform, in degrees.")

        ttk.Separator(p, orient="horizontal").grid(row=row, column=0, columnspan=2,
                                                     sticky="ew", pady=6); row += 1
        ttk.Label(p, text="Tapers", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"); row += 1
        self.e_tap_start = self._labeled_row(p, "Start taper (s)", self._entry, 0.25, row); row += 1
        self._tip(self.e_tap_start, "Ramp-up duration at the start of the sweep.")
        self.e_tap_end = self._labeled_row(p, "End taper (s)", self._entry, 0.25, row); row += 1
        self._tip(self.e_tap_end, "Ramp-down duration at the end of the sweep.")
        self.c_taper_type = self._labeled_row(
            p, "Taper type", lambda pp, d: self._combo(pp, TAPER_TYPES, d), "Cosine", row); row += 1
        self._tip(self.c_taper_type, "Cosine: standard Hann-style raised-cosine "
                                      "ramp. Blackman: steeper, lower sidelobes "
                                      "in the taper's own spectrum, shorter "
                                      "effective ramp-up.")

        ttk.Separator(p, orient="horizontal").grid(row=row, column=0, columnspan=2,
                                                     sticky="ew", pady=6); row += 1
        ttk.Label(p, text="Drive / sampling", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"); row += 1
        self.e_force = self._labeled_row(p, "Drive level (%)", self._entry, 100.0, row); row += 1
        self._tip(self.e_force, "Relative drive/ground-force level, as a "
                                 "percentage. Scales signal amplitude linearly "
                                 "(e.g. 70 for a 70% drive test); correlation "
                                 "energy scales with the square of this value.")
        self.e_dt_ms = self._labeled_row(p, "Sample interval (ms)", self._entry, 1.0, row); row += 1
        self._tip(self.e_dt_ms, "Sample interval in milliseconds (e.g. 2 ms = "
                                 "500 Hz sample rate, Nyquist = 250 Hz). Sets "
                                 "the fs used for spectrum/correlation.")
        self.e_dt_ms.var.trace_add("write", lambda *a: self._update_nyquist_label())
        self.nyquist_label = ttk.Label(p, text="", foreground="#555", wraplength=260)
        self.nyquist_label.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        self._update_nyquist_label()

        ttk.Separator(p, orient="horizontal").grid(row=row, column=0, columnspan=2,
                                                     sticky="ew", pady=6); row += 1
        ttk.Label(p, text="Type-specific", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"); row += 1

        self.e_boost = self._labeled_row(p, "Boost (dB, low->high)", self._entry, 6.0, row); row += 1
        self._tip(self.e_boost, "dB/Octave or dB/Hz sweeps only. Positive: bias "
                                 "sweep dwell time (and energy) toward the high "
                                 "end of the band. Negative: bias toward the low "
                                 "end. 0 = standard log/linear sweep.")
        self.e_tpow = self._labeled_row(p, "T-power exponent", self._entry, 2.0, row); row += 1
        self._tip(self.e_tpow, "T-power sweeps only. Exponent p in the "
                                "amplitude envelope (t/T)^p applied across the "
                                "whole sweep, independent of the edge tapers.")
        self.e_seed = self._labeled_row(p, "Random seed", self._entry, 0, row); row += 1
        self._tip(self.e_seed, "Random sweeps only. Seed for the random-walk "
                                "instantaneous frequency -- same seed reproduces "
                                "the same trace.")
        self.e_smooth = self._labeled_row(p, "Random smoothing (s)", self._entry, 0.5, row); row += 1
        self._tip(self.e_smooth, "Random sweeps only. Moving-average window "
                                  "used to smooth the random-walk frequency "
                                  "trajectory; larger = slower wandering.")
        self.e_cycles = self._labeled_row(p, "Pulse cycles under envelope", self._entry, 2.5, row); row += 1
        self._tip(self.e_cycles, "Pulse sweeps only. Roughly how many cycles of "
                                  "the center frequency fit under the Gaussian "
                                  "envelope.")
        self._on_type_change()
        row += 5

        ttk.Separator(p, orient="horizontal").grid(row=row, column=0, columnspan=2,
                                                     sticky="ew", pady=6); row += 1
        ttk.Label(p, text="Stacking (theoretical)", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"); row += 1
        self.e_stack_n = self._labeled_row(p, "Stack count (n)", self._entry, 1, row); row += 1
        self._tip(self.e_stack_n, "Number of identical sources/repeats to "
                                   "combine in the stacked/array overlay.")
        self.e_stack_sep = self._labeled_row(p, "Sweep separation (m)", self._entry, 0.0, row); row += 1
        self._tip(self.e_stack_sep, "Spacing between array elements. 0 = "
                                     "stationary (all sources at the same "
                                     "point) -- pure n-fold gain, no shape "
                                     "change. Nonzero requires an apparent "
                                     "velocity below to compute the array "
                                     "response.")
        self.e_stack_vel = self._labeled_row(
            p, "Apparent velocity (m/s)", self._entry, 400.0, row); row += 1
        self._tip(self.e_stack_vel, "Apparent (moveout) velocity of the "
                                     "wavefield component you're evaluating "
                                     "the array against -- e.g. a typical "
                                     "ground-roll speed. Only used when "
                                     "separation > 0.")
        self.v_stack_add = tk.BooleanVar(value=False)
        chk = ttk.Checkbutton(p, text="Also add stacked/array version",
                               variable=self.v_stack_add)
        chk.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        self._tip(chk, "When checked, 'Add sweep to plot' also adds a second "
                       "overlay trace: the theoretical composite of n sources, "
                       "cross-correlated against the original single-unit "
                       "reference sweep (not against itself).")
        ttk.Label(p, text="n=1 or separation=0: pure linear n-fold gain, no "
                           "shape change. Separation>0 needs an apparent "
                           "velocity (e.g. ground-roll speed) and applies a "
                           "frequency-dependent array response -- can help "
                           "or hurt depending on the geometry.",
                  foreground="#555", wraplength=260).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 4)); row += 1

        ttk.Separator(p, orient="horizontal").grid(row=row, column=0, columnspan=2,
                                                     sticky="ew", pady=8); row += 1
        btn_row = ttk.Frame(p)
        btn_row.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4); row += 1
        b_add = ttk.Button(btn_row, text="Add sweep to plot", command=self._add_sweep)
        b_add.pack(fill=tk.X, pady=2)
        self._tip(b_add, "Generate the sweep defined above and overlay it "
                          "(in a new color) on both views' panels, recording "
                          "the vibrator currently set on the Vibrator tab. "
                          "Also adds the stacked/array version if that "
                          "checkbox is on.")
        b_clear = ttk.Button(btn_row, text="Clear all sweeps", command=self._clear_sweeps)
        b_clear.pack(fill=tk.X, pady=2)
        self._tip(b_clear, "Remove every sweep currently on the plot.")
        b_remove = ttk.Button(btn_row, text="Remove last sweep", command=self._remove_last)
        b_remove.pack(fill=tk.X, pady=2)
        self._tip(b_remove, "Remove only the most recently added overlay trace.")

        ttk.Separator(p, orient="horizontal").grid(row=row, column=0, columnspan=2,
                                                     sticky="ew", pady=8); row += 1
        ttk.Label(p, text="Export", font=("", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w"); row += 1
        self.export_fmt = tk.StringVar(value=EXPORT_FORMATS[0][0].upper())
        fmt_row = ttk.Frame(p); fmt_row.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1
        # Labelled, because there are two export buttons under it now and
        # this choice governs only the first: SVG and PNG are image formats
        # and have nothing to do with the trace export's ASCII/SEG-Y.
        ttk.Label(fmt_row, text="Image format:").pack(side=tk.LEFT, padx=(0, 6))
        # Driven by EXPORT_FORMATS so the buttons, the frozen build's bundled
        # backends and --selftest cannot drift apart. See that constant.
        for fmt, _backend, tip in EXPORT_FORMATS:
            rb = ttk.Radiobutton(fmt_row, text=fmt.upper(),
                                  variable=self.export_fmt, value=fmt.upper())
            rb.pack(side=tk.LEFT)
            self._tip(rb, tip)
        # Side by side, not stacked: this panel is the tallest thing in the
        # window and already runs off the bottom of a short one (a 900 px
        # window loses the Export row entirely), so a second export button
        # is not allowed to cost another row of height.
        exp_row = ttk.Frame(p)
        exp_row.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4); row += 1
        b_export = ttk.Button(exp_row, text="Figure...", command=self._export)
        b_export.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        self._tip(b_export, "Save the current view (all six panels, current "
                             "zoom) to disk as an image, in the format "
                             "selected above. The two views export "
                             "separately -- switch and export again for the "
                             "other one.")

        # The samples, not the picture. Options live in a dialog rather than
        # on this panel: there are ten of them, they are set once and then
        # rarely changed, and the panel has no room.
        self.tx_sweep = tk.BooleanVar(value=True)
        self.tx_wavelet = tk.BooleanVar(value=True)
        self.tx_ascii = tk.BooleanVar(value=True)
        self.tx_segy = tk.BooleanVar(value=True)
        # Off by default: it is the format for a specific downstream package
        # rather than a general one, and it writes a file per sweep.
        self.tx_petrel = tk.BooleanVar(value=False)
        # One place the file-format keys meet their variables, so the dialog,
        # the writer and the saved settings cannot drift apart.
        self._tx_kind_vars = {"ascii": self.tx_ascii, "segy": self.tx_segy,
                               "petrel": self.tx_petrel}
        self.tx_segy_fmt = tk.StringVar(value=SEGY_SAMPLE_FORMATS[0][0])
        self.tx_source = tk.StringVar(value=TRACE_SOURCES[0][0])
        self.tx_amp = tk.StringVar(value=TRACE_AMPLITUDES[0][0])
        self.tx_extent = tk.StringVar(value=TRACE_EXTENTS[0][0])
        self.tx_samples = tk.StringVar(value=TRACE_WAVELET_SAMPLES)
        b_traces = ttk.Button(exp_row, text="Traces (data)...",
                               command=self._export_traces)
        b_traces.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self._tip(b_traces, "Save the sweeps themselves -- the samples, not "
                             "a picture of them -- as an ASCII table, a "
                             "SEG-Y file and/or Petrel ASCII wavelets. You "
                             "choose the sweep, its autocorrelation wavelet, "
                             "or both, and how many samples of wavelet; one "
                             "trace per sweep on the plot. The image format "
                             "above does not apply here.")

        self.status = ttk.Label(p, text="No sweeps yet.", wraplength=260, foreground="#555")
        self.status.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        # Registry of every field that gets saved/restored as "recent
        # parameters" (see _collect_state / _apply_state).
        self._state_vars = {
            "label": self.e_label.var, "sweep_type": self.c_type.var,
            "f1": self.e_f1.var, "f2": self.e_f2.var, "length": self.e_len.var,
            "phase": self.e_phase.var, "tap_start": self.e_tap_start.var,
            "tap_end": self.e_tap_end.var, "taper_type": self.c_taper_type.var,
            "force": self.e_force.var, "dt_ms": self.e_dt_ms.var,
            "boost": self.e_boost.var, "tpow": self.e_tpow.var,
            "seed": self.e_seed.var, "smooth": self.e_smooth.var,
            "cycles": self.e_cycles.var, "stack_n": self.e_stack_n.var,
            "stack_sep": self.e_stack_sep.var, "stack_vel": self.e_stack_vel.var,
            "stack_add": self.v_stack_add, "export_fmt": self.export_fmt,
            "tx_sweep": self.tx_sweep, "tx_wavelet": self.tx_wavelet,
            "tx_ascii": self.tx_ascii, "tx_segy": self.tx_segy,
            "tx_petrel": self.tx_petrel,
            "tx_segy_fmt": self.tx_segy_fmt, "tx_source": self.tx_source,
            "tx_amp": self.tx_amp, "tx_extent": self.tx_extent,
            "tx_samples": self.tx_samples,
        }

        # Auto-generate the sweep label from its parameters (e.g.
        # "Linear, 6-96 Hz, 12 s, tapers 500/300 ms cosine, 70% DL, 1 ms")
        # instead of a generic "Sweep N", and keep it live-updated as fields
        # change -- but only while the user hasn't typed a custom label over
        # it. Every field that describe_params() can print is traced, so the
        # label never lags behind a parameter it displays.
        for var in (self.c_type.var, self.e_f1.var, self.e_f2.var, self.e_len.var,
                    self.e_phase.var, self.e_tap_start.var, self.e_tap_end.var,
                    self.c_taper_type.var, self.e_force.var, self.e_dt_ms.var,
                    self.e_boost.var, self.e_tpow.var, self.e_seed.var,
                    self.e_smooth.var, self.e_cycles.var):
            var.trace_add("write", self._refresh_auto_label)
        self._refresh_auto_label()

    def _try_auto_label(self):
        """Best-effort descriptive label from the current field values;
        falls back to a generic 'Sweep N' if anything doesn't parse yet
        (e.g. mid-edit)."""
        try:
            p = SweepParams(
                sweep_type=self.c_type.var.get(),
                f1=float(self.e_f1.var.get()),
                f2=float(self.e_f2.var.get()),
                length=float(self.e_len.var.get()),
                start_phase_deg=float(self.e_phase.var.get()),
                taper_start_len=float(self.e_tap_start.var.get()),
                taper_end_len=float(self.e_tap_end.var.get()),
                taper_type=self.c_taper_type.var.get(),
                force_pct=float(self.e_force.var.get()),
                dt_ms=float(self.e_dt_ms.var.get()),
                nonlin_boost_db=float(self.e_boost.var.get()),
                t_power_exp=float(self.e_tpow.var.get()),
                random_seed=int(float(self.e_seed.var.get())),
                random_smooth_s=float(self.e_smooth.var.get()),
                pulse_cycles=float(self.e_cycles.var.get()),
            )
            return describe_params(p)
        except ValueError:
            return f"Sweep {self.sweep_counter + 1}"

    def _refresh_auto_label(self, *args):
        if self._suspend_auto_label:
            return
        current = self.e_label.var.get()
        if current.strip() == "" or current == self._last_auto_label:
            new_label = self._try_auto_label()
            self.e_label.var.set(new_label)
            self._last_auto_label = new_label

    # ------------------------------------------------------------- vibrator
    def _build_vibrator_tab(self, p):
        p.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(p, text="Vibrator", font=("", 11, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6)); row += 1
        ttk.Label(p, text="Mechanical limits of the unit. Used by the Field "
                           "model view; the Sweep design view is unaffected.",
                  foreground="#555", wraplength=260).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 6)); row += 1

        # The saved library is read once, here, because this tab is its only
        # consumer. Problems are reported through the status line rather than
        # a modal: they are per-entry and the program is perfectly usable
        # without the offending machine.
        self.vib_library, lib_problems = load_vib_library()
        self._vib_deleted = set()      # see _write_vib_library

        self.c_vib_preset = self._labeled_row(
            p, "Preset", lambda pp, d: self._combo(
                pp, self._vib_preset_values(), d),
            DEFAULT_PRESET, row); row += 1
        self.c_vib_preset.bind("<<ComboboxSelected>>", self._on_vib_preset)
        self._tip(self.c_vib_preset,
                   "Built-in size classes first, then your own saved "
                   "vibrators, then Custom. The built-ins are named by "
                   "hold-down weight rather than by a force rating -- the "
                   "usable force is derived from the weight and the "
                   "decoupling margin, and shown on the panel. They are "
                   "representative figures for each class, not the specs of "
                   "any particular product; for a real machine, enter its "
                   "spec sheet below and save it under its own name. Editing "
                   "any field switches this to Custom.")

        self.e_vib_label = self._labeled_row(p, "Label", self._entry,
                                              DEFAULT_PRESET, row); row += 1
        self._tip(self.e_vib_label, "Name for this vibrator in the legend of "
                                     "sweeps added while it is selected, and "
                                     "the name it is filed under by Save "
                                     "vibrator.")

        units_row = ttk.Frame(p)
        units_row.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 2)); row += 1
        ttk.Label(units_row, text="Units").pack(side=tk.LEFT, padx=(0, 8))
        self.vib_units = tk.StringVar(value=UNIT_SYSTEMS[0])
        self._units_prev = UNIT_SYSTEMS[0]
        for u in UNIT_SYSTEMS:
            rb = ttk.Radiobutton(units_row, text=u, variable=self.vib_units,
                                  value=u, command=self._on_vib_units)
            rb.pack(side=tk.LEFT, padx=(0, 6))
            self._tip(rb, "Switches the fields below, and the readout on the "
                          "figure, between SI (kg, mm, m/s, kN) and field "
                          "units (lb, in, in/s, lbf). Converts the values "
                          "already entered -- it never reinterprets them.")

        # Entries hold DISPLAYED values in the current unit system; SI is
        # recovered in _read_vib. Kept in a dict keyed by the VibratorParams
        # attribute so the conversion, the read and the preset load all walk
        # the same table (VIB_FIELDS) and cannot drift apart.
        self.vib_entries = {}
        preset = VIBRATOR_PRESETS[DEFAULT_PRESET]
        si_factors = VIB_UNIT_FACTORS[UNIT_SYSTEMS[0]]
        for attr, si_label, _f_label, tip in VIB_FIELDS:
            e = self._labeled_row(
                p, si_label, self._entry,
                self._fmt_vib(getattr(preset, attr) * si_factors[attr][0]),
                row); row += 1
            self._tip(e, tip)
            self.vib_entries[attr] = e

        # Any edit means the fields no longer describe the named preset.
        for e in self.vib_entries.values():
            e.var.trace_add("write", self._on_vib_edit)

        lib_row = ttk.Frame(p)
        lib_row.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(6, 2)); row += 1
        b_vib_save = ttk.Button(lib_row, text="Save vibrator",
                                 command=self._save_vib_to_library)
        b_vib_save.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        b_vib_del = ttk.Button(lib_row, text="Delete",
                                command=self._delete_vib_from_library)
        b_vib_del.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self._tip(b_vib_save,
                   "File the settings above under the name in Label, so the "
                   "machine can be picked from Preset next time. Stored in "
                   f"{os.path.basename(VIB_LIBRARY_FILE)}, always in SI "
                   "whichever units you typed them in, and kept separate from "
                   "the settings file so it survives a settings reset.")
        self._tip(b_vib_del,
                   "Remove the selected saved vibrator from the library. The "
                   "values stay in the fields, so a delete is recoverable by "
                   "saving them again. Built-in presets cannot be deleted.")

        ttk.Separator(p, orient="horizontal").grid(row=row, column=0, columnspan=2,
                                                     sticky="ew", pady=8); row += 1
        self.vib_readout = ttk.Label(p, text="", foreground="#333", wraplength=260,
                                      justify="left")
        self.vib_readout.grid(row=row, column=0, columnspan=2, sticky="w"); row += 1

        ttk.Label(p, text="Each sweep records the vibrator that was set when "
                           "it was added, so a heavy and a light unit can be "
                           "compared on one plot. Drive level stays on the "
                           "Sweep tab -- it is an operating choice, not a "
                           "property of the machine.",
                  foreground="#555", wraplength=260).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 0)); row += 1

        self._state_vars.update({
            "vib_preset": self.c_vib_preset.var, "vib_label": self.e_vib_label.var,
            "vib_units": self.vib_units, "view": self.view,
            **{f"vib_{attr}": e.var for attr, e in self.vib_entries.items()},
        })
        self._suspend_vib_preset = False
        # Drive level lives on the Sweep tab but scales the force ceiling, so
        # the derived numbers here have to follow it.
        self.e_force.var.trace_add("write", self._on_drive_change)
        self._refresh_vib_readout()
        if lib_problems:
            self.status.config(
                text="Saved vibrators skipped: " + "; ".join(lib_problems))
        elif self.vib_library:
            self.status.config(
                text=f"{len(self.vib_library)} saved vibrator(s) loaded from "
                      f"{os.path.basename(VIB_LIBRARY_FILE)}.")

    # ------------------------------------------------------- vibrator library
    def _vib_preset_values(self):
        """Combobox entries: built-ins, then saved machines, then Custom.

        A saved name that shadows a built-in is dropped here rather than
        listed twice -- Save vibrator refuses such a name, so this only
        happens to a hand-edited file, and the built-in has to keep working."""
        saved = [n for n in sorted(self.vib_library) if n not in VIBRATOR_PRESETS]
        return list(VIBRATOR_PRESETS) + saved + ["Custom"]

    def _known_vibrators(self) -> dict:
        """Every selectable vibrator by name. Built-ins win a collision."""
        return {**self.vib_library, **VIBRATOR_PRESETS}

    def _refresh_vib_presets(self, select=None):
        self.c_vib_preset.config(values=self._vib_preset_values())
        if select is not None:
            self.c_vib_preset.var.set(select)

    def _write_vib_library(self):
        """Merge with what is on disk NOW, then write. Raises OSError.

        The in-memory copy was read at startup, and the file is shared: a
        second instance of the program, or the user with a text editor, may
        have added a machine since. Writing our own copy wholesale would
        delete theirs without a word -- which is exactly what happened the
        first time this feature met a second writer.

        Entries deleted in THIS session are remembered separately, so the
        merge does not resurrect them from the copy on disk.
        """
        disk, _ = load_vib_library()
        merged = {n: v for n, v in disk.items() if n not in self._vib_deleted}
        merged.update(self.vib_library)
        save_vib_library(merged)
        self.vib_library = merged

    def _save_vib_to_library(self):
        """File the current fields under the Label, and write the library now.

        Written immediately rather than at exit: this is reference data the
        user has just finished deriving, and losing it to a crash would cost
        far more than re-typing a sweep length."""
        name = self.e_vib_label.var.get().strip()
        if not name or name == "Custom":
            messagebox.showerror(
                "Name the vibrator",
                "Type a name in the Label field first -- that is the name it "
                "gets filed and listed under.")
            return
        if name in VIBRATOR_PRESETS:
            messagebox.showerror(
                "Name already taken",
                f"'{name}' is a built-in preset. Choose another name, so the "
                "list cannot show two different machines under one entry.")
            return
        try:
            vib = self._read_vib()
        except ValueError as exc:
            messagebox.showerror("Cannot save vibrator", str(exc))
            return
        if name in self.vib_library and not messagebox.askyesno(
                "Overwrite?", f"'{name}' is already saved. Replace it with "
                               "the settings currently on this tab?"):
            return
        previous = self.vib_library.get(name)
        was_deleted = name in self._vib_deleted
        self.vib_library[name] = vib
        self._vib_deleted.discard(name)
        try:
            self._write_vib_library()
        except OSError as exc:
            # Put the library back the way it was: a failed write must not
            # leave the list showing a machine that is not on disk.
            if previous is None:
                self.vib_library.pop(name, None)
            else:
                self.vib_library[name] = previous
            if was_deleted:
                self._vib_deleted.add(name)
            messagebox.showerror("Save failed", str(exc))
            return
        self._refresh_vib_presets(select=name)
        self.status.config(text=f"Saved vibrator '{name}' to "
                                 f"{os.path.basename(VIB_LIBRARY_FILE)}.")

    def _delete_vib_from_library(self):
        """Remove the selected saved machine, leaving its numbers in place."""
        name = self.c_vib_preset.var.get()
        if name not in self.vib_library:
            name = self.e_vib_label.var.get().strip()
        if name not in self.vib_library:
            messagebox.showinfo(
                "Nothing to delete",
                "Select one of your saved vibrators in Preset first. Built-in "
                "presets cannot be deleted.")
            return
        if not messagebox.askyesno(
                "Delete saved vibrator?",
                f"Remove '{name}' from the library? Its values stay in the "
                "fields, so you can save it again if this was a mistake."):
            return
        removed = self.vib_library.pop(name)
        self._vib_deleted.add(name)
        try:
            self._write_vib_library()
        except OSError as exc:
            self.vib_library[name] = removed
            self._vib_deleted.discard(name)
            messagebox.showerror("Delete failed", str(exc))
            return
        self._refresh_vib_presets(select="Custom")
        self.status.config(text=f"Deleted saved vibrator '{name}'.")

    def _on_drive_change(self, *args):
        self._refresh_vib_readout()
        self._refresh_vibinfo()
        self._redraw_if_field()

    def _redraw_if_field(self):
        """Repaint only when the change is actually on screen."""
        if self.view.get() == "field" and getattr(self, "canvas", None) is not None:
            self.canvas.draw_idle()

    def _sync_vib_labels(self):
        """Relabel the fields for the current unit system WITHOUT converting.

        Used after restoring saved state, where the stored values are already
        in the stored unit system -- converting them there would apply the
        factor a second time."""
        for attr, (label, _factor) in self._vib_labels(self.vib_units.get()).items():
            self.vib_entries[attr].row_label.config(text=label)
        self._units_prev = self.vib_units.get()

    @staticmethod
    def _fmt_vib(x: float) -> str:
        """Trim a converted value to something enterable, not 3400.0000000000005.

        Eight significant figures rather than six: the unit toggle rewrites
        these fields in place, so every switch re-rounds them, and at six
        figures 70,000 lb came back as 31,751.5 kg instead of 31,751.466.
        Harmless once, but it is a ratchet -- and %g still drops the trailing
        zeros, so clean values stay clean."""
        return f"{x:.8g}"

    def _vib_labels(self, units):
        """(label, factor) per field: label from VIB_FIELDS, factor from the
        engine's VIB_UNIT_FACTORS -- the same table the library file uses."""
        i = 1 if units == UNIT_SYSTEMS[0] else 2
        table = VIB_UNIT_FACTORS[units]
        return {row[0]: (row[i], table[row[0]][0]) for row in VIB_FIELDS}

    def _on_vib_units(self):
        """Convert what is already entered into the newly chosen units.

        The values on screen mean the same thing before and after: only their
        unit changes. Re-labelling without converting -- or converting without
        re-labelling -- would silently turn a 26 t vibrator into a 26,000 lb
        one, so both happen here together or not at all."""
        new = self.vib_units.get()
        if new == self._units_prev:
            return
        old_map, new_map = self._vib_labels(self._units_prev), self._vib_labels(new)
        for attr, e in self.vib_entries.items():
            try:
                shown = float(e.var.get())
            except ValueError:
                continue      # mid-edit garbage: leave it for _read_vib to reject
            si = shown / old_map[attr][1]
            e.var.set(self._fmt_vib(si * new_map[attr][1]))
            e.row_label.config(text=new_map[attr][0])
        self._units_prev = new
        self._refresh_vib_readout()
        self._refresh_vibinfo()
        self._redraw_if_field()

    def _on_vib_preset(self, event=None):
        name = self.c_vib_preset.var.get()
        vib = self._known_vibrators().get(name)
        if vib is None:
            return        # "Custom": keep whatever is in the fields
        factors = self._vib_labels(self.vib_units.get())
        self._suspend_vib_preset = True
        try:
            for attr, e in self.vib_entries.items():
                e.var.set(self._fmt_vib(getattr(vib, attr) * factors[attr][1]))
            self.e_vib_label.var.set(name)
        finally:
            self._suspend_vib_preset = False
        self._refresh_vib_readout()
        self._refresh_vibinfo()
        self._redraw_if_field()

    def _on_vib_edit(self, *args):
        if not getattr(self, "_suspend_vib_preset", False):
            if self.c_vib_preset.var.get() != "Custom":
                self.c_vib_preset.var.set("Custom")
                # A built-in's name describes a generic size class, so once
                # the numbers move it is a lie and gets dropped. A SAVED name
                # is the user's own machine, and editing it is almost always
                # refining that machine -- keeping the name is what lets Save
                # vibrator offer to update it rather than making them retype.
                if self.e_vib_label.var.get() in VIBRATOR_PRESETS:
                    self.e_vib_label.var.set("Custom vibrator")
        self._refresh_vib_readout()
        self._refresh_vibinfo()

    def _read_vib(self) -> VibratorParams:
        """The Vibrator tab as SI VibratorParams. Raises ValueError like
        _read_params does, with the field named as the user sees it."""
        factors = self._vib_labels(self.vib_units.get())
        kwargs = {}
        for attr, e in self.vib_entries.items():
            label, factor = factors[attr]
            try:
                shown = float(e.var.get())
            except ValueError:
                raise ValueError(f"{label} must be a number.")
            if shown <= 0:
                raise ValueError(f"{label} must be greater than zero.")
            kwargs[attr] = shown / factor
        if kwargs["decouple_pct"] > 100:
            raise ValueError("Decoupling margin cannot exceed 100% -- above "
                              "the hold-down weight the baseplate decouples.")
        label = self.e_vib_label.var.get().strip() or "Vibrator"
        # Carry the unit system along so that saving this machine writes it
        # back the way it was typed. Nothing in the physics reads it.
        return VibratorParams(label=label, entry_units=self.vib_units.get(),
                               **kwargs)

    def _current_drive(self) -> float:
        try:
            return float(self.e_force.var.get())
        except (ValueError, AttributeError):
            return 100.0

    def _refresh_vib_readout(self):
        """The derived numbers, live on the tab as the fields are edited."""
        try:
            vib = self._read_vib()
        except ValueError as exc:
            self.vib_readout.config(text=str(exc), foreground="#a33")
            return
        drive = self._current_drive()
        lines = [f"{k}: {v}" for k, v in
                 vibrator_summary(vib, drive, self.vib_units.get())[5:]]
        self.vib_readout.config(text="\n".join(lines), foreground="#333")

    def _refresh_vibinfo(self):
        """Push the current vibrator onto the figure's readout panel."""
        panel = getattr(self, "_vibinfo", None)
        if panel is None:
            return
        try:
            vib = self._read_vib()
        except ValueError:
            return        # mid-edit: leave the last good reading on the figure
        self._set_panel_rows(panel, vibrator_summary(
            vib, self._current_drive(), self.vib_units.get()))
        try:
            w_px = max(self.fig.get_figwidth() * self.fig.dpi, 1.0)
            self._fit_text_panel(panel, w_px)
        except Exception:
            pass          # no renderer yet; the next layout pass sizes it

    def _build_zoom_tab(self, p):
        ttk.Label(p, text="Explicit axis ranges (blank = auto)", font=("", 10, "bold")).grid(
            row=0, column=0, columnspan=5, sticky="w", pady=(0, 6))
        headers = ["Panel", "X min", "X max", "Y min", "Y max"]
        for c, h in enumerate(headers):
            ttk.Label(p, text=h, font=("", 9, "bold")).grid(row=1, column=c, padx=2)

        for i, key in enumerate(PANELS + PANELS_FIELD):
            r = 2 + i
            ttk.Label(p, text=PANEL_TITLES[key]).grid(row=r, column=0, sticky="w", pady=2)
            self.range_vars[key] = {}
            for c, field in enumerate(["xmin", "xmax", "ymin", "ymax"], start=1):
                v = tk.StringVar(value="")
                e = ttk.Entry(p, textvariable=v, width=8)
                e.grid(row=r, column=c, padx=2, pady=2)
                self.range_vars[key][field] = v

        n_panels = len(PANELS) + len(PANELS_FIELD)
        btn_row = ttk.Frame(p)
        btn_row.grid(row=2 + n_panels, column=0, columnspan=5, pady=10, sticky="ew")
        b_apply = ttk.Button(btn_row, text="Apply zoom", command=self._redraw)
        b_apply.pack(fill=tk.X, pady=2)
        self._tip(b_apply, "Redraw every panel using the ranges entered above "
                            "(blank fields stay auto-scaled). The two blocks "
                            "are the two views; each keeps its own ranges.")
        b_reset = ttk.Button(btn_row, text="Reset all to auto", command=self._reset_zoom)
        b_reset.pack(fill=tk.X, pady=2)
        self._tip(b_reset, "Clear every range field and let all panels "
                            "auto-scale again.")

        ttk.Label(p, text="Tip: leave a field blank to auto-scale that "
                           "bound. Apply zoom re-draws with current sweeps.",
                  foreground="#555", wraplength=280).grid(
            row=3 + n_panels, column=0, columnspan=5, sticky="w", pady=(4, 0))

    def _reset_zoom(self):
        for key in PANELS + PANELS_FIELD:
            for field in ("xmin", "xmax", "ymin", "ymax"):
                self.range_vars[key][field].set("")
        self._redraw()

    def _get_range(self, key):
        """Returns (xmin,xmax,ymin,ymax) as floats or None each, from the zoom tab."""
        out = []
        for field in ("xmin", "xmax", "ymin", "ymax"):
            s = self.range_vars[key][field].get().strip()
            if s == "":
                out.append(None)
            else:
                try:
                    out.append(float(s))
                except ValueError:
                    out.append(None)
        return tuple(out)

    def _update_nyquist_label(self):
        try:
            dt_ms = float(self.e_dt_ms.var.get())
            if dt_ms <= 0:
                raise ValueError
            fs = 1000.0 / dt_ms
            nyq = fs / 2.0
            safe = 0.8 * nyq
            self.nyquist_label.config(
                text=f"fs = {fs:.1f} Hz | Nyquist = {nyq:.1f} Hz | "
                     f"safe ceiling (0.8x Nyquist) = {safe:.1f} Hz")
        except ValueError:
            self.nyquist_label.config(text="Enter a positive sample interval (ms).")

    def _on_type_change(self, event=None):
        t = self.c_type.var.get()
        widget_rows = {
            "dB/Octave": [self.e_boost],
            "dB/Hz": [self.e_boost],
            "T-power": [self.e_tpow],
            "Random": [self.e_seed, self.e_smooth],
            "Pulse": [self.e_cycles],
            "Linear": [],
        }
        all_extra = [self.e_boost, self.e_tpow, self.e_seed, self.e_smooth, self.e_cycles]
        show = widget_rows.get(t, [])
        for w in all_extra:
            if w in show:
                w.grid()
                w.row_label.grid()
            else:
                w.grid_remove()
                w.row_label.grid_remove()

    # --------------------------------------------------------------- plots
    def _build_plot_panel(self):
        # BASE_DPI flat, NOT multiplied by the display scale: matplotlib's Tk
        # backend already does that. On Windows it reads the scale out of Tk
        # (_update_device_pixel_ratio) and sets figure.dpi = ratio *
        # figure._original_dpi. Handing it a pre-scaled dpi applies the factor
        # twice -- 225 dpi on a 150% display -- which is what made axis labels
        # overlap the neighbouring panels in v1.0.2. Set the nominal dpi and
        # let the backend scale it; read the result back through fpx().
        self.fig = Figure(figsize=(11, 8.9), dpi=BASE_DPI)
        # Six equal panels on a 2x3 grid (a 2x6 gridspec with every panel
        # spanning two columns, so the pixel-gap arithmetic in _apply_layout
        # stays uniform). Row 0: Signal / Freq-vs-time / metrics key. Row 1:
        # Spectrum / Corr / Env. Signal used to span 2/3 of the top row; it
        # was cut back to one cell so the freed corner can carry the key to
        # the legend's abbreviations -- exported images have to explain
        # themselves without the About dialog.
        #
        # Fixed margins/spacing (not tight_layout!) -- subplot positions and
        # sizes are set ONCE here and never recomputed from tick-label
        # content. tight_layout() recalculates spacing on every call, so
        # applying a zoom range (which changes tick labels) would shift
        # every OTHER panel's position too -- it looked like zooming the
        # whole canvas instead of just one plot. Fixed margins avoid that.
        #
        # The one exception is `top`: it's deliberately recomputed in
        # _redraw() based on how many rows the single shared legend needs
        # (see below) -- that's an intentional, content-driven adjustment
        # tied to the sweep count, not an incidental one tied to zooming.
        gs = self.fig.add_gridspec(2, 6)
        self.ax_signal = self.fig.add_subplot(gs[0, 0:2])
        self.ax_freq = self.fig.add_subplot(gs[0, 2:4])
        self.ax_info = self.fig.add_subplot(gs[0, 4:6])
        self.ax_spec = self.fig.add_subplot(gs[1, 0:2])
        self.ax_corr = self.fig.add_subplot(gs[1, 2:4])
        self.ax_env = self.fig.add_subplot(gs[1, 4:6])

        # The field-model view: a second set of six axes on the SAME gridspec
        # cells, so subplots_adjust positions both sets identically and the
        # whole pixel-budget layout in _apply_layout is reused untouched. Only
        # one set is ever visible (_set_view). Two figures, or a rebuilt
        # gridspec, would have meant a second copy of the margin solver -- the
        # part of this program that took longest to get right on a scaled
        # display, and the last part worth duplicating.
        self.ax_flimit = self.fig.add_subplot(gs[0, 0:2])
        self.ax_fsig = self.fig.add_subplot(gs[0, 2:4])
        self.ax_vibinfo = self.fig.add_subplot(gs[0, 4:6])
        self.ax_fspec = self.fig.add_subplot(gs[1, 0:2])
        self.ax_foct = self.fig.add_subplot(gs[1, 2:4])
        self.ax_fcorr = self.fig.add_subplot(gs[1, 4:6])

        # ax_info / ax_vibinfo are deliberately NOT in self.axes: that dict
        # drives clearing on replot and the axis-range/zoom tab, and both of
        # those panels hold text rather than data.
        self.axes = {"signal": self.ax_signal, "freq": self.ax_freq, "spec": self.ax_spec,
                     "corr": self.ax_corr, "env": self.ax_env,
                     "flimit": self.ax_flimit, "fsig": self.ax_fsig,
                     "fspec": self.ax_fspec, "foct": self.ax_foct,
                     "fcorr": self.ax_fcorr}
        self._shared_legend = None

        # Text (titles, axis labels, tick labels, legend) is drawn in
        # POINTS, which -- at fixed dpi -- is a constant PIXEL size on
        # screen no matter how big or small the canvas itself is resized.
        # FigureCanvasTkAgg resizes the figure's inch-dimensions to match
        # the widget on every window resize (dpi held constant), so the
        # PLOT AREA shrinks/grows with the window while fixed-point-size
        # text does not. Margins expressed as FRACTIONS of the figure
        # therefore shrink in pixels as the window shrinks, while the text
        # they have to hold stays the same size -- that's why full-screen
        # looked fine but the default (smaller) window had y-axis titles
        # overlapping the neighbouring plot.
        #
        # Fix: keep the font sizes fixed (as they originally were) and
        # instead reserve the margins/gaps in PIXELS -- a constant amount
        # of room for the constant-size text furniture. Those pixel
        # budgets get converted to figure fractions against the CURRENT
        # canvas size in _apply_layout(), so the plot content itself
        # scales proportionally with the GUI while the labels always get
        # exactly the room they need. See _apply_layout / _on_canvas_configure.
        #
        # Nominal: the room the text needs at BASE_DPI. Converted to the
        # figure's real pixels by _scale_margins(), which runs on every layout
        # pass rather than once here -- the Tk backend changes the figure dpi
        # when the canvas is first mapped, and again if the window moves to a
        # differently scaled monitor, so a value computed at construction is
        # stale by the time it is used.
        self._nominal_margins = {
            "left": 78,    # y label + y tick labels of the leftmost panel
            "right": 22,   # overhang of the rightmost x tick label
            "bottom": 52,  # x tick labels + x label of the bottom row
            "footer": 22,  # credit / licence footer below the panels
            "title": 30,   # panel title above the top row
            "gap_w": 78,   # between columns: inner y label + tick labels
            "gap_h": 78,   # between rows: top x label + bottom title
            "legend_row": 31,   # per legend row at _legend_fs
            "legend_pad": 16,   # legend frame padding / breathing room
        }
        self._scale_margins()
        self._legend_fs = 12      # legend text: the sweep parameter strings,
                                   # deliberately larger than the tick labels
                                   # since they're the read-me-first content
        self._legend_fs_used = 12  # actual size in use; drops below
                                    # _legend_fs only on a very narrow canvas
        self._legend_rows = 0   # _legend_row_px / _legend_pad_px: see
                                 # _nominal_margins and _scale_margins
        self._resize_after_id = None

        # Metrics table artists (see _build_table): header cells, one entry
        # per sweep, and the grid lines. Rebuilt when the sweep set changes,
        # only re-sized/re-placed on resize.
        self._table_head = []
        self._table_body = []
        self._table_hlines = []
        self._table_vlines = []

        # Credit / licence footer. It lives on the FIGURE rather than in a Tk
        # widget so it is part of every exported SVG/PNG too, not just the
        # on-screen window -- the plots get shared around on their own.
        self._footer_fs = 9.5   # 1.25x the original 7.5 pt
        self._footer = self.fig.text(
            0.5, 0.004, FOOTER_TEXT, ha="center", va="bottom",
            fontsize=self._footer_fs, color="#6a6a6a")

        self._glossary = self._build_text_panel(
            self.ax_info, GLOSSARY_TITLE, GLOSSARY_ROWS, GLOSSARY_NOTE)
        self._vibinfo = self._build_text_panel(
            self.ax_vibinfo, VIBINFO_TITLE,
            vibrator_summary(VIBRATOR_PRESETS[DEFAULT_PRESET]), VIBINFO_NOTE)
        self._refresh_vibinfo()
        self._set_view(self.view.get())

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.right)
        # With SWEEP_DESIGN_SCALE set, drive the backend's own scaling hook
        # rather than only the Tk side. Off Windows the backend derives its
        # ratio from the X server's reported DPI, which is 1.0 on an ordinary
        # desktop, so without this there is no way to exercise the scaled
        # figure path from Linux at all -- and not being able to is precisely
        # how the double-scaling bug reached a release.
        if os.environ.get(UI_SCALE_ENV, "").strip() and self.ui_scale != 1.0:
            try:
                self.canvas._set_device_pixel_ratio(self.ui_scale)
            except Exception:
                pass
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        # add="+" is essential here: FigureCanvasTkAgg already binds its
        # OWN <Configure> handler (self.resize) in its __init__ to keep the
        # figure's pixel size in sync with the widget. A plain .bind() call
        # replaces that binding instead of adding to it, silently breaking
        # matplotlib's auto-resize -- the canvas would stop rescaling on
        # window resize and just clip/crop instead.
        self.canvas.get_tk_widget().bind("<Configure>", self._on_canvas_configure, add="+")
        toolbar = NavigationToolbar2Tk(self.canvas, self.right)
        toolbar.update()

    def _build_text_panel(self, ax, title, rows, notes):
        """A two-column key/value panel of text on a plot-shaped axes.

        Used for both text panels in the top-right cell: the metrics key in the
        sweep view and the vibrator readout in the field view. Positions and
        point sizes are all left unset -- they are solved against the panel's
        real pixel size in _fit_text_panel, so there is nothing meaningful to
        place until then. Returns the handle that method takes back."""
        ax.set_title(title, fontsize=12)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_facecolor("#fbfbfb")
        for spine in ax.spines.values():
            spine.set_color("#c9c9c9")
        # It holds text, not data -- keep the toolbar's zoom/pan off it, or a
        # stray drag scrolls the content out of its own frame.
        ax.set_navigate(False)

        panel = {"ax": ax, "notes": list(notes), "keys": [], "vals": []}
        for key, val in rows:
            panel["keys"].append(ax.text(
                0.0, 0.0, key, transform=ax.transAxes, va="top", ha="left",
                fontweight="bold", color="#1a1a1a"))
            panel["vals"].append(ax.text(
                0.0, 0.0, val, transform=ax.transAxes, va="top", ha="left",
                color="#333333"))
        # One Text per sentence, each wrapped on its own, so the second always
        # begins on a fresh line instead of running on from the first.
        panel["note_artists"] = [
            ax.text(0.0, 0.0, s, transform=ax.transAxes, va="top", ha="left",
                    color="#5f5f5f", style="italic", linespacing=1.3)
            for s in notes
        ]
        return panel

    def _set_panel_rows(self, panel, rows):
        """Replace a text panel's key/value text in place.

        The ROW COUNT is fixed at construction, so this is only ever a text
        swap -- the vibrator readout always has the same nine rows, only their
        values (and one label, which carries the drive level) change."""
        for artist, (key, val) in zip(zip(panel["keys"], panel["vals"]), rows):
            artist[0].set_text(key)
            artist[1].set_text(val)

    def _fit_text_panel(self, panel, w_px):
        """Size and lay out a text panel to fill its cell.

        The panel is a fraction of the canvas, so it shrinks with the window
        while point-sized text does not -- the text has to be solved against
        the panel's current pixels, not set once. Rather than shrink from a
        fixed nominal size (which left the lower half of the panel empty), the
        largest size that still fits BOTH the widest row and the stacked
        height of everything is picked, and the rows are then spaced to match
        it. Measure-and-step-down, as with the footer: rendered width is not
        linear in point size, so a computed ratio overshoots."""
        try:
            renderer = self.fig.canvas.get_renderer()
        except Exception:
            return   # no renderer yet -- the first resize pass sizes it
        keys, vals = panel["keys"], panel["vals"]
        notes, note_artists = panel["notes"], panel["note_artists"]
        if not keys:
            return
        pos = panel["ax"].get_position()
        h_px = self.fig.get_figheight() * self.fig.dpi
        panel_w, panel_h = pos.width * w_px, pos.height * h_px
        if panel_w <= 0 or panel_h <= 0:
            return

        pad = 0.045                       # left/right inset, panel fractions
        pt_px = self.fig.dpi / 72.0
        avail_w = panel_w * (1.0 - 2 * pad)

        def measure(fs, show_note):
            """Apply `fs` to everything, re-wrap the notes for it, and report
            whether the result fits along with the column offset and pitches."""
            for text in keys + vals:
                text.set_fontsize(fs)
            for text, sentence in zip(note_artists, notes):
                text.set_fontsize(fs * 0.92)
                text.set_text(self._wrap_text(sentence, text, renderer, avail_w))

            # Values start just right of the widest key, so the gutter tracks
            # the font instead of being a fixed fraction that goes gappy at one
            # size and cramped at another.
            widest = max(t.get_window_extent(renderer).width for t in keys)
            col = pad + (widest + 0.9 * fs * pt_px) / panel_w
            val_w = max(t.get_window_extent(renderer).width for t in vals)

            row_px = fs * 1.65 * pt_px
            note_px = fs * 0.92 * 1.3 * pt_px
            gap_px = row_px * 0.85
            n_note = sum(t.get_text().count("\n") + 1 for t in note_artists)
            total_h = len(keys) * row_px
            if show_note:
                total_h += gap_px + n_note * note_px

            fits = (col * panel_w + val_w <= panel_w * (1.0 - pad)
                    and total_h <= panel_h * 0.93)
            return fits, col, row_px, gap_px, note_px

        def solve(show_note):
            for text in note_artists:
                text.set_visible(show_note)
            fs = GLOSSARY_FS_MAX
            while True:
                result = measure(fs, show_note)
                if result[0] or fs <= GLOSSARY_FS_MIN:
                    return (fs,) + result
                fs = max(GLOSSARY_FS_MIN, fs - 0.5)

        fs, fits, col, row_px, gap_px, note_px = solve(True)
        if not fits:
            # Panel too short for the whole block even at the smallest legible
            # size (a small window with a tall legend leaves the top row barely
            # 120 px). The key rows are the part that has to survive, so the
            # closing note steps aside rather than spilling out of frame.
            fs, fits, col, row_px, gap_px, note_px = solve(False)

        y = 0.96
        for i, (key, val) in enumerate(zip(keys, vals)):
            row_y = y - i * row_px / panel_h
            key.set_position((pad, row_y))
            val.set_position((col, row_y))
        note_y = y - (len(keys) * row_px + gap_px) / panel_h
        for text in note_artists:
            text.set_position((pad, note_y))
            note_y -= (text.get_text().count("\n") + 1) * note_px / panel_h

    @staticmethod
    def _wrap_text(sentence, text, renderer, max_px):
        """Greedy word wrap measured in real rendered pixels rather than in
        characters -- the font is proportional, so a character budget either
        wastes a third of the line or overruns it. get_text_width_height_descent
        measures a candidate string without having to create an artist for it."""
        prop = text.get_fontproperties()
        lines, cur = [], ""
        for word in sentence.split():
            trial = f"{cur} {word}" if cur else word
            width = renderer.get_text_width_height_descent(trial, prop, False)[0]
            if cur and width > max_px:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
        return "\n".join(lines)

    # -------------------------------------------------------- metrics table
    def _build_table(self, corr_ref, metrics_key="metrics"):
        """(Re)create the artists of the metrics table under the plots: a
        header row, then one row per sweep -- colour swatch plus one cell per
        metric -- and the grid lines that separate them. Content only; every
        coordinate here is a placeholder, because the point size and the
        column positions depend on the canvas and are solved on each resize in
        _layout_table.

        Rebuilt from scratch rather than updated in place: the row count
        follows the sweep count, and 'pk' is relative to the shared reference
        of the CURRENT overlay set, so every cell can change when one sweep is
        added or removed anyway."""
        self._clear_table()
        if not self.results:
            return

        # Units live in the header, once, instead of in every cell.
        self._table_head = [
            self.fig.text(0.0, 0.0, f"{abbr} ({unit})", ha="right", va="center",
                          fontweight="bold", color="#555555")
            for abbr, unit in METRIC_COLUMNS
        ]
        for r in self.results:
            # In the field view the metrics describe the FORCE wavelet, and
            # live one level down in the cached field-domain analysis.
            src = r if metrics_key == "metrics" else (r.get(metrics_key) or {})
            vals = metric_values(src.get("metrics") or {}, corr_ref)
            swatch = Line2D([0, 0], [0, 0], transform=self.fig.transFigure,
                             color=r["color"], linewidth=2.6, solid_capstyle="butt")
            self.fig.add_artist(swatch)
            self._table_body.append({
                "swatch": swatch,
                # An en dash, not a blank: "not measurable for this wavelet"
                # (a Gaussian pulse has no side lobes) should look deliberate.
                "cells": [self.fig.text(0.0, 0.0, vals.get(abbr, ("–",))[0],
                                         ha="right", va="center", color="#333333")
                           for abbr, _unit in METRIC_COLUMNS],
            })

        # Grid: one horizontal line per row boundary (n rows + header, so
        # n+2 lines) and one vertical per column boundary (swatch column plus
        # the metrics, so len+2). Created here because their COUNT is fixed by
        # the sweep set; their coordinates are not.
        def rule(outer):
            line = Line2D([0, 0], [0, 0], transform=self.fig.transFigure,
                           color=TABLE_RULE_COLOR if outer else TABLE_GRID_COLOR,
                           linewidth=0.8 if outer else 0.5)
            self.fig.add_artist(line)
            return line

        n_rows, n_cols = len(self._table_body), len(METRIC_COLUMNS) + 1
        # Outer frame, plus the line under the header: those three carry the
        # table's shape, the rest only keep the cells apart.
        self._table_hlines = [rule(i in (0, n_rows, n_rows + 1))
                              for i in range(n_rows + 2)]
        self._table_vlines = [rule(j in (0, n_cols)) for j in range(n_cols + 1)]

    def _clear_table(self):
        for artist in self._table_artists():
            try:
                artist.remove()
            except (ValueError, NotImplementedError):
                pass   # already detached (e.g. a re-entrant rebuild)
        self._table_head = []
        self._table_body = []
        self._table_hlines = []
        self._table_vlines = []

    def _table_artists(self):
        artists = list(self._table_head) + self._table_hlines + self._table_vlines
        for row in self._table_body:
            artists += [row["swatch"]] + row["cells"]
        return artists

    def _layout_table(self, w_px, h_px):
        """Size and place the metrics table, and report the height in pixels
        it needs so _apply_layout can reserve it above the footer.

        Every column is sized to its own content plus a fixed padding, and the
        whole block is then CENTRED on the canvas -- with the sweep names left
        to the legend there is no reason to stretch the numbers across the full
        width of the figure, and a centred block reads as one object. The point
        size steps down (never up) only if the table would otherwise run past
        the edge, which on a normal window it never does."""
        if not self._table_body:
            return 0.0
        pt_px = self.fig.dpi / 72.0
        n_rows = len(self._table_body)
        # Point sizes ride on pt_px and need no help; the three constants
        # measured in pixels do. See SweepDesignApp.px().
        swatch_px = self.fpx(TABLE_SWATCH_PX)
        pad_top_px = self.fpx(TABLE_PAD_TOP_PX)
        pad_bot_px = self.fpx(TABLE_PAD_BOT_PX)
        try:
            renderer = self.fig.canvas.get_renderer()
        except Exception:
            renderer = None
        if renderer is None:
            # First pass, before there is anything to measure with: reserve
            # the nominal height so the panels don't jump on the next draw.
            return (pad_top_px + pad_bot_px
                    + (n_rows + 1) * TABLE_FS * TABLE_ROW_MULT * pt_px)

        def measure(fs):
            """Cell widths at `fs`: the swatch column first, then one per
            metric, each sized to the wider of its header and its values."""
            for text in self._table_head:
                text.set_fontsize(fs)
            for row in self._table_body:
                for cell in row["cells"]:
                    cell.set_fontsize(fs)
            pad = TABLE_PAD_MULT * fs * pt_px
            widths = [swatch_px + 2 * pad]
            for j, head in enumerate(self._table_head):
                cells = [row["cells"][j] for row in self._table_body]
                widths.append(max(t.get_window_extent(renderer).width
                                   for t in [head] + cells) + 2 * pad)
            return widths, pad

        fs = TABLE_FS
        while True:
            widths, pad = measure(fs)
            if sum(widths) <= w_px * TABLE_MAX_W_FRAC or fs <= TABLE_FS_MIN:
                break
            fs = max(TABLE_FS_MIN, fs - 0.5)

        row_px = fs * TABLE_ROW_MULT * pt_px
        # Rows are stacked upward from a baseline just above the footer strip,
        # so the table stays put while the plots above it take the slack.
        base = self._m_footer_px + pad_bot_px
        x = 0.5 * (w_px - sum(widths))          # centred on the canvas
        edges = [x]
        for width in widths:
            x += width
            edges.append(x)

        def hline(i):    # boundary i counted from the bottom of the table
            return (base + i * row_px) / h_px

        for i, row in enumerate(self._table_body):
            y = (base + (n_rows - i - 0.5) * row_px) / h_px
            mid = 0.5 * (edges[0] + edges[1])
            row["swatch"].set_data([(mid - swatch_px / 2) / w_px,
                                     (mid + swatch_px / 2) / w_px], [y, y])
            for cell, right in zip(row["cells"], edges[2:]):
                cell.set_position(((right - pad) / w_px, y))

        head_y = hline(n_rows + 0.5)
        for head, right in zip(self._table_head, edges[2:]):
            head.set_position(((right - pad) / w_px, head_y))

        x0, x1 = edges[0] / w_px, edges[-1] / w_px
        for i, line in enumerate(self._table_hlines):
            line.set_data([x0, x1], [hline(i)] * 2)
        for edge, line in zip(edges, self._table_vlines):
            line.set_data([edge / w_px] * 2, [hline(0), hline(n_rows + 1)])

        return pad_top_px + pad_bot_px + (n_rows + 1) * row_px

    def _get_float(self, widget, name):
        try:
            return float(widget.var.get())
        except ValueError:
            raise ValueError(f"'{name}' must be a number (got: {widget.var.get()!r})")

    def _read_params(self):
        self.sweep_counter += 1
        label = self.e_label.var.get().strip() or f"Sweep {self.sweep_counter}"
        p = SweepParams(
            label=label,
            sweep_type=self.c_type.var.get(),
            f1=self._get_float(self.e_f1, "Freq start"),
            f2=self._get_float(self.e_f2, "Freq end"),
            length=self._get_float(self.e_len, "Length"),
            start_phase_deg=self._get_float(self.e_phase, "Start phase"),
            taper_start_len=self._get_float(self.e_tap_start, "Start taper"),
            taper_end_len=self._get_float(self.e_tap_end, "End taper"),
            taper_type=self.c_taper_type.var.get(),
            force_pct=self._get_float(self.e_force, "Drive level"),
            dt_ms=self._get_float(self.e_dt_ms, "Sample interval"),
            nonlin_boost_db=self._get_float(self.e_boost, "Boost"),
            t_power_exp=self._get_float(self.e_tpow, "T-power exponent"),
            random_seed=int(self._get_float(self.e_seed, "Random seed")),
            random_smooth_s=self._get_float(self.e_smooth, "Random smoothing"),
            pulse_cycles=self._get_float(self.e_cycles, "Pulse cycles"),
        )
        if p.f1 <= 0 or p.f2 <= 0:
            raise ValueError("Frequencies must be > 0 Hz.")
        if p.length <= 0:
            raise ValueError("Length must be > 0 s.")
        if p.dt_ms <= 0:
            raise ValueError("Sample interval must be > 0 ms.")
        if p.taper_start_len + p.taper_end_len > p.length:
            raise ValueError("Start + end taper cannot exceed sweep length.")
        if p.force_pct < 0:
            raise ValueError("Drive level cannot be negative.")
        return p

    def _add_sweep(self):
        try:
            p = self._read_params()
            vib = self._read_vib()
        except ValueError as exc:
            messagebox.showerror("Invalid parameter", str(exc))
            return
        r = generate_sweep(p)
        sig, fs = r["signal"], r["fs"]
        freqs, mag_lin = compute_spectrum(sig, fs)
        lags, ac_raw = compute_autocorrelation(sig, fs)
        env_raw = compute_envelope(ac_raw)
        color = COLOR_CYCLE[self.next_color_idx % len(COLOR_CYCLE)]
        self.next_color_idx += 1

        # The vibrator is recorded WITH the sweep, not read at draw time:
        # comparing a heavy unit against a light one means two traces on one
        # plot, each carrying the machine it was added under. ref_signal is
        # what this trace is correlated against -- itself here, the original
        # single-unit sweep for a stacked one below -- and the field view
        # needs the same distinction, so it is stored rather than inferred.
        self.results.append({
            "params": p, "t": r["t"], "signal": sig, "inst_freq": r["inst_freq"],
            "ref_signal": sig, "vib": vib,
            "freqs": freqs, "mag_lin": mag_lin, "lags": lags, "ac_raw": ac_raw, "env_raw": env_raw,
            "color": color,
            "metrics": compute_sweep_metrics(lags, ac_raw, env_raw, freqs, mag_lin),
        })

        warn = ""
        if max(p.f1, p.f2) > p.safe_freq:
            warn = (f" Note: top frequency {max(p.f1, p.f2):.1f} Hz exceeds the "
                    f"0.8x-Nyquist safe ceiling ({p.safe_freq:.1f} Hz) for a "
                    f"{p.dt_ms:g} ms sample interval.")

        # Optional stacked / array overlay, cross-correlated against the
        # ORIGINAL single-unit signal (not against itself) so the peak
        # scales linearly with n, matching real correlate-then-sum
        # practice -- see sweep_engine.stack_sweep() docstring.
        if self.v_stack_add.get():
            try:
                n_stack = int(self._get_float(self.e_stack_n, "Stack count"))
                sep = self._get_float(self.e_stack_sep, "Sweep separation")
                vel = self._get_float(self.e_stack_vel, "Apparent velocity")
            except ValueError as exc:
                messagebox.showerror("Invalid parameter", str(exc))
                self._redraw()
                self.status.config(text=f"{len(self.results)} sweep(s) plotted.{warn}")
                return
            if n_stack < 2:
                messagebox.showinfo("Stack count too low",
                                     "Set Stack count (n) to 2 or more to add a "
                                     "stacked/array version.")
            else:
                composite = stack_sweep(sig, fs, n_stack, spacing_m=sep, velocity_mps=vel)
                c_freqs, c_mag = compute_spectrum(composite, fs)
                c_lags, c_ac = correlate_signals(composite, sig, fs)
                c_env = compute_envelope(c_ac)
                c_color = COLOR_CYCLE[self.next_color_idx % len(COLOR_CYCLE)]
                self.next_color_idx += 1
                geom = f"stationary" if sep == 0 else f"d={sep:g}m, v={vel:g}m/s"
                stack_label = f"{p.label} [stack x{n_stack}, {geom}]"
                self.results.append({
                    "params": p, "t": r["t"], "signal": composite, "inst_freq": r["inst_freq"],
                    "ref_signal": sig, "vib": vib,
                    "freqs": c_freqs, "mag_lin": c_mag, "lags": c_lags, "ac_raw": c_ac,
                    "env_raw": c_env, "color": c_color, "label_override": stack_label,
                    "metrics": compute_sweep_metrics(c_lags, c_ac, c_env, c_freqs, c_mag),
                })

        self._redraw()
        self.status.config(text=f"{len(self.results)} sweep(s) plotted.{warn}")
        next_auto = self._try_auto_label()
        self.e_label.var.set(next_auto)
        self._last_auto_label = next_auto

    def _clear_sweeps(self):
        self.results = []
        self.next_color_idx = 0
        self._redraw()
        self.status.config(text="No sweeps yet.")

    def _remove_last(self):
        if self.results:
            self.results.pop()
            self.next_color_idx = max(0, self.next_color_idx - 1)
            self._redraw()
            self.status.config(text=f"{len(self.results)} sweep(s) plotted.")

    def _apply_axis_range(self, key, ax):
        xmin, xmax, ymin, ymax = self._get_range(key)
        if xmin is not None or xmax is not None:
            cur = ax.get_xlim()
            ax.set_xlim(xmin if xmin is not None else cur[0],
                         xmax if xmax is not None else cur[1])
        if ymin is not None or ymax is not None:
            cur = ax.get_ylim()
            ax.set_ylim(ymin if ymin is not None else cur[0],
                         ymax if ymax is not None else cur[1])

    def _apply_layout(self):
        """Convert the fixed PIXEL budgets for text furniture (margins, the
        gaps between panels, the legend strip) into figure fractions for the
        canvas's current pixel size -- see _build_plot_panel. Everything left
        over is plot area, so the content scales with the window while the
        labels keep a constant size and a constant amount of room."""
        self._scale_margins()      # the dpi may have moved since the last pass
        w_px = max(self.fig.get_figwidth() * self.fig.dpi, 1.0)
        h_px = max(self.fig.get_figheight() * self.fig.dpi, 1.0)

        top_px = self._m_title_px
        if self._legend_rows:
            row_px = self._legend_row_px * (self._legend_fs_used / self._legend_fs)
            top_px += self._legend_pad_px + row_px * self._legend_rows

        # Clamps keep the plot area from collapsing on an extremely small
        # canvas; below them the labels simply crowd, which beats zero axes.
        left = min(self._m_left_px / w_px, 0.30)
        right = 1.0 - min(self._m_right_px / w_px, 0.10)
        top = 1.0 - min(top_px / h_px, 0.45)

        # The metrics table sits between the x-axis labels and the footer, and
        # grows by one row per sweep, so the bottom margin has to be solved
        # against it the same way the top margin is solved against the legend.
        table_px = self._layout_table(w_px, h_px)
        bottom = min((self._m_bottom_px + self._m_footer_px + table_px) / h_px, 0.55)
        # ...but the panels come first: on a canvas too short to hold both, the
        # table keeps its rows and slides up over the x-axis labels rather than
        # squeezing the plots down to nothing.
        bottom = min(bottom, max(top - 0.18, 0.0))

        # Footer sits in the strip reserved for it, a few pixels off the
        # bottom edge, and is re-pinned here because its offset is in figure
        # fractions -- which move as the canvas is resized. It's a single
        # unbreakable line, so on a narrow canvas the only way to keep the
        # whole statement visible is to set it slightly smaller.
        self._footer.set_position((0.5, 5.0 / h_px))
        self._footer.set_fontsize(self._footer_fs)
        try:
            renderer = self.fig.canvas.get_renderer()
            fs = self._footer_fs
            # Step down and re-measure rather than extrapolating from one
            # measurement: hinting makes rendered text width non-linear in
            # point size, so a computed ratio overshoots.
            while (fs > 7.0 and
                    self._footer.get_window_extent(renderer).width > w_px * 0.98):
                fs = max(7.0, fs - 0.5)
                self._footer.set_fontsize(fs)
        except Exception:
            pass   # no renderer yet -- the next resize pass sizes it

        # wspace/hspace are fractions of the AVERAGE cell size, not of the
        # figure, so the pixel gaps have to be solved against the cell grid:
        # ncols*cell + (ncols-1)*gap = available.
        avail_w = (right - left) * w_px
        gap_w = min(self._gap_w_px, avail_w * 0.10)
        cell_w = (avail_w - 5 * gap_w) / 6.0
        wspace = gap_w / cell_w if cell_w > 0 else 0.3

        avail_h = (top - bottom) * h_px
        gap_h = min(self._gap_h_px, avail_h * 0.30)
        cell_h = (avail_h - gap_h) / 2.0
        hspace = gap_h / cell_h if cell_h > 0 else 0.2

        self.fig.subplots_adjust(left=left, right=right, top=top, bottom=bottom,
                                  wspace=wspace, hspace=hspace)

        # After the panels move: the text panel's content is sized against the
        # cell it now occupies. Only the visible view's is worth fitting -- the
        # hidden one is re-fitted when it is switched to.
        self._fit_text_panel(
            self._glossary if self.view.get() == "sweep" else self._vibinfo, w_px)

    def _on_canvas_configure(self, event):
        # <Configure> fires repeatedly during a drag-resize -- debounce so
        # we only rescale/redraw once things settle, not on every pixel.
        if self._resize_after_id is not None:
            try:
                self.root.after_cancel(self._resize_after_id)
            except tk.TclError:
                pass
        try:
            self._resize_after_id = self.root.after(120, self._on_resize_settled)
        except tk.TclError:
            pass

    def _on_resize_settled(self):
        self._resize_after_id = None
        if not self.root.winfo_exists():
            return
        # Only the margins, the legend's column split and the table's column
        # widths depend on the canvas size -- the plotted data does not -- so
        # re-lay-out and repaint instead of regenerating every curve.
        self._rebuild_legend()
        self.canvas.draw_idle()

    # ----------------------------------------------------------- view switch
    def _view_axes(self, view=None):
        """The data panels belonging to one view, keyed as in PANEL_TITLES."""
        view = view or self.view.get()
        return {key: self.axes[key] for key in VIEW_PANELS[view]}

    def _set_view(self, view):
        """Show one view's panels and hide the other's.

        Both sets sit on the same gridspec cells, so nothing has to move --
        _apply_layout has already positioned all twelve identically, and only
        visibility separates them."""
        for key, ax in self.axes.items():
            ax.set_visible(key in VIEW_PANELS[view])
        self.ax_info.set_visible(view == "sweep")
        self.ax_vibinfo.set_visible(view == "field")

    def _on_view_change(self):
        self._set_view(self.view.get())
        self._redraw()

    def _field_data(self, r):
        """Force-domain analysis of one sweep, computed once and cached.

        The vibrator was recorded with the sweep, so this never goes stale:
        changing the Vibrator tab affects the NEXT sweep added, not one
        already on the plot -- which is what makes a heavy-vs-light overlay
        possible in the first place.

        'ideal' is the same sweep with the flat hold-down ceiling and no
        stroke or flow limit -- what the unit would radiate if it could hold
        target force all the way down. Carrying it alongside is the whole
        point of the view: the gap between the two curves is what the low end
        of the sweep actually costs."""
        cached = r.get("field")
        if cached is not None:
            return cached
        p, vib = r["params"], r["vib"]
        fs, drive = p.fs, p.force_pct
        # Pulse is broadband at every instant, so "the frequency at time t"
        # is a fiction there and the ceiling has to be applied as a filter.
        # Every swept type gets the time-domain map, which is what a real
        # vibrator's control loop does.
        mode = "spectral" if p.sweep_type == "Pulse" else "time"
        force = apply_force_model(r["signal"], r["inst_freq"], fs, vib, drive, mode)
        force_ref = apply_force_model(r["ref_signal"], r["inst_freq"], fs,
                                       vib, drive, mode)
        # signal already carries the drive level, and target = drive * rated,
        # so multiplying by the RATED force (not the target) is what leaves
        # the drive applied exactly once. See apply_force_model().
        ideal = np.asarray(r["signal"]) * vib.rated_force_n
        ideal_ref = np.asarray(r["ref_signal"]) * vib.rated_force_n

        freqs, mag = compute_spectrum(force, fs)
        i_freqs, i_mag = compute_spectrum(ideal, fs)
        lags, ac = correlate_signals(force, force_ref, fs)
        i_lags, i_ac = correlate_signals(ideal, ideal_ref, fs)
        env = compute_envelope(ac)
        fd = {
            "force": force, "ideal": ideal,
            "freqs": freqs, "mag": mag, "i_freqs": i_freqs, "i_mag": i_mag,
            "lags": lags, "ac": ac, "i_lags": i_lags, "i_ac": i_ac,
            "oct": energy_per_octave(freqs, mag),
            "i_oct": energy_per_octave(i_freqs, i_mag),
            "metrics": compute_sweep_metrics(lags, ac, env, freqs, mag),
        }
        r["field"] = fd
        return fd

    def _draw_field_view(self):
        """Ground force in kN: the same sweeps as the recorded vibrator can
        actually deliver them, against what it would deliver with no stroke or
        flow limit at all (dotted)."""
        ax_lim, ax_sig = self.ax_flimit, self.ax_fsig
        ax_spec, ax_oct, ax_corr = self.ax_fspec, self.ax_foct, self.ax_fcorr

        ax_lim.set_title(PANEL_TITLES["flimit"], fontsize=13)
        ax_lim.set_xlabel("Frequency (Hz)", fontsize=10)
        ax_lim.set_ylabel("Peak ground force (kN)", fontsize=10)
        # Log-log: the stroke and flow limits are power laws, so they come out
        # as straight lines whose slopes (12 and 6 dB/octave) can be read off
        # directly -- on linear axes they are two indistinguishable curves.
        ax_lim.set_xscale("log")
        ax_lim.set_yscale("log")

        ax_sig.set_title(PANEL_TITLES["fsig"], fontsize=13)
        ax_sig.set_xlabel("Time (s)", fontsize=10)
        ax_sig.set_ylabel("Ground force (kN)", fontsize=10)

        ax_spec.set_title(PANEL_TITLES["fspec"], fontsize=13)
        ax_spec.set_xlabel("Frequency (Hz)", fontsize=10)
        ax_spec.set_ylabel("dB (shared ref.)", fontsize=10)

        ax_oct.set_title(PANEL_TITLES["foct"], fontsize=13)
        ax_oct.set_xlabel("Frequency (Hz)", fontsize=10)
        ax_oct.set_ylabel("dB (shared ref.)", fontsize=10)

        ax_corr.set_title(PANEL_TITLES["fcorr"], fontsize=13)
        ax_corr.set_xlabel("Lag (s)", fontsize=10)
        ax_corr.set_ylabel("Amplitude (shared ref.)", fontsize=10)

        corr_ref = None
        if self.results:
            fds = [self._field_data(r) for r in self.results]
            # One shared reference per panel across the whole set, including
            # the ideal traces: the difference between limited and ideal is
            # exactly what must not be normalized away.
            spec_ref = max(max(fd["mag"].max(), fd["i_mag"].max()) for fd in fds) or 1.0
            oct_ref = max(max(fd["oct"].max(), fd["i_oct"].max()) for fd in fds) or 1.0
            corr_ref = max(max(np.abs(fd["ac"]).max(), np.abs(fd["i_ac"]).max())
                            for fd in fds) or 1.0

            for r, fd in zip(self.results, fds):
                c = r["color"]
                vib, drive = r["vib"], r["params"].force_pct
                p = r["params"]
                # The legend carries the machine here, not the sweep design:
                # in this view two entries can differ only by the vibrator.
                lbl = (f"{r.get('label_override', p.label)}\n"
                        f"{describe_vibrator(vib, drive)}")

                # Force limits, over the band this sweep actually covers plus
                # a margin either side, so the knee is never just off-frame.
                lo = max(0.2, min(p.f1, p.f2) * 0.3)
                hi = max(p.f1, p.f2) * 1.5
                grid = np.logspace(np.log10(lo), np.log10(hi), 500)
                lim = force_limits(grid, vib, drive)
                # At zero drive there is no force at all and a log axis has
                # nothing positive to scale itself against, so this one panel
                # steps aside. The rest still say something useful -- a flat
                # zero is an answer.
                if lim["target"][0] > 0:
                    # The stroke and flow curves keep rising for the whole
                    # band -- at 100 Hz the stroke limit is five orders of
                    # magnitude above anything achievable -- so they are cut
                    # off just above the ceiling they cross. Left unclipped
                    # they set the y-range and flatten the part of the plot
                    # worth looking at into a line.
                    ceiling = lim["target"][0] * 3.0
                    for key, style in (("stroke", ":"), ("flow", "--")):
                        ax_lim.plot(grid, np.where(lim[key] <= ceiling,
                                                    lim[key], np.nan) / 1e3,
                                     color=c, linewidth=0.7, linestyle=style,
                                     alpha=0.65)
                    ax_lim.plot(grid, lim["target"] / 1e3, color=c,
                                 linewidth=0.7, linestyle="-.", alpha=0.65)
                    ax_lim.plot(grid, lim["available"] / 1e3, color=c,
                                 label=lbl, linewidth=1.8)
                    # The one number to take away: below this the machine
                    # cannot reach target force however long the sweep is.
                    f_full = vib.f_full_force_hz(drive)
                    if f_full > 0:
                        ax_lim.axvline(f_full, color=c, linewidth=0.8, alpha=0.45)

                ax_sig.plot(r["t"], fd["ideal"] / 1e3, color=c, linewidth=0.7,
                             linestyle=":", alpha=0.6)
                ax_sig.plot(r["t"], fd["force"] / 1e3, color=c, label=lbl,
                             linewidth=0.9)

                for freqs, mag, style, width in (
                        (fd["i_freqs"], fd["i_mag"], ":", 0.8),
                        (fd["freqs"], fd["mag"], "-", 1.0)):
                    db = 20 * np.log10(np.maximum(mag, spec_ref * 1e-12) / spec_ref)
                    ax_spec.plot(freqs, db, color=c, linewidth=width,
                                  linestyle=style,
                                  label=lbl if style == "-" else None)

                for freqs, dens, style, width in (
                        (fd["i_freqs"], fd["i_oct"], ":", 0.8),
                        (fd["freqs"], fd["oct"], "-", 1.0)):
                    db = 10 * np.log10(np.maximum(dens, oct_ref * 1e-12) / oct_ref)
                    ax_oct.plot(freqs, db, color=c, linewidth=width,
                                 linestyle=style,
                                 label=lbl if style == "-" else None)

                ax_corr.plot(fd["i_lags"], fd["i_ac"] / corr_ref, color=c,
                              linewidth=0.7, linestyle=":", alpha=0.6)
                ax_corr.plot(fd["lags"], fd["ac"] / corr_ref, color=c,
                              label=lbl, linewidth=0.9)

            # Which line is which limit, inside the panel: an exported figure
            # has to explain its own line styles, the same reason the metrics
            # key is on the canvas. Neutral grey -- colour already means
            # "which sweep" everywhere else on this figure.
            ax_lim.legend(handles=[
                Line2D([], [], color="#555555", linewidth=0.9, linestyle=ls,
                        label=text)
                for ls, text in ((":", "stroke limit (+12 dB/oct)"),
                                  ("--", "flow limit (+6 dB/oct)"),
                                  ("-.", "hold-down x drive"),
                                  ("-", "achievable"))],
                loc="lower right", fontsize=7, framealpha=0.85,
                handlelength=2.6, borderpad=0.4, labelspacing=0.3)

            # The spectra run to Nyquist but the force limits stop at the top
            # of the sweep band; hold the two frequency panels to the same
            # window so they can be read against each other.
            f_hi = max(max(r["params"].f1, r["params"].f2) for r in self.results) * 1.2
            for ax in (ax_spec, ax_oct):
                ax.set_xlim(0, f_hi)

        # Metrics of the FORCE wavelet, not of the dimensionless sweep: in
        # this view the table has to describe what the machine radiates.
        self._build_table(corr_ref, metrics_key="field")

    def _redraw(self):
        for ax in self.axes.values():
            ax.clear()
        for ax in self.axes.values():
            ax.tick_params(axis="both", labelsize=9)

        if self.view.get() == "field":
            self._draw_field_view()
        else:
            self._draw_sweep_view()

        for key, ax in self._view_axes().items():
            ax.grid(alpha=0.3)
            self._apply_axis_range(key, ax)

        self._rebuild_legend()
        self.canvas.draw_idle()

    def _draw_sweep_view(self):
        self.ax_signal.set_title("Signal", fontsize=13)
        self.ax_signal.set_xlabel("Time (s)", fontsize=10)
        self.ax_signal.set_ylabel("Amplitude", fontsize=10)

        self.ax_freq.set_title("Frequency vs Time", fontsize=13)
        self.ax_freq.set_xlabel("Time (s)", fontsize=10)
        self.ax_freq.set_ylabel("Frequency (Hz)", fontsize=10)

        self.ax_spec.set_title("Amplitude Spectrum", fontsize=13)
        self.ax_spec.set_xlabel("Frequency (Hz)", fontsize=10)
        self.ax_spec.set_ylabel("dB (shared ref.)", fontsize=10)

        self.ax_corr.set_title("Autocorrelation", fontsize=13)
        self.ax_corr.set_xlabel("Lag (s)", fontsize=10)
        self.ax_corr.set_ylabel("Amplitude (shared ref.)", fontsize=10)

        self.ax_env.set_title("Correlation Envelope", fontsize=13)
        self.ax_env.set_xlabel("Lag (s)", fontsize=10)
        self.ax_env.set_ylabel("Amplitude (shared ref.)", fontsize=10)

        corr_ref = None
        if self.results:
            # Shared references across the whole overlay set, so relative
            # amplitude differences between sweeps (duration, drive level,
            # boost) remain visible instead of being masked by per-trace
            # normalization. See sweep_engine.py docstring.
            spec_ref = max(r["mag_lin"].max() for r in self.results)
            spec_ref = spec_ref if spec_ref > 0 else 1.0
            corr_ref = max(np.abs(r["ac_raw"]).max() for r in self.results)
            corr_ref = corr_ref if corr_ref > 0 else 1.0

            for r in self.results:
                c = r["color"]
                # One line: what was asked for. What came out of it -- the
                # measured wavelet quality -- goes in the table below the
                # plots (see _build_table), where the numbers line up in
                # columns instead of trailing each legend entry as prose.
                lbl = r.get("label_override", r["params"].label)
                self.ax_signal.plot(r["t"], r["signal"], color=c, label=lbl, linewidth=0.9)
                if np.all(np.isfinite(r["inst_freq"])):
                    self.ax_freq.plot(r["t"], r["inst_freq"], color=c, label=lbl, linewidth=1.3)
                else:
                    self.ax_freq.plot(r["t"], r["inst_freq"], color=c, label=lbl,
                                       linewidth=1.3, linestyle="--")

                # Floor relative to the shared reference (-240 dB), not an
                # absolute one: mag_lin is a spectral density in amplitude*s,
                # so its magnitude moves with the units of the sweep, and a
                # fixed floor would sit at a different depth for each.
                mag_db = 20 * np.log10(
                    np.maximum(r["mag_lin"], spec_ref * 1e-12) / spec_ref)
                self.ax_spec.plot(r["freqs"], mag_db, color=c, label=lbl, linewidth=1.0)

                ac_disp = r["ac_raw"] / corr_ref
                env_disp = r["env_raw"] / corr_ref
                self.ax_corr.plot(r["lags"], ac_disp, color=c, label=lbl, linewidth=0.9)
                self.ax_env.plot(r["lags"], env_disp, color=c, label=lbl, linewidth=1.1)

        # Rebuilt with the plots, not with the layout: the cells depend on the
        # sweep set and on corr_ref, both of which are settled right here.
        self._build_table(corr_ref)

    def _rebuild_legend(self):
        """One shared legend across the top of the figure instead of a
        repeated legend on each of the 5 panels -- the same handles/labels
        exist on every panel (each sweep is one color throughout), so any
        single panel's handles represent the whole set. Column count and the
        top margin both adapt to the sweeps on the plot and to the canvas
        width, so this is re-run on resize as well as on replot."""
        if self._shared_legend is not None:
            self._shared_legend.remove()
            self._shared_legend = None

        if not self.results:
            self._legend_rows = 0
            self._apply_layout()
            return

        # Any panel of the CURRENT view carries the whole set (one colour per
        # sweep throughout), but the hidden view's panels are empty -- so the
        # handles have to come from a panel that was actually drawn. Not
        # necessarily the first one: a panel can legitimately draw nothing for
        # a given sweep (the force-limit panel has nothing to show at zero
        # drive), so take the fullest, and give up only if none of them
        # labelled anything.
        handles, labels = [], []
        for key in VIEW_PANELS[self.view.get()]:
            h, l = self.axes[key].get_legend_handles_labels()
            if len(l) > len(labels):
                handles, labels = h, l
        n = len(labels)
        if not n:
            self._legend_rows = 0
            self._apply_layout()
            return
        fig_px = self.fig.get_figwidth() * self.fig.dpi

        # Labels are full parameter strings, so the column count has to
        # follow both the widest label and the current canvas width -- a
        # fixed 3 or 4 columns would run entries off the edge of the figure.
        # Text width can't be predicted reliably from character counts
        # (proportional font), so build the legend and measure it, dropping
        # a column at a time until it fits. Once down to a single column
        # there's nothing left to fold, so a very narrow canvas shrinks the
        # legend text instead (down to a floor) rather than clipping it.
        # Entries are single-line now that the metrics have moved to the
        # table, but a legend "row" is still as tall as the tallest entry, so
        # the reservation counts text lines rather than entries.
        lines = max(lbl.count("\n") + 1 for lbl in labels)

        ncol = min(n, 3)
        self._legend_fs_used = self._legend_fs
        while True:
            self._legend_rows = -(-n // ncol) * lines  # ceil division
            self._apply_layout()
            legend = self.fig.legend(
                handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.998),
                ncol=ncol, fontsize=self._legend_fs_used, frameon=True, framealpha=0.9,
                handlelength=1.4, columnspacing=1.4, labelspacing=0.4)
            try:
                width = legend.get_window_extent(self.fig.canvas.get_renderer()).width
            except Exception:
                break   # no renderer yet (first build) -- the resize pass re-runs this
            if width <= fig_px * 0.98:
                break
            if ncol > 1:
                legend.remove()
                ncol -= 1
                continue
            shrunk = max(7.0, self._legend_fs_used * fig_px * 0.98 / width)
            if shrunk >= self._legend_fs_used - 0.1:
                break   # already at the floor -- nothing further to give
            legend.remove()
            self._legend_fs_used = shrunk
        self._shared_legend = legend

    def _export(self):
        if not self.results:
            messagebox.showinfo("Nothing to export", "Add at least one sweep first.")
            return
        fmt = self.export_fmt.get().lower()
        path = filedialog.asksaveasfilename(
            defaultextension=f".{fmt}",
            filetypes=[(f"{fmt.upper()} image", f"*.{fmt}")],
            initialfile=f"sweep_comparison.{fmt}",
        )
        if not path:
            return
        try:
            self.fig.savefig(path, format=fmt)
            self.status.config(text=f"Exported to {path}")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    # --------------------------------------------------------- trace export
    def _trace_items(self, source):
        """One record per sweep on the plot, in the form assemble_traces wants.

        The only thing this decides is WHICH signal the export is of: the
        sweep as designed, or the ground force the recorded vibrator can
        actually radiate. Both already exist -- the field one is the cached
        force analysis -- so switching source never regenerates a sweep, the
        same as switching the view does not.
        """
        items = []
        for r in self.results:
            p = r["params"]
            label = r.get("label_override", p.label)
            if source == "field":
                fd = self._field_data(r)
                items.append({
                    "label": label, "params": p, "trace": fd["force"],
                    "corr": fd["ac"], "lags": fd["lags"],
                    "units": "N (newtons)", "units_corr": "N^2 x seconds",
                    "unit_code": 8,
                    "vib": describe_vibrator(r["vib"], p.force_pct),
                })
            else:
                items.append({
                    "label": label, "params": p, "trace": r["signal"],
                    "corr": r["ac_raw"], "lags": r["lags"],
                    "units": "dimensionless (+/-1 at 100% drive level)",
                    "units_corr": "amplitude^2 x seconds",
                    "unit_code": 0, "vib": None,
                })
        return items

    def _trace_samples(self):
        """The wavelet sample count as typed: a positive int, or None = all.

        Raises ValueError with a message meant for the user, because this is
        read both by the dialog (to grey out its button) and by the export
        itself (where the value has to be right)."""
        text = self.tx_samples.get().strip()
        if not text:
            return None
        try:
            n = int(float(text))
        except ValueError:
            raise ValueError(f"Wavelet samples: {text!r} is not a number.")
        if n < 1:
            raise ValueError("Wavelet samples must be at least 1, "
                              "or blank for the whole autocorrelation.")
        if n > SEGY_MAX_SAMPLES:
            raise ValueError(f"Wavelet samples must be at most "
                              f"{SEGY_MAX_SAMPLES}, which is as much as "
                              f"SEG-Y's 16-bit sample count can express.")
        return n

    def _trace_kinds(self) -> list:
        """The ticked file formats, in the order TRACE_FILE_TYPES lists them."""
        return [k for k, _lbl, _ext, _tip in TRACE_FILE_TYPES
                if self._tx_kind_vars[k].get()]

    def _export_traces(self):
        """Export the samples themselves, rather than a picture of them."""
        if not self.results:
            messagebox.showinfo("Nothing to export", "Add at least one sweep first.")
            return
        # Default the source to whichever view is on screen: whatever the
        # user is looking at is what they mean by "the sweep".
        self.tx_source.set(self.view.get())
        if not self._trace_options_dialog():
            return
        self._write_traces()

    def _trace_options_dialog(self) -> bool:
        """Modal options dialog. True if the user chose to go ahead."""
        win = tk.Toplevel(self.root)
        win.title("Export traces")
        win.transient(self.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        chosen = {"ok": False}
        state = {"row": 0}
        wrap = int(self.px(430))

        def section(text):
            if state["row"]:
                ttk.Separator(frame, orient="horizontal").grid(
                    row=state["row"], column=0, sticky="ew", pady=(10, 6))
                state["row"] += 1
            head = ttk.Label(frame, text=text, font=("", 10, "bold"))
            head.grid(row=state["row"], column=0, sticky="w")
            state["row"] += 1
            return head

        def add(widget, tip=None, indent=True, pad=0):
            widget.grid(row=state["row"], column=0, sticky="w",
                         padx=(self.px(12) if indent else 0, 0), pady=pad)
            state["row"] += 1
            if tip:
                self._tip(widget, tip)
            return widget

        def checkbox(label, var, tip):
            return add(ttk.Checkbutton(frame, text=label, variable=var,
                                        command=refresh), tip)

        def radios(rows, var):
            return [add(ttk.Radiobutton(frame, text=label, variable=var,
                                         value=key, command=refresh), tip)
                    for key, label, tip in rows]

        def enable(widgets, on):
            for w in widgets:
                w.state(["!disabled"] if on else ["disabled"])

        def refresh(*_):
            """Keep the two live lines and the button in step with the boxes."""
            contents = [k for k, v in (("sweep", self.tx_sweep),
                                        ("wavelet", self.tx_wavelet)) if v.get()]
            kinds = self._trace_kinds()
            # A section that cannot affect this export is greyed rather than
            # hidden: the dialog then keeps one shape, and it stays obvious
            # that the setting exists and which box turns it back on. A
            # greyed-out sample count must not block the export either,
            # which is why the parse only runs when the wavelet is wanted.
            wanted = "wavelet" in contents
            enable(wavelet_widgets, wanted)
            enable(segy_widgets, "segy" in kinds)
            problem, n = None, None
            if wanted:
                try:
                    n = self._trace_samples()
                except ValueError as exc:
                    problem = str(exc)
            hint.config(
                text=(problem if problem else
                       self._wavelet_hint(n) if wanted else
                       "Not written: the wavelet is unchecked above."),
                foreground="#a00" if problem else "#555")
            if problem:
                summary.config(text="Fix the sample count to continue.",
                                foreground="#a00")
            elif not contents or not kinds:
                summary.config(
                    text="Choose at least one thing to write and one file "
                          "format.", foreground="#a00")
            else:
                n_tr = len(self.results)
                # Petrel holds one wavelet per file, so it contributes a
                # file per sweep where the other two contribute one file
                # holding every sweep.
                n_files = sum((n_tr if k == "petrel" else 1)
                              for k in kinds) * len(contents)
                per = ("one trace each" if kinds == ["petrel"] else
                        f"{n_tr} trace(s) in each table")
                summary.config(
                    text=f"{n_tr} sweep(s) -> {n_files} file"
                          f"{'s' if n_files != 1 else ''}, {per}.",
                    foreground="#555")
            ok_btn.state(["!disabled"] if (contents and kinds and not problem)
                          else ["disabled"])

        section("What to write")
        for key, label, tip in TRACE_CONTENTS:
            checkbox(label, self.tx_sweep if key == "sweep" else self.tx_wavelet, tip)

        section("Take it from")
        radios(TRACE_SOURCES, self.tx_source)

        section("Amplitude")
        radios(TRACE_AMPLITUDES, self.tx_amp)

        wave_head = section("Wavelet length")
        wavelet_widgets = [wave_head] + radios(TRACE_EXTENTS, self.tx_extent)
        samp_row = ttk.Frame(frame)
        add(samp_row)
        samp_lbl = ttk.Label(samp_row, text="Samples per trace:")
        samp_lbl.pack(side=tk.LEFT)
        e_samp = ttk.Entry(samp_row, textvariable=self.tx_samples, width=8)
        e_samp.pack(side=tk.LEFT, padx=(6, 0))
        self._tip(e_samp,
                   "How many samples the exported wavelet gets. Leave it "
                   "blank for the whole autocorrelation, which is twice the "
                   "sweep length and almost all zeros. A symmetric wavelet "
                   "is centred on the zero-lag peak (an even count is "
                   "rounded up by one so the peak has a sample of its own); "
                   "a half wavelet starts at the peak. The sweep itself is "
                   "always written at its full length -- its sample count "
                   "comes from its own parameters.")
        hint = ttk.Label(frame, text="", foreground="#555", wraplength=wrap)
        add(hint)
        wavelet_widgets += [samp_lbl, e_samp, hint]
        # The variable outlives this window -- it is one of the saved
        # settings -- so the trace has to come off again on close, or a
        # later Load settings fires refresh() against destroyed widgets.
        trace_id = self.tx_samples.trace_add("write", refresh)

        section("File formats")
        for key, label, _ext, tip in TRACE_FILE_TYPES:
            checkbox(label, self._tx_kind_vars[key], tip)
        fmt_lbl = add(ttk.Label(frame, text="SEG-Y sample format:"),
                       indent=True, pad=(6, 0))
        segy_widgets = [fmt_lbl] + radios(
            [(name, f"{name} 32-bit float (format code {code})", tip)
             for name, code, tip in SEGY_SAMPLE_FORMATS], self.tx_segy_fmt)

        ttk.Separator(frame, orient="horizontal").grid(
            row=state["row"], column=0, sticky="ew", pady=(10, 6))
        state["row"] += 1
        summary = ttk.Label(frame, text="", foreground="#555", wraplength=wrap)
        add(summary, indent=False)

        btns = ttk.Frame(frame)
        add(btns, indent=False, pad=(10, 0))

        def close():
            self.tx_samples.trace_remove("write", trace_id)
            win.destroy()

        def on_ok():
            chosen["ok"] = True
            close()

        ok_btn = ttk.Button(btns, text="Choose file name...", command=on_ok)
        ok_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btns, text="Cancel", command=close).pack(side=tk.LEFT)

        refresh()
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _e: close())
        win.bind("<Return>", lambda _e: on_ok() if "disabled" not in
                  ok_btn.state() else None)
        # Placed over the main window rather than wherever the window
        # manager would drop it, and clamped to the screen so a tall dialog
        # on a scaled display cannot open with its buttons off the bottom.
        win.update_idletasks()
        x = self.root.winfo_rootx() + max(
            0, (self.root.winfo_width() - win.winfo_width()) // 2)
        y = self.root.winfo_rooty() + int(self.px(40))
        y = min(y, max(0, self.root.winfo_screenheight() - win.winfo_height()
                        - int(self.px(40))))
        win.geometry(f"+{int(x)}+{int(y)}")
        win.grab_set()
        e_samp.focus_set()
        self.root.wait_window(win)
        return chosen["ok"]

    def _wavelet_hint(self, n_samples) -> str:
        """The live line under the sample box: what that count means in ms."""
        if not self.results:
            return ""
        dt_ms = self.results[0]["params"].dt_ms
        half = self.tx_extent.get() == "half"
        if n_samples is None:
            return ("The whole autocorrelation: as many samples as the "
                     "longest sweep has" + ("." if half else ", doubled."))
        n = n_samples if (half or n_samples % 2) else n_samples + 1
        span = (n - 1) * dt_ms
        if half:
            what = f"lags 0 to +{span:g} ms"
        else:
            what = f"lags -{span / 2:g} to +{span / 2:g} ms"
        note = ""
        if n > SEGY_SAFE_SAMPLES and self.tx_segy.get():
            note = (f"  Over {SEGY_SAFE_SAMPLES} samples, some SEG-Y readers "
                     f"misread the sample count; SEG-Y is still written.")
        return f"{n} samples at {dt_ms:g} ms = {what}.{note}"

    def _trace_header_lines(self, block, content, source, items,
                            only=None) -> list:
        """The description block every file format carries, in one place.

        ASCII gets it as `#` comments, SEG-Y as the 3200-byte textual
        header and Petrel as WAVELET-DESC, so it is written once here and
        adapted at the point of writing. A file of samples with no
        statement of what they are, in what units, at what sample interval
        and from which sweep is a file you cannot trust six months later,
        which is the whole reason this is as long as it is.

        `only` is a trace index, for a format that holds a single trace per
        file: the block then describes that one trace instead of listing
        the whole set, since the others are not in the file.
        """
        source_label = dict((k, lbl) for k, lbl, _t in TRACE_SOURCES)[source]
        if content == "sweep":
            what = "the sweep itself, time zero at the first sample"
        elif self.tx_extent.get() == "half":
            what = ("the autocorrelation of the sweep -- half, zero lag "
                     "onward; it is symmetric, so nothing is lost")
        else:
            what = ("the autocorrelation of the sweep -- full and symmetric, "
                     "centred on the zero-lag peak")
        if block["scale"] != 1.0:
            amp = (f"Amplitude: normalized -- the whole set divided by "
                    f"{block['scale']:.6g} ({block['units']}), one shared "
                    f"divisor, so relative sizes are preserved")
        else:
            amp = (f"Amplitude: as computed, in {block['units']}; not "
                    f"normalized, so traces are directly comparable")
        lines = [
            f"Sweep Design {APP_VERSION} -- exported traces",
            f"Written {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Content: {what}",
            f"Taken from: {source_label}",
            amp,
            f"Sample interval {block['dt_ms']:g} ms, {block['n_samples']} "
            f"samples per trace, first sample at {block['t0_s'] * 1000.0:g} ms",
            (f"Traces: {block['n_traces']}, one per sweep on the plot, in the "
             f"order they were added") if only is None else
            (f"This file holds one trace: number {only + 1} of "
             f"{block['n_traces']} on the plot"),
        ]
        if content == "wavelet" and block["t0_s"] < 0:
            t0_ms = block["t0_s"] * 1000.0
            lines.append(
                (f"Zero lag is at time 0: the SEG-Y delay recording time "
                 f"(bytes 109-110) is {round(t0_ms)} ms, and the ASCII time "
                 f"column is negative before the peak") if only is None else
                (f"Zero lag is at time 0: WAVELET-TFS is {t0_ms:g} ms, and "
                 f"the time column below is a 0-based offset counting from "
                 f"there, so the peak is halfway down it"))
        lines.extend(block["notes"])
        shown = list(enumerate(items))
        if only is not None:
            shown = [(only, items[only])]
        for i, it in shown:
            lines.append("")
            label = str(it["label"])
            lines.append(f"Trace {i + 1}: {label}")
            # The label is auto-generated FROM the parameters unless the
            # user typed over it, so spelling them out underneath usually
            # prints the same sentence twice. Print it only when the label
            # does not already contain it -- which covers a custom label
            # ("Test A") and leaves a stacked trace, whose label is the
            # description plus a "[stack x4, ...]" suffix, unrepeated.
            desc = describe_params(it["params"])
            if desc not in label:
                lines.append(f"  {desc}")
            if it.get("vib"):
                lines.append(f"  vibrator: {it['vib']}")
        return lines

    def _write_traces(self):
        contents = [k for k, v in (("sweep", self.tx_sweep),
                                    ("wavelet", self.tx_wavelet)) if v.get()]
        kinds = self._trace_kinds()
        exts = dict((k, ext) for k, _lbl, ext, _t in TRACE_FILE_TYPES)
        fmt_codes = dict((name, code) for name, code, _t in SEGY_SAMPLE_FORMATS)
        source = self.tx_source.get()

        # Everything that can fail on the data is done BEFORE the file
        # dialog: being asked where to put a file and only then told the
        # sweeps cannot be exported together is a poor order to find out in.
        try:
            # Only when it is going to be used: a stale value left in the
            # box must not stop an export that writes no wavelet.
            n_samples = (self._trace_samples() if "wavelet" in contents
                         else None)
            items = self._trace_items(source)
            blocks = {c: assemble_traces(items, c, self.tx_amp.get(),
                                          self.tx_extent.get(), n_samples)
                      for c in contents}
        except ValueError as exc:
            messagebox.showerror("Cannot export these sweeps", str(exc))
            return

        if "segy" in kinds:
            too_big = [c for c, b in blocks.items()
                       if b["n_samples"] > SEGY_MAX_SAMPLES]
            if too_big:
                messagebox.showerror(
                    "Too many samples for SEG-Y",
                    f"{blocks[too_big[0]]['n_samples']} samples per trace is "
                    f"more than SEG-Y's 16-bit sample count can express "
                    f"(limit {SEGY_MAX_SAMPLES}). Shorten the sweep, widen "
                    f"the sample interval, or ask for fewer wavelet samples.")
                return
            risky = [c for c, b in blocks.items()
                     if b["n_samples"] > SEGY_SAFE_SAMPLES]
            if risky and not messagebox.askokcancel(
                    "Long traces",
                    f"{blocks[risky[0]]['n_samples']} samples per trace is "
                    f"within SEG-Y's limit, but the sample count is stored "
                    f"in 16 bits and some readers treat that field as "
                    f"signed, which tops out at {SEGY_SAFE_SAMPLES}. The "
                    f"file will be written correctly; software that reads it "
                    f"as signed may not open it.\n\nWrite it anyway?"):
                return

        base = filedialog.asksaveasfilename(
            title="Export traces -- base name",
            initialfile="sweep_export",
            filetypes=[("All files", "*.*")],
        )
        if not base:
            return
        # One name for up to four files: the content and the format decide
        # the rest, so "sweep_export" becomes sweep_export_wavelet.sgy and
        # its siblings rather than asking four times.
        for ext in (".txt", ".sgy", ".segy", ".sgd", ".wlt"):
            if base.lower().endswith(ext):
                base = base[:-len(ext)]
                break

        written = []
        try:
            for content, block in blocks.items():
                head = self._trace_header_lines(block, content, source, items)
                for kind in kinds:
                    # One file holding every trace, except Petrel below,
                    # which needs a name per file and so numbers them.
                    path = f"{base}_{content}{exts[kind]}"
                    if kind == "ascii":
                        write_ascii(path, block["data"], block["dt_ms"],
                                     block["t0_s"] * 1000.0,
                                     comment_lines=_wrap_header(head, 96))
                    elif kind == "segy":
                        write_segy(
                            path, block["data"], block["dt_us"],
                            format_code=fmt_codes[self.tx_segy_fmt.get()],
                            delay_ms=round(block["t0_s"] * 1000.0),
                            text_lines=_wrap_header(head, 76, limit=40),
                            trace_fields=block["headers"],
                            extra_binary=_binary_sweep_fields(block["headers"]))
                    else:
                        # One wavelet per file, so one file per sweep, each
                        # with its own name and its own description. Numbered
                        # only when there is more than one, so the common
                        # single-sweep case keeps the plain name.
                        for i, it in enumerate(items):
                            if len(items) > 1:
                                path = f"{base}_{content}_{i + 1}{exts[kind]}"
                            desc = self._trace_header_lines(
                                block, content, source, items, only=i)
                            write_petrel_wavelet(
                                path, block["data"][i], block["dt_ms"],
                                block["t0_s"] * 1000.0,
                                f"{it['label']} [{content}]",
                                description=_wrap_header(desc, 62),
                                history=[
                                    f"{time.strftime('%Y-%m-%d %H:%M')}  "
                                    f"Created by Sweep Design {APP_VERSION}"])
                            written.append(os.path.basename(path))
                        continue
                    written.append(os.path.basename(path))
        except (OSError, ValueError) as exc:
            messagebox.showerror(
                "Export failed",
                f"{exc}\n\n"
                + (f"Written before the failure: {', '.join(written)}"
                   if written else "No files were written."))
            return

        n_tr = blocks[contents[0]]["n_traces"]
        self.status.config(
            text=f"Exported {n_tr} trace(s) to {', '.join(written)} in "
                  f"{os.path.dirname(base) or '.'}")
        messagebox.showinfo(
            "Traces exported",
            f"{n_tr} trace(s) written to:\n\n" + "\n".join(written))


def _wrap_header(lines, width, limit=None):
    """Wrap a description block to a line width, keeping its indentation.

    SEG-Y's textual header is a fixed 40 lines of 80 characters and simply
    has no room for a long overlay set, so `limit` truncates with a line
    saying how much was dropped -- silently losing the tail of the trace
    list would make the header quietly wrong rather than merely short.
    """
    out = []
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        indent = " " * (len(line) - len(line.lstrip()))
        out.extend(textwrap.wrap(line.strip(), width=width - len(indent),
                                  initial_indent=indent,
                                  subsequent_indent=indent + "  ") or [indent])
    if limit is not None and len(out) > limit:
        kept = out[:limit - 1]
        kept.append(f"... {len(out) - limit + 1} more header lines did not "
                    f"fit; see the ASCII export")
        out = kept
    return out


def _binary_sweep_fields(headers) -> dict:
    """Copy the first trace's sweep description into the binary header.

    The binary header has one set of sweep fields for the whole file, so it
    can only describe one sweep. Taking the first is the convention, and the
    per-trace headers carry each sweep's own parameters regardless.
    """
    if not headers:
        return {}
    h = headers[0]
    return {"hsfs": h["sfs"], "hsfe": h["sfe"], "hslen": h["slen"],
            "hstyp": h["styp"], "hstas": h["stas"], "hstae": h["stae"],
            "htatyp": h["tatyp"], "hcorr": h["corr"]}


def _selftest(report_path=None) -> int:
    """Exercise the parts of a packaged build that only fail at runtime.

    A frozen build can compile, launch and plot perfectly and still be unable
    to save a file, because matplotlib loads an output backend lazily inside
    savefig() and a dependency scanner cannot see an import that has not
    happened yet. v1.0 shipped exactly that way. Nothing short of writing a
    real file in each format proves the backend is actually there, so that is
    what this does -- plus a known-answer check on the engine.

    Writes to a temporary directory, opens no window, and returns an exit
    code. `report_path` is for windowed builds, which have nowhere to print:
    the same report is written there as well.
    """
    import traceback

    lines, failed = [], 0

    def check(name, fn):
        nonlocal failed
        try:
            lines.append(f"PASS  {name}: {fn()}")
        except Exception:
            failed += 1
            lines.append(f"FAIL  {name}\n{traceback.format_exc()}")

    lines.append(f"Sweep Design {APP_VERSION} self-test")
    lines.append(f"frozen={getattr(sys, 'frozen', False)} python={sys.version.split()[0]}")
    lines.append(f"state file would be: {display_path(STATE_FILE)}")
    lines.append(
        f"vibrator library would be: {display_path(VIB_LIBRARY_FILE)}")
    lines.append(f"written to {app_dir_kind()}")

    # Informational, never a pass/fail: reports what the display scaling
    # would resolve to, so the numbers can be captured from a command line
    # instead of read off a screenshot. Guarded because a self-test has to
    # keep working where no display exists at all.
    try:
        _declare_dpi_aware()
        probe = tk.Tk()
        probe.withdraw()
        scale = _detect_ui_scale(probe)
        lines.append(
            f"display: {probe.winfo_screenwidth()}x{probe.winfo_screenheight()} px, "
            f"scale {scale:g}x, Tk {float(probe.tk.call('tk', 'scaling')):.3f}, "
            f"figure would run at {BASE_DPI * scale:g} dpi")
        probe.destroy()
    except Exception as exc:
        lines.append(f"display: not probed ({type(exc).__name__})")

    def engine():
        p = SweepParams()
        d = generate_sweep(p)
        sig, fs = d["signal"], d["fs"]
        freqs, mag = compute_spectrum(sig, fs)
        lags, ac = compute_autocorrelation(sig, fs)
        m = compute_sweep_metrics(lags, ac, compute_envelope(ac), freqs, mag)
        # Known answers for the default 6-96 Hz, 12 s linear sweep. These are
        # dt-invariant by construction, so a mismatch means real breakage.
        for key, want, tol in (("ac_peak", 5.844, 0.01), ("pt_db", -10.27, 0.05),
                               ("sll_db", -13.30, 0.05), ("mlw_s", 0.01376, 1e-4)):
            got = m[key]
            if abs(got - want) > tol:
                raise AssertionError(f"{key}={got!r}, expected {want} +/- {tol}")
        return f"{len(sig)} samples, " + format_metrics(m, peak_ref=m["ac_peak"])

    check("engine", engine)

    def vibrator():
        vib = VIBRATOR_PRESETS[DEFAULT_PRESET]
        drive = 70.0
        # The three regimes, checked by their slopes rather than by their
        # values: a slope is what the physics actually asserts, and it stays
        # right even if the preset numbers are ever retuned.
        def slope(a, b, at_drive):
            av = force_limits(np.array([a, b]), vib, at_drive)["available"]
            return 20 * np.log10(av[1] / av[0]) / np.log2(b / a)
        # At 100% drive all three regimes exist. At the 70% used below they do
        # not: the target drops far enough that the stroke curve reaches it
        # BELOW the stroke/flow crossover, so the flow limit never binds and
        # there is no +6 dB/octave stretch to measure. That is the model
        # behaving correctly, not a gap in it -- hence the slopes are checked
        # at full drive, where each regime is guaranteed a window.
        cross, full = vib.f_stroke_flow_hz, vib.f_full_force_hz(100.0)
        for name, got, want in (
                ("stroke slope", slope(cross * 0.2, cross * 0.4, 100.0), 12.0),
                ("flow slope", slope(cross * 1.02, full * 0.98, 100.0), 6.0),
                ("ceiling slope", slope(full * 2, full * 4, 100.0), 0.0)):
            if abs(got - want) > 0.15:
                raise AssertionError(f"{name}={got:.2f} dB/oct, expected {want}")
        # Drive level must act exactly once: peak ground force has to land on
        # the target, not on its square or on the undrived rating.
        p = SweepParams(f1=6.0, f2=96.0, length=8.0, force_pct=drive)
        d = generate_sweep(p)
        force = apply_force_model(d["signal"], d["inst_freq"], d["fs"], vib, drive)
        peak, target = float(np.abs(force).max()), vib.target_force_n(drive)
        if abs(peak - target) > 0.005 * target:
            raise AssertionError(f"peak {peak:.0f} N != target {target:.0f} N")
        # Field units are a display convention only -- a round trip must not
        # move the value.
        back = vib.hold_down_kg / KG_PER_LB * KG_PER_LB
        if abs(back - vib.hold_down_kg) > 1e-6:
            raise AssertionError(f"unit round-trip drifted to {back}")
        return (f"{target / 1e3:.1f} kN at {drive:g}%, full force above "
                f"{vib.f_full_force_hz(drive):.2f} Hz "
                f"({full:.2f} Hz at 100%), crossover {cross:.2f} Hz")

    check("vibrator", vibrator)

    def vib_library():
        """Round-trip the saved-vibrator library through a real file.

        Through a TEMPORARY file: the user's own library sits next to the
        app under the same name, and a self-test that overwrote it would
        destroy exactly the data this feature exists to protect.
        """
        # A machine whose spec sheet is in field units: 62,000 lb hold-down,
        # 8,250 lb reaction mass, 3.5 in stroke, 41.2 in/s.
        typed = {"hold_down": 62000.0, "reaction_mass": 8250.0,
                 "stroke_pp": 3.5, "mass_vel_pk": 41.2, "decouple_pct": 80.0}
        vib = vibrator_from_dict("HEMI-60", {"units": "Field", **typed})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "vibrators.json")
            if load_vib_library(path) != ({}, []):
                raise AssertionError("missing library did not read as empty")
            save_vib_library({vib.label: vib}, path)
            # The file must hold what was TYPED, not its SI equivalent --
            # this is the whole point of the units tag.
            entry = json.load(open(path))["vibrators"]["HEMI-60"]
            if entry.get("units") != "Field":
                raise AssertionError(f"units tag is {entry.get('units')!r}")
            for key, want in typed.items():
                if abs(entry[key] - want) > 1e-9:
                    raise AssertionError(
                        f"{key} written as {entry[key]}, typed {want}")
            lib, problems = load_vib_library(path)
            if problems:
                raise AssertionError(f"round trip reported {problems}")
            got = lib.get(vib.label)
            if got is None:
                raise AssertionError(f"{vib.label} did not come back")
            for attr in VIB_ATTRS:
                a, b = getattr(vib, attr), getattr(got, attr)
                if abs(a - b) > 1e-9:
                    raise AssertionError(f"{attr} drifted {a} -> {b}")
            if abs(got.rated_force_n - vib.rated_force_n) > 1e-6:
                raise AssertionError("rated force did not survive the trip")
            # Same machine saved from an SI tab: different numbers in the
            # file, identical physics out of it.
            si = VibratorParams(**{**{a: getattr(vib, a) for a in VIB_ATTRS},
                                    "label": "HEMI-60", "entry_units": "SI"})
            save_vib_library({si.label: si}, path)
            entry = json.load(open(path))["vibrators"]["HEMI-60"]
            if entry["units"] != "SI" or abs(entry["stroke_pp"] - 88.9) > 1e-6:
                raise AssertionError(f"SI entry wrote {entry}")
            back = load_vib_library(path)[0]["HEMI-60"]
            if abs(back.rated_force_n - vib.rated_force_n) > 1e-6:
                raise AssertionError("the two unit systems disagree")
            # A version-1 file (raw SI under the attribute names, no tag)
            # must still load, and must not be read as millimetres.
            with open(path, "w") as f:
                json.dump({"version": 1, "vibrators": {"Old": {
                    a: getattr(vib, a) for a in VIB_ATTRS}}}, f)
            old = load_vib_library(path)[0].get("Old")
            if old is None or abs(old.stroke_pp_m - vib.stroke_pp_m) > 1e-9:
                raise AssertionError(f"v1 entry read as {old}")
            # A bad entry must cost only itself, not the whole file.
            with open(path, "w") as f:
                json.dump({"version": VIB_LIBRARY_VERSION, "vibrators": {
                    "good": vibrator_to_dict(vib),
                    "bad": {"units": "Field", "hold_down": 0.0},
                    "alien": {"units": "cubits", **typed}}}, f)
            lib, problems = load_vib_library(path)
            if "good" not in lib or len(problems) != 2:
                raise AssertionError(
                    f"bad entries not isolated: kept {sorted(lib)}, "
                    f"problems {problems}")
        return (f"{len(VIB_ATTRS)} fields round-tripped in both unit systems, "
                f"v1 migration and bad-entry isolation")

    check("vibrator library", vib_library)

    def manual():
        """Render the REAL bundled README, and check it came out whole.

        This runs against the shipped file rather than a fixture on
        purpose. The Help button's whole claim is that the manual matches
        the build it came from, so the thing worth testing is that THIS
        copy's README survives the conversion -- including in a frozen
        build, where the README has to be found in the bundle at all.
        """
        src = manual_source_path()
        if src is None:
            raise AssertionError("README.md not found beside the program")
        with open(src, encoding="utf-8") as f:
            md = f.read()
        doc = render_manual(md, "self-test", "self-test")
        heads = [l for l in md.split("\n") if re.match(r"^#{1,6} ", l)]
        ids = re.findall(r'<h[1-6] id="([^"]+)"', doc)
        if len(ids) != len(heads):
            raise AssertionError(f"{len(heads)} headings in, {len(ids)} out")
        anchors = set(ids)
        broken = [t for t in re.findall(r'href="#([^"]+)"', doc)
                  if t not in anchors]
        if broken:
            raise AssertionError(f"dead in-page links: {broken}")
        # Fenced code must survive character for character -- the manual
        # quotes commands and file formats people copy out of it.
        fenced = re.findall(r"```[^\n]*\n(.*?)```", md, re.S)
        rendered = [html.unescape(b) for b in
                    re.findall(r"<pre[^>]*><code>(.*?)</code></pre>", doc, re.S)]
        if len(fenced) != len(rendered):
            raise AssertionError(
                f"{len(fenced)} code blocks in, {len(rendered)} out")
        for a, b in zip(fenced, rendered):
            if a.rstrip("\n") != b.rstrip("\n"):
                raise AssertionError(f"code block altered: {a[:60]!r}")
        # Nothing may reach the reader as raw markdown. Strip the code and
        # the tags, and no markup characters should be left in the prose.
        prose = re.sub(r"<pre.*?</pre>", "", doc, flags=re.S)
        prose = re.sub(r"<code>.*?</code>", "", prose, flags=re.S)
        prose = html.unescape(re.sub(r"<[^>]+>", "", prose))
        for name, pat in (("bold", r"\*\*"), ("code span", r"`"),
                          ("table", r"\|"), ("link", r"\[[^\]]*\]\(")):
            if re.search(pat, prose):
                raise AssertionError(f"unconverted {name} markup in the text")
        if "<table>" not in doc or "<ol>" not in doc:
            raise AssertionError("tables or lists did not render")
        return (f"{len(md):,} chars of README -> {len(doc):,} chars of HTML, "
                f"{len(ids)} headings, {len(rendered)} code blocks, "
                f"{doc.count('<table>')} tables")

    check("manual", manual)

    def no_leaked_paths():
        """Nothing the user can see may print an absolute path.

        This program gets passed around, screenshotted and demoed. An
        absolute path in the About box or a status line describes the
        machine it happens to be running on, not the program, and the
        person sharing it did not choose to publish their directory tree.
        display_path is the single place that decides how a path is shown;
        this is the check that keeps callers going through it.
        """
        home = os.path.abspath(os.path.expanduser("~"))
        cases = [
            (os.path.join(APP_DIR, "sweep_design_state.json"),
             "sweep_design_state.json"),
            (os.path.join(home, "Documents", "x.json"),
             "~" + os.sep + os.path.join("Documents", "x.json")),
        ]
        for raw, want in cases:
            got = display_path(raw)
            if got != want:
                raise AssertionError(f"display_path({raw!r}) = {got!r}, "
                                      f"expected {want!r}")
        # A location outside both is reduced to its filename rather than
        # printed: a roaming %APPDATA% on a network share belongs there,
        # and its full path would name the file server.
        outside = os.path.join(os.sep, "opt", "elsewhere", "x.json")
        if display_path(outside) != "x.json":
            raise AssertionError(
                f"external path not reduced: {display_path(outside)!r}")
        # And the visible text itself must be clean.
        for name, text in (("About", ABOUT_TEXT),
                           ("licence", LICENSE_STATEMENT)):
            for token in (APP_DIR, home):
                if token and token in text:
                    raise AssertionError(f"{name} text contains {token!r}")
        return f"About is {len(ABOUT_TEXT)} chars, no absolute path in it"

    check("no leaked paths", no_leaked_paths)

    def trace_export():
        """Prove the exported bytes decode back to the numbers that went in.

        A figure that looks right on screen is its own evidence; a file of
        samples is not. Nobody can tell by eye that a SEG-Y trace header
        put the sample interval in the right two bytes, that IBM floats
        round-trip, or that a half wavelet really starts at the zero-lag
        peak -- and all three are the kind of mistake that is only found
        weeks later by the person trying to load the file. So the file is
        written, read back, and compared.
        """
        # Two sweeps of DIFFERENT lengths, sharing a sample interval: the
        # zero-padding path and the shared-normalization path both need
        # more than one trace to mean anything.
        pa = SweepParams(label="A", f1=6, f2=96, length=4.0, dt_ms=2.0,
                          taper_start_len=0.5, taper_end_len=0.3)
        pb = SweepParams(label="B", sweep_type="dB/Octave", f1=8, f2=80,
                          length=3.0, dt_ms=2.0, force_pct=70.0)
        items = []
        for p in (pa, pb):
            g = generate_sweep(p)
            lags, ac = compute_autocorrelation(g["signal"], g["fs"])
            items.append({"label": p.label, "params": p, "trace": g["signal"],
                           "corr": ac, "lags": lags, "vib": None,
                           "units": "dimensionless",
                           "units_corr": "amplitude^2 x seconds",
                           "unit_code": 0})

        # IBM floats: worst case is a value whose leading hex digit is 1,
        # where the fraction is down to 21 significant bits.
        probe = np.array([0.0, 1.0, -1.0, 1e-8, -6.02e23, 0.0625, 1 / 3])
        back = ibm_to_ieee(ieee_to_ibm(probe))
        err = np.max(np.abs(back - probe) / np.maximum(np.abs(probe), 1e-300))
        if err > 1e-6:
            raise AssertionError(f"IBM float round trip off by {err:.2e}")

        n_want = 201
        sweep_block = assemble_traces(items, "sweep", "raw", "full", n_want)
        full = assemble_traces(items, "wavelet", "raw", "full", n_want)
        half = assemble_traces(items, "wavelet", "raw", "half", n_want)
        norm = assemble_traces(items, "wavelet", "norm", "full", n_want)

        n_a = len(items[0]["trace"])
        if sweep_block["data"].shape != (2, n_a):
            raise AssertionError(f"sweep block is {sweep_block['data'].shape}, "
                                  f"expected (2, {n_a}) -- padded to the longest")
        if sweep_block["t0_s"] != 0.0 or sweep_block["dt_us"] != 2000:
            raise AssertionError("sweep block has the wrong time base")
        # The shorter sweep is padded with silence, not with a repeat.
        n_b = len(items[1]["trace"])
        if np.any(sweep_block["data"][1, n_b:] != 0.0):
            raise AssertionError("short trace was not zero-padded")

        for name, block, n_exp, t0_exp in (
                ("full", full, n_want, -100 * 0.002),
                ("half", half, n_want, 0.0)):
            if block["data"].shape != (2, n_exp):
                raise AssertionError(f"{name} wavelet is "
                                      f"{block['data'].shape}, expected (2, {n_exp})")
            if abs(block["t0_s"] - t0_exp) > 1e-12:
                raise AssertionError(f"{name} wavelet starts at "
                                      f"{block['t0_s']}, expected {t0_exp}")
        # Half is exactly the positive side of full: that is the claim the
        # option makes, and the reason it is safe to halve the file. Note
        # the sample count means the SAME thing in both -- samples written
        # -- so the same count reaches twice as far in lag on a half
        # wavelet, and only full's positive side is comparable.
        mid = (n_want - 1) // 2
        if not np.allclose(half["data"][:, :mid + 1], full["data"][:, mid:],
                            rtol=0, atol=1e-12):
            raise AssertionError("half wavelet is not the positive half of full")
        # ...and full is symmetric about that centre.
        if not np.allclose(full["data"][:, :mid], full["data"][:, mid + 1:][:, ::-1],
                            rtol=1e-9, atol=1e-9):
            raise AssertionError("full wavelet is not symmetric about zero lag")
        # An even count is rounded up so the peak gets its own sample.
        if assemble_traces(items, "wavelet", "raw", "full", 200)["n_samples"] != 201:
            raise AssertionError("even sample count was not rounded up")
        # One shared divisor, so the quieter sweep stays quieter.
        peak = float(np.max(np.abs(full["data"])))
        if abs(norm["scale"] - peak) > peak * 1e-12:
            raise AssertionError("normalization did not use the set's peak")
        if not np.allclose(norm["data"] * norm["scale"], full["data"],
                            rtol=1e-9, atol=1e-12):
            raise AssertionError("normalized set does not scale back")
        if abs(np.max(np.abs(norm["data"])) - 1.0) > 1e-12:
            raise AssertionError("normalized set does not peak at 1")

        # Sweeps at different sample intervals cannot share a file, and
        # have to say so rather than being silently resampled.
        mixed = [items[0], dict(items[1], params=SweepParams(label="C", dt_ms=1.0))]
        try:
            assemble_traces(mixed, "sweep", "raw", "full", n_want)
        except ValueError as exc:
            if "sample interval" not in str(exc):
                raise AssertionError(f"wrong error for mixed rates: {exc}")
        else:
            raise AssertionError("mixed sample intervals were accepted")

        head = _wrap_header(
            [f"Sweep Design {APP_VERSION} -- exported traces",
             "Content: the autocorrelation of the sweep -- full and symmetric",
             "", "Trace 1: A", f"  {describe_params(pa)}"], 76, limit=40)
        fields = full["headers"]

        with tempfile.TemporaryDirectory() as tmp:
            for fmt_name, fmt_code, _tip in SEGY_SAMPLE_FORMATS:
                out = os.path.join(tmp, f"selftest_{fmt_name}.sgy")
                delay = round(full["t0_s"] * 1000.0)
                write_segy(out, full["data"], full["dt_us"],
                            format_code=fmt_code, delay_ms=delay,
                            text_lines=head, trace_fields=fields,
                            extra_binary=_binary_sweep_fields(fields))
                want_size = 3600 + 2 * (240 + 4 * n_want)
                if os.path.getsize(out) != want_size:
                    raise AssertionError(
                        f"{fmt_name} file is {os.path.getsize(out)} bytes, "
                        f"expected {want_size}")
                text, binary, traces = read_segy(out)
                if not text.startswith("C 1 SWEEP DESIGN"):
                    raise AssertionError(f"textual header starts {text[:20]!r}")
                if len(text) != 3200:
                    raise AssertionError(f"textual header is {len(text)} chars")
                for key, want in (("format", fmt_code), ("hns", n_want),
                                   ("hdt", 2000), ("ntrpr", 2), ("rev", 0x0100),
                                   ("fixed", 1), ("hsfs", 6), ("hsfe", 96),
                                   ("hslen", 4000), ("hcorr", 2)):
                    if binary[key] != want:
                        raise AssertionError(f"binary header {key} = "
                                              f"{binary[key]}, expected {want}")
                if len(traces) != 2:
                    raise AssertionError(f"read {len(traces)} traces, expected 2")
                # Tolerance follows the format: IEEE is exact to float32,
                # IBM carries as few as 21 mantissa bits.
                tol = 1e-6 if fmt_code == 1 else 1e-7
                for i, (hdr, samples) in enumerate(traces):
                    ref = full["data"][i]
                    scale = max(float(np.max(np.abs(ref))), 1e-300)
                    if np.max(np.abs(samples - ref)) > tol * scale:
                        raise AssertionError(
                            f"{fmt_name} trace {i + 1} differs by "
                            f"{np.max(np.abs(samples - ref)):.3e}")
                    for key, want in (("ns", n_want), ("dt", 2000),
                                       ("delrt", delay), ("trid", 1),
                                       ("corr", 2), ("tracl", i + 1),
                                       ("sfs", round(items[i]["params"].f1)),
                                       ("sfe", round(items[i]["params"].f2)),
                                       ("styp", sweep_type_code(
                                           items[i]["params"].sweep_type))):
                        if hdr[key] != want:
                            raise AssertionError(
                                f"{fmt_name} trace {i + 1} header {key} = "
                                f"{hdr[key]}, expected {want}")
                if traces[0][0]["delrt"] >= 0:
                    raise AssertionError("a symmetric wavelet needs a negative "
                                          "delay recording time")

            # ASCII: it has to load with nothing but numpy and no arguments.
            txt = os.path.join(tmp, "selftest.txt")
            write_ascii(txt, full["data"], full["dt_ms"], full["t0_s"] * 1000.0,
                        comment_lines=_wrap_header(
                            ["Sweep Design -- exported traces",
                             "Trace 1: A", "Trace 2: B"], 96))
            table = np.loadtxt(txt)
            if table.shape != (n_want, 3):
                raise AssertionError(f"ASCII table is {table.shape}, "
                                      f"expected ({n_want}, 3)")
            # In MILLISECONDS, to agree with every other time this program
            # prints -- and with the SEG-Y delay field written above.
            want_t = full["t0_s"] * 1000.0 + np.arange(n_want) * full["dt_ms"]
            if not np.allclose(table[:, 0], want_t, atol=1e-6):
                raise AssertionError(
                    f"ASCII time column is wrong: starts {table[0, 0]}, "
                    f"expected {want_t[0]}")
            if abs(table[0, 0] - delay) > 1e-6:
                raise AssertionError(
                    f"ASCII start {table[0, 0]} ms disagrees with the SEG-Y "
                    f"delay recording time {delay} ms")
            if not np.allclose(table[:, 1:].T, full["data"], rtol=1e-5,
                                atol=1e-5 * peak):
                raise AssertionError("ASCII amplitudes do not match")
            with open(txt, encoding="utf-8") as fh:
                body = fh.read()
            if "Trace 2: B" not in body or "time_ms" not in body:
                raise AssertionError("ASCII comment block is incomplete")

            # Petrel: one wavelet per file, and the trap is the time
            # column -- it is a 0-BASED OFFSET, with the real start time
            # carried once in WAVELET-TFS. Getting that backwards puts the
            # zero-lag peak half a wavelet late in whatever loads it, and
            # nothing in the file looks wrong when it happens.
            wlt = os.path.join(tmp, "selftest.wlt")
            write_petrel_wavelet(
                wlt, full["data"][1], full["dt_ms"], full["t0_s"] * 1000.0,
                "B [wavelet]",
                description=["Sweep Design -- exported traces",
                             "", "  indented detail line"],
                history=["2026-01-01 00:00  Created"])
            hdr, offs, amps = read_petrel_wavelet(wlt)
            if hdr["WAVELET-NAME"] != ["B [wavelet]"]:
                raise AssertionError(f"Petrel name is {hdr.get('WAVELET-NAME')}")
            if abs(float(hdr["WAVELET-TFS"][0]) - full["t0_s"] * 1000.0) > 1e-6:
                raise AssertionError(
                    f"Petrel WAVELET-TFS is {hdr['WAVELET-TFS'][0]}, expected "
                    f"{full['t0_s'] * 1000.0}")
            if abs(float(hdr["SAMPLE-RATE"][0]) - full["dt_ms"]) > 1e-9:
                raise AssertionError(
                    f"Petrel SAMPLE-RATE is {hdr['SAMPLE-RATE'][0]} ms, "
                    f"expected {full['dt_ms']}")
            if not np.allclose(offs, np.arange(n_want) * full["dt_ms"],
                                atol=1e-6):
                raise AssertionError(
                    f"Petrel time column is not a 0-based offset: it starts "
                    f"at {offs[0]} and steps {offs[1] - offs[0]}")
            if not np.allclose(amps, full["data"][1], rtol=1e-5,
                                atol=1e-5 * peak):
                raise AssertionError("Petrel amplitudes do not match")
            # TFS plus the offset has to land back on the real time axis --
            # the same axis SEG-Y's delay field and the ASCII column use.
            if abs(float(hdr["WAVELET-TFS"][0]) + offs[0] - delay) > 1e-6:
                raise AssertionError(
                    "Petrel TFS + offset disagrees with the SEG-Y delay")
            # The description survives with its indent, and the blank
            # separator line is dropped rather than breaking the block.
            if hdr["WAVELET-DESC"] != ["Sweep Design -- exported traces",
                                       "  indented detail line"]:
                raise AssertionError(
                    f"Petrel description came back as {hdr['WAVELET-DESC']}")
            with open(wlt, encoding="utf-8") as fh:
                wtext = fh.read()
            for marker in ("EOH\n", "EOD\n", "HISTORY"):
                if marker not in wtext:
                    raise AssertionError(f"Petrel file has no {marker.strip()}")
            if wtext.rstrip().splitlines()[-1] != "EOD":
                raise AssertionError("Petrel file does not end with EOD")

        return (f"{n_want}-sample wavelet, 2 traces, IBM+IEEE SEG-Y read "
                f"back within {1e-6:.0e}, ASCII loads with numpy, Petrel "
                f"wavelet round-trips with TFS on the SEG-Y time axis")

    check("trace export", trace_export)

    # One figure, saved through every format the Export button offers.
    fig = Figure(figsize=(4, 3), dpi=BASE_DPI)
    ax = fig.add_subplot(111)
    ax.plot([0, 1, 2], [0, 1, 0])
    ax.set_title("self-test")
    fig.text(0.5, 0.02, "figure-level artist", ha="center")

    with tempfile.TemporaryDirectory() as tmp:
        for fmt, backend, _tip in EXPORT_FORMATS:
            def one(fmt=fmt, backend=backend, tmp=tmp):
                __import__(backend)          # the import savefig defers
                out = os.path.join(tmp, f"selftest.{fmt}")
                fig.savefig(out, format=fmt)
                size = os.path.getsize(out)
                if size < 512:
                    raise AssertionError(f"{out} is only {size} bytes")
                head = open(out, "rb").read(8)
                if fmt == "png" and not head.startswith(b"\x89PNG"):
                    raise AssertionError(f"not a PNG: {head!r}")
                if fmt == "svg" and not head.lstrip().startswith(b"<"):
                    raise AssertionError(f"not an SVG: {head!r}")
                return f"{size} bytes, {backend}"
            check(f"export {fmt}", one)

    lines.append("FAILED" if failed else "ALL PASSED")
    report = "\n".join(lines)
    print(report)
    if report_path:
        try:
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write(report + "\n")
        except OSError as exc:
            print(f"(could not write {report_path}: {exc})")
    return 1 if failed else 0


def main():
    # A packaged build has no console, so --selftest takes an optional path to
    # write its report to. It must run before any window exists.
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        rest = sys.argv[i + 1:]
        sys.exit(_selftest(rest[0] if rest else None))

    # Before the first window exists: Windows fixes a process's DPI awareness
    # at the moment it starts drawing, so this cannot be deferred.
    _declare_dpi_aware()
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass
    app = SweepDesignApp(root)
    root.protocol("WM_DELETE_WINDOW", app._on_exit)
    root.mainloop()


if __name__ == "__main__":
    main()

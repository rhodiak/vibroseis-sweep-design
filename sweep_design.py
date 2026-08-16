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

import json
import os
import sys

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


APP_DIR = _app_dir()
STATE_FILE = os.path.join(APP_DIR, "sweep_design_state.json")
STATE_VERSION = 1

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

PANELS = ["signal", "freq", "spec", "corr", "env"]
PANEL_TITLES = {
    "signal": "Signal", "freq": "Frequency vs Time", "spec": "Amplitude Spectrum",
    "corr": "Autocorrelation", "env": "Correlation Envelope",
}

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

APP_VERSION = "1.0"

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
    "candidates -- Linear, dB/Octave, dB/Hz, T-power, Random, and Pulse "
    "types -- across five panels: time-domain signal, instantaneous "
    "frequency vs time, amplitude spectrum, autocorrelation, and "
    "correlation envelope.\n\n"
    "Also supports theoretical (noiseless) stacking: a stationary n-fold "
    "stack (pure amplitude gain, no shape change) or a spaced source "
    "array evaluated against a chosen apparent velocity, which applies a "
    "genuine frequency-dependent array response.\n\n"
    "Spectrum / autocorrelation / envelope panels are normalized once, "
    "using a single reference shared across every sweep on the plot, so "
    "relative amplitude differences from duration, drive level, and "
    "array geometry stay meaningful rather than being hidden by "
    "per-trace normalization. The spectrum and the correlation both carry "
    "a dt factor, so they measure spectral density and energy rather than "
    "bare sums over samples: re-reading the same sweep at a finer sample "
    "rate does not move its level. (A bare sum would rise 6 dB every time "
    "dt is halved, purely because there are twice as many samples, and "
    "would read as energy that was never there.)\n\n"
    "The legend names each sweep's parameters; the measured properties of "
    "its correlation wavelet are tabulated under the plots, one row per "
    "sweep, colour-keyed to the traces: pk (autocorrelation peak, dB "
    "relative to the strongest sweep on the plot, as an amplitude "
    "ratio so it matches the heights in the autocorrelation panel: "
    "doubling the sweep length is +6 dB of correlated signal, though "
    "only about 3 dB of signal-to-noise, since random noise "
    "correlates up as the square root of length), "
    "SLL (first side-lobe "
    "level below the peak), P/T (deepest trough below the peak), MLW "
    "(main-lobe width at half amplitude, ms), "
    "ISLR (integrated side-lobe ratio), T40dB (ringing length: how far "
    "from the peak the envelope is still above -40 dB), decay (how fast "
    "the side-lobe crests fall, dB per 100 ms) and BW (achieved -6 dB "
    "band). Lower SLL/ISLR means less correlation ringing; lower MLW "
    "means sharper resolution -- longer tapers trade the second for the "
    "first. P/T and SLL are not the same measurement and rarely agree: "
    "SLL reads the envelope between lobes and comes out near -13 dB for "
    "almost any bandpass sweep, while P/T reads the signed wavelet and "
    "finds the trough at the first carrier half cycle, inside the "
    "envelope's main lobe. P/T is therefore the one that tracks "
    "bandwidth -- about -10 dB over four octaves, -1.5 dB over one, and "
    "near 0 dB for a narrow sweep whose correlation is a ringing cosine "
    "with troughs as deep as its peak. It is also what prices a long "
    "taper, which narrows the effective band. A Ricker wavelet sits at "
    "-7.0 dB for comparison. "
    "T40dB and decay are measured against each sweep's own peak, "
    "so read them together with pk when comparing traces on the shared-"
    "reference plot. A key to the same abbreviations is drawn on the canvas "
    "itself, top right, so exported images explain their own numbers.\n\n"
    "Sweep parameters and axis-range/zoom settings are auto-saved on exit "
    f"and restored on the next launch (in {os.path.basename(STATE_FILE)}, "
    "kept beside the app so the folder stays self-contained).\n\n"
    "See README.md, bundled with the app, for the full physics notes and "
    "assumptions behind each sweep type and the stacking model.\n\n"
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
                             f"automatically on Exit. File: {STATE_FILE}")
        self._tip(load_btn, "Reload the most recently saved sweep parameters "
                             "and axis-range/zoom settings, discarding any "
                             "unsaved edits on the Sweep and Zoom tabs.")

        btn_row = ttk.Frame(footer)
        btn_row.pack(fill=tk.X)
        about_btn = ttk.Button(btn_row, text="About", command=self._show_about)
        about_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 4))
        exit_btn = ttk.Button(btn_row, text="Exit", command=self._on_exit)
        exit_btn.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self._tip(about_btn, "About this application: what it does, the "
                              "normalization/stacking conventions it uses, "
                              "and where to find the full physics notes.")
        self._tip(exit_btn, "Close Sweep Design. "
                             "Auto-saves settings first; asks for "
                             "confirmation if you have unexported sweeps "
                             "on the plot.")

        self.notebook = ttk.Notebook(self.left)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        tab_sweep = ttk.Frame(self.notebook, padding=6)
        tab_zoom = ttk.Frame(self.notebook, padding=6)
        self.notebook.add(tab_sweep, text="Sweep")
        self.notebook.add(tab_zoom, text="Axis ranges / zoom")

        self._build_sweep_tab(tab_sweep)
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
            f"Settings file: {STATE_FILE}"
        )

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
                    "(SVG/PNG) will be lost. Sweep parameters and axis-"
                    "range/zoom settings will be saved for next time."):
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
                self.status.config(text=f"Settings saved to {STATE_FILE}")
        except OSError as exc:
            if notify:
                messagebox.showerror("Save failed", str(exc))

    def _load_state(self, notify=True):
        if not os.path.exists(STATE_FILE):
            if notify:
                messagebox.showinfo("No saved settings",
                                     f"No saved settings file found at {STATE_FILE}.")
            return
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            self._apply_state(data)
            if notify:
                self.status.config(text=f"Settings loaded from {STATE_FILE}")
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
                          "(in a new color) on all 5 panels. Also adds the "
                          "stacked/array version if that checkbox is on.")
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
        # Driven by EXPORT_FORMATS so the buttons, the frozen build's bundled
        # backends and --selftest cannot drift apart. See that constant.
        for fmt, _backend, tip in EXPORT_FORMATS:
            rb = ttk.Radiobutton(fmt_row, text=fmt.upper(),
                                  variable=self.export_fmt, value=fmt.upper())
            rb.pack(side=tk.LEFT)
            self._tip(rb, tip)
        b_export = ttk.Button(p, text="Export figure...", command=self._export)
        b_export.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4); row += 1
        self._tip(b_export, "Save the current overlay (all 5 panels, current "
                             "zoom) to disk in the selected format.")

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

    def _build_zoom_tab(self, p):
        ttk.Label(p, text="Explicit axis ranges (blank = auto)", font=("", 10, "bold")).grid(
            row=0, column=0, columnspan=5, sticky="w", pady=(0, 6))
        headers = ["Panel", "X min", "X max", "Y min", "Y max"]
        for c, h in enumerate(headers):
            ttk.Label(p, text=h, font=("", 9, "bold")).grid(row=1, column=c, padx=2)

        for i, key in enumerate(PANELS):
            r = 2 + i
            ttk.Label(p, text=PANEL_TITLES[key]).grid(row=r, column=0, sticky="w", pady=2)
            self.range_vars[key] = {}
            for c, field in enumerate(["xmin", "xmax", "ymin", "ymax"], start=1):
                v = tk.StringVar(value="")
                e = ttk.Entry(p, textvariable=v, width=8)
                e.grid(row=r, column=c, padx=2, pady=2)
                self.range_vars[key][field] = v

        btn_row = ttk.Frame(p)
        btn_row.grid(row=2 + len(PANELS), column=0, columnspan=5, pady=10, sticky="ew")
        b_apply = ttk.Button(btn_row, text="Apply zoom", command=self._redraw)
        b_apply.pack(fill=tk.X, pady=2)
        self._tip(b_apply, "Redraw all 5 panels using the ranges entered above "
                            "(blank fields stay auto-scaled).")
        b_reset = ttk.Button(btn_row, text="Reset all to auto", command=self._reset_zoom)
        b_reset.pack(fill=tk.X, pady=2)
        self._tip(b_reset, "Clear every range field and let all panels "
                            "auto-scale again.")

        ttk.Label(p, text="Tip: leave a field blank to auto-scale that "
                           "bound. Apply zoom re-draws with current sweeps.",
                  foreground="#555", wraplength=280).grid(
            row=3 + len(PANELS), column=0, columnspan=5, sticky="w", pady=(4, 0))

    def _reset_zoom(self):
        for key in PANELS:
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
        # ax_info is deliberately NOT in self.axes: that dict drives clearing
        # on replot and the axis-range/zoom tab, and the key is static text.
        self.axes = {"signal": self.ax_signal, "freq": self.ax_freq, "spec": self.ax_spec,
                     "corr": self.ax_corr, "env": self.ax_env}
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

        self._build_glossary()

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

    def _build_glossary(self):
        """Draw the metrics key into the top-right panel. Built once (the text
        never changes) and only re-sized on resize -- see _fit_glossary."""
        ax = self.ax_info
        ax.set_title(GLOSSARY_TITLE, fontsize=12)
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_facecolor("#fbfbfb")
        for spine in ax.spines.values():
            spine.set_color("#c9c9c9")
        # It holds text, not data -- keep the toolbar's zoom/pan off it, or a
        # stray drag scrolls the key out of its own frame.
        ax.set_navigate(False)

        # Positions are left at zero here: both the point size and the row
        # spacing are solved in _fit_glossary against the panel's actual pixel
        # size, so there is nothing meaningful to place until then.
        self._gloss_abbr, self._gloss_desc = [], []
        for abbr, desc in GLOSSARY_ROWS:
            self._gloss_abbr.append(ax.text(
                0.0, 0.0, abbr, transform=ax.transAxes, va="top", ha="left",
                fontweight="bold", color="#1a1a1a"))
            self._gloss_desc.append(ax.text(
                0.0, 0.0, desc, transform=ax.transAxes, va="top", ha="left",
                color="#333333"))
        # One Text per sentence, each wrapped on its own, so the second always
        # begins on a fresh line instead of running on from the first.
        self._gloss_notes = [
            ax.text(0.0, 0.0, s, transform=ax.transAxes, va="top", ha="left",
                    color="#5f5f5f", style="italic", linespacing=1.3)
            for s in GLOSSARY_NOTE
        ]

    def _fit_glossary(self, w_px):
        """Size and lay out the key to fill its panel.

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
        pos = self.ax_info.get_position()
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
            for text in self._gloss_abbr + self._gloss_desc:
                text.set_fontsize(fs)
            for text, sentence in zip(self._gloss_notes, GLOSSARY_NOTE):
                text.set_fontsize(fs * 0.92)
                text.set_text(self._wrap_text(sentence, text, renderer, avail_w))

            # Descriptions start just right of the widest abbreviation, so the
            # gutter tracks the font instead of being a fixed fraction that
            # goes gappy at one size and cramped at another.
            widest = max(t.get_window_extent(renderer).width for t in self._gloss_abbr)
            col = pad + (widest + 0.9 * fs * pt_px) / panel_w
            desc_w = max(t.get_window_extent(renderer).width for t in self._gloss_desc)

            row_px = fs * 1.65 * pt_px
            note_px = fs * 0.92 * 1.3 * pt_px
            gap_px = row_px * 0.85
            n_note = sum(t.get_text().count("\n") + 1 for t in self._gloss_notes)
            total_h = len(GLOSSARY_ROWS) * row_px
            if show_note:
                total_h += gap_px + n_note * note_px

            fits = (col * panel_w + desc_w <= panel_w * (1.0 - pad)
                    and total_h <= panel_h * 0.93)
            return fits, col, row_px, gap_px, note_px

        def solve(show_note):
            for text in self._gloss_notes:
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
            # 120 px). The abbreviation rows are the part that has to survive,
            # so the closing note steps aside rather than spilling out of frame.
            fs, fits, col, row_px, gap_px, note_px = solve(False)

        y = 0.96
        for i, (abbr, desc) in enumerate(zip(self._gloss_abbr, self._gloss_desc)):
            row_y = y - i * row_px / panel_h
            abbr.set_position((pad, row_y))
            desc.set_position((col, row_y))
        note_y = y - (len(GLOSSARY_ROWS) * row_px + gap_px) / panel_h
        for text in self._gloss_notes:
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
    def _build_table(self, corr_ref):
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
            vals = metric_values(r.get("metrics") or {}, corr_ref)
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

        self.results.append({
            "params": p, "t": r["t"], "signal": sig, "inst_freq": r["inst_freq"],
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

        # After the panels move: the key's text is sized against the panel it
        # now occupies.
        self._fit_glossary(w_px)

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

    def _redraw(self):
        for ax in self.axes.values():
            ax.clear()

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

        for ax in self.axes.values():
            ax.tick_params(axis="both", labelsize=9)

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

        for ax in self.axes.values():
            ax.grid(alpha=0.3)
        for key, ax in self.axes.items():
            self._apply_axis_range(key, ax)

        self._rebuild_legend()
        self.canvas.draw_idle()

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

        handles, labels = self.ax_signal.get_legend_handles_labels()
        n = len(labels)
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
    import tempfile
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
    lines.append(f"state file would be: {STATE_FILE}")

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

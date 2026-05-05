#!/usr/bin/env python3
"""
BestTrack Photo Finish Timer Controller
Iowa Children's Museum -- Race Track

Hardware:
  - Raspberry Pi (32-bit Raspbian)
  - Solenoid gate release via relay board (active LOW)
  - Two BestTrack photo-finish timers via USB serial adapters

Serial protocol (reverse-engineered BestTrack):
  ra\\r  -> Read all lane results
            Response: \\n1=0.6149f 2=0.4780e 3=0.4144d 4=0.3713c 5=0.2348b 6=0.000a\\r
            Place suffix: a=1st  b=2nd  c=3rd  d=4th  e=5th  f=6th
  rl\\r  -> Read lane blocking status
            Response: \\n000000\\r  (0x30=clear, 0x31=blocked per lane position)

Race sequence:
  1. Operator presses button -> solenoid fires -> cars release
  2. RACE_WAIT seconds countdown while cars travel the track
  3. 'ra' command sent to both timers -> results parsed and displayed
"""

import re
import sys
import time
import threading
import tkinter as tk

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    print("WARNING: pyserial not installed. Running without serial.")
    SERIAL_AVAILABLE = False

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except (ImportError, RuntimeError):
    print("WARNING: RPi.GPIO not available. Running in display-only mode.")
    GPIO_AVAILABLE = False

# ===== Configuration =========================================================

BUTTON_PIN   = 40              # Physical BOARD pin, active LOW (has pull-up)
RELAY_PINS   = [7, 11, 13, 15] # Solenoid relay pins -- LOW = energized
RELAY_ON_SEC = 0.5             # How long to hold the solenoid (seconds)
RACE_WAIT    = 4.5             # Seconds after gate opens before reading results

# Stable USB-port paths so Timer 1 and Timer 2 don't swap on reboot.
# These are tied to the physical USB jack on the Pi, not to the adapter
# itself.  The Prolific PL2303 adapters in use don't carry unique serial
# numbers, so by-id can't disambiguate them -- we use by-path instead.
#
# Mapping (label these jacks on the Pi):
#   T1 -> physical port 1.1.3
#   T2 -> physical port 1.1.2
#
# To verify after a re-cabling, run on the Pi:
#   ls -l /dev/serial/by-path/
SERIAL_PORT_1 = "/dev/serial/by-path/platform-3f980000.usb-usb-0:1.1.3:1.0-port0"
SERIAL_PORT_2 = "/dev/serial/by-path/platform-3f980000.usb-usb-0:1.1.2:1.0-port0"
BAUD_RATE     = 9600

NUM_LANES = 6

# BestTrack returns 9.9999 when a lane never tripped a sensor (timer
# timed out).  Any time at or above this threshold is treated as "no result"
# and displayed as '---' instead of a real-looking number.
NO_RESULT_THRESHOLD = 9.99

# Mapping from BestTrack place suffix to ordinal string
PLACE_LABEL = {
    'a': '1st', 'b': '2nd', 'c': '3rd',
    'd': '4th', 'e': '5th', 'f': '6th',
}

# Colors for each place (gold -> silver -> bronze -> ...)
PLACE_COLOR = {
    '1st': '#FFD700',  # gold
    '2nd': '#C0C0C0',  # silver
    '3rd': '#CD7F32',  # bronze
    '4th': '#4DA6FF',  # blue
    '5th': '#CC66FF',  # purple
    '6th': '#FF6666',  # red
    ' ':   '#555566',  # placeholder
}

# Dark-mode palette
BG          = '#0d0d1a'
HEADER_BG   = '#1a1a3e'
HEADER_FG   = '#e8e8ff'
STATUS_BG   = '#111128'
STATUS_FG   = '#88aadd'
LANE_BG     = '#1a1a3e'
LANE_FG     = '#ccddff'
CELL_BG     = '#0d1b2e'
TIME_FG     = '#e8e8f8'
T1_LABEL_FG = '#88bbff'
T2_LABEL_FG = '#ffbb88'
DIM_FG      = '#444455'


# ===== BestTrack serial timer ================================================

class BestTrackTimer:
    """Wraps one BestTrack timer serial port."""

    def __init__(self, port, baud=BAUD_RATE):
        self.port      = port
        self.connected = False
        self._ser      = None
        if SERIAL_AVAILABLE:
            self._connect(baud)

    def _connect(self, baud):
        try:
            self._ser      = serial.Serial(self.port, baud, timeout=1)
            self.connected = True
            print("Timer connected: " + self.port)
        except Exception as exc:
            print("Timer not found on " + self.port + ": " + str(exc))

    def reset(self):
        """
        Send a reset/restart command before each race.
        BestTrack units vary; 'rf\\r' is a common reset.
        If your timer doesn't respond well to this, comment out the write line.
        """
        if not self.connected:
            return
        try:
            self._ser.reset_input_buffer()
            self._ser.write(b"rf\r")
            time.sleep(0.1)
        except Exception:
            pass

    def read_results(self):
        """
        Send 'ra\\r', parse response, return dict keyed by lane number 1-6.
        Each value: {'time': '0.6149', 'place': '6th'}
        """
        empty = {i: {'time': '---', 'place': ' '} for i in range(1, NUM_LANES + 1)}
        if not self.connected:
            return empty
        try:
            self._ser.reset_input_buffer()
            self._ser.write(b"ra\r")
            raw = self._ser.read(128).decode(errors="ignore")
            return self._parse(raw) or empty
        except Exception as exc:
            print("Serial read error on " + self.port + ": " + str(exc))
            return empty

    @staticmethod
    def _parse(raw):
        """
        Parse BestTrack 'ra' response.
        Handles both '1=0.6149f' and '1 = 0.6149f' spacing variants.

        BestTrack returns 9.9999 for any lane that didn't trip a sensor
        (the timer's max/timeout value).  We map those to '---' so the
        display clearly reads "no result" instead of a real-looking time.
        """
        results = {}
        # Matches:  lane_digit [spaces] = [spaces] digits.digits [optional_letter]
        pattern = re.compile(r'([1-6])\s*=\s*([\d.]+)([a-f]?)', re.IGNORECASE)
        for m in pattern.finditer(raw):
            lane         = int(m.group(1))
            time_str     = m.group(2)[:7]
            place_letter = m.group(3).lower() if m.group(3) else ' '

            # Detect timer's "no car detected" sentinel value (9.9999).
            try:
                if float(time_str) >= NO_RESULT_THRESHOLD:
                    results[lane] = {'time': '---', 'place': ' '}
                    continue
            except ValueError:
                pass

            results[lane] = {
                'time':  time_str,
                'place': PLACE_LABEL.get(place_letter, ' '),
            }
        for lane in range(1, NUM_LANES + 1):
            results.setdefault(lane, {'time': '---', 'place': ' '})
        return results

    def check_blocked(self):
        """Return True if any lane sensor is reporting a blockage."""
        if not self.connected:
            return False
        try:
            self._ser.reset_input_buffer()
            self._ser.write(b"rl\r")
            line = self._ser.read(16).decode(errors="ignore").strip()
            return '1' in line   # 0x31 = blocked lane
        except Exception:
            return False

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()


# ===== GPIO solenoid controller ==============================================

class GateController:
    """Manages button input and solenoid relay output."""

    def __init__(self):
        self._available = GPIO_AVAILABLE
        if not self._available:
            return
        GPIO.setmode(GPIO.BOARD)
        GPIO.setwarnings(False)
        GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        for pin in RELAY_PINS:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)  # HIGH = relay off

    def button_pressed(self):
        if not self._available:
            return False
        return GPIO.input(BUTTON_PIN) == GPIO.LOW

    def fire(self):
        """Pulse all relay pins to release the solenoid gate."""
        if not self._available:
            print("[DEMO] Solenoid fired")
            return
        for pin in RELAY_PINS:
            GPIO.output(pin, GPIO.LOW)    # energize
        time.sleep(RELAY_ON_SEC)
        for pin in RELAY_PINS:
            GPIO.output(pin, GPIO.HIGH)   # de-energize

    def cleanup(self):
        if self._available:
            GPIO.cleanup()


# ===== Tkinter full-screen display ===========================================

class RaceApp:
    """Full-screen Tkinter race result display."""

    def __init__(self, root, gate, timer1, timer2):
        self.root   = root
        self.gate   = gate
        self.timer1 = timer1
        self.timer2 = timer2
        self._racing = False

        # --- Kiosk-mode setup --------------------------------------------------
        # Multiple layers because window managers vary and we cannot tolerate
        # a stray window appearing in a museum.
        root.configure(bg=BG)
        root.title("Photo Finish")
        try:
            root.attributes("-fullscreen", True)
        except tk.TclError:
            pass
        # Hide the mouse cursor so kids cannot smear or fixate on it.
        root.config(cursor="none")
        # Force window to top and grab focus.
        root.attributes("-topmost", True)
        root.focus_force()
        # Re-assert fullscreen after the WM has had a chance to do its thing.
        # Some WMs (LXDE/Openbox especially) honor it on the second try.
        root.after(200,  lambda: root.attributes("-fullscreen", True))
        root.after(1000, lambda: root.attributes("-fullscreen", True))

        # --- Key bindings ------------------------------------------------------
        # Hidden admin exit: Ctrl+Shift+Q (kids cannot find this by accident).
        # Escape and 'q' alone are intentionally NOT bound, so curious fingers
        # on a keyboard cannot kill the kiosk.
        root.bind("<Control-Shift-Q>", self._quit)
        root.bind("<Control-Shift-q>", self._quit)
        # Dev helper: F5 simulates a race without GPIO.
        root.bind("<F5>", lambda _: self._simulate_race())
        # Swallow Alt-F4 so a connected keyboard cannot close the window.
        root.protocol("WM_DELETE_WINDOW", lambda: None)

        self._build_ui()
        self._poll()

    # --- scaling -------------------------------------------------------------

    def _s(self, base):
        """Scale a font/pixel size relative to 768 px reference height."""
        h = self.root.winfo_screenheight() or 768
        return max(8, int(base * h / 768))

    # --- UI construction -----------------------------------------------------

    def _build_ui(self):
        self.root.update_idletasks()
        s = self._s

        # --- title bar -------------------------------------------------------
        title_frame = tk.Frame(self.root, bg=HEADER_BG)
        title_frame.pack(fill=tk.X, ipady=s(6))

        t1_color = '#44ff44' if self.timer1.connected else '#ff4444'
        tk.Label(title_frame, text="*",
                 font=("Helvetica", s(18), "bold"),
                 bg=HEADER_BG, fg=t1_color).pack(side=tk.LEFT, padx=(s(16), 0))
        tk.Label(title_frame, text="T1",
                 font=("Helvetica", s(12)), bg=HEADER_BG,
                 fg=T1_LABEL_FG).pack(side=tk.LEFT, padx=(2, s(16)))

        tk.Label(title_frame, text="PHOTO  FINISH",
                 font=("Helvetica", s(34), "bold"),
                 bg=HEADER_BG, fg=HEADER_FG).pack(side=tk.LEFT, expand=True)

        t2_color = '#44ff44' if self.timer2.connected else '#ff4444'
        tk.Label(title_frame, text="T2",
                 font=("Helvetica", s(12)), bg=HEADER_BG,
                 fg=T2_LABEL_FG).pack(side=tk.RIGHT, padx=(2, s(16)))
        tk.Label(title_frame, text="*",
                 font=("Helvetica", s(18), "bold"),
                 bg=HEADER_BG, fg=t2_color).pack(side=tk.RIGHT, padx=(0, 2))

        # --- status bar ------------------------------------------------------
        self._status_var = tk.StringVar(
            value="Ready -- press the button to start a race"
        )
        status_frame = tk.Frame(self.root, bg=STATUS_BG)
        status_frame.pack(fill=tk.X, ipady=s(4))
        tk.Label(status_frame, textvariable=self._status_var,
                 font=("Helvetica", s(18)),
                 bg=STATUS_BG, fg=STATUS_FG).pack(expand=True)

        # --- results grid ----------------------------------------------------
        grid = tk.Frame(self.root, bg=BG)
        grid.pack(fill=tk.BOTH, expand=True, padx=s(8), pady=s(6))

        # column weights: 0=row-label, 1-6=lanes
        grid.columnconfigure(0, weight=2)
        for c in range(1, NUM_LANES + 1):
            grid.columnconfigure(c, weight=3)

        # row weights
        for r in range(5):
            grid.rowconfigure(r, weight=1)

        pad = {'padx': s(3), 'pady': s(3)}

        # lane header row
        tk.Label(grid, text="", bg=BG).grid(row=0, column=0, sticky="nsew", **pad)
        for lane in range(1, NUM_LANES + 1):
            f = tk.Frame(grid, bg=LANE_BG)
            f.grid(row=0, column=lane, sticky="nsew", **pad)
            tk.Label(f, text="Lane " + str(lane),
                     font=("Helvetica", s(22), "bold"),
                     bg=LANE_BG, fg=LANE_FG).pack(expand=True, fill=tk.BOTH)

        # Timer 1 label (spans time + place rows)
        f = tk.Frame(grid, bg=CELL_BG)
        f.grid(row=1, column=0, rowspan=2, sticky="nsew", **pad)
        tk.Label(f, text="Timer\n1",
                 font=("Helvetica", s(22), "bold"),
                 bg=CELL_BG, fg=T1_LABEL_FG).pack(expand=True)

        # Timer 2 label
        f = tk.Frame(grid, bg=CELL_BG)
        f.grid(row=3, column=0, rowspan=2, sticky="nsew", **pad)
        tk.Label(f, text="Timer\n2",
                 font=("Helvetica", s(22), "bold"),
                 bg=CELL_BG, fg=T2_LABEL_FG).pack(expand=True)

        # data cell stores: {lane: {'time': Label, 'place': Label}}
        self._cells = {1: {}, 2: {}}

        time_font  = ("Helvetica", s(30), "bold")
        place_font = ("Helvetica", s(26), "bold")

        def make_data_rows(timer_idx, time_row, place_row):
            for lane in range(1, NUM_LANES + 1):
                # time cell
                tf = tk.Frame(grid, bg=CELL_BG)
                tf.grid(row=time_row, column=lane, sticky="nsew", **pad)
                t_lbl = tk.Label(tf, text="---",
                                 font=time_font, bg=CELL_BG, fg=TIME_FG)
                t_lbl.pack(expand=True, fill=tk.BOTH)

                # place cell
                pf = tk.Frame(grid, bg=CELL_BG)
                pf.grid(row=place_row, column=lane, sticky="nsew", **pad)
                p_lbl = tk.Label(pf, text="",
                                 font=place_font, bg=CELL_BG, fg=DIM_FG)
                p_lbl.pack(expand=True, fill=tk.BOTH)

                self._cells[timer_idx][lane] = {'time': t_lbl, 'place': p_lbl}

        make_data_rows(1, time_row=1, place_row=2)
        make_data_rows(2, time_row=3, place_row=4)

    # --- result update -------------------------------------------------------

    def _set_status(self, msg):
        self._status_var.set(msg)

    def _clear_results(self):
        for timer_idx in (1, 2):
            for lane in range(1, NUM_LANES + 1):
                cell = self._cells[timer_idx][lane]
                cell['time'].config(text="---", fg=TIME_FG)
                cell['place'].config(text="",   fg=DIM_FG)

    def _apply_results(self, results1, results2):
        for timer_idx, results in ((1, results1), (2, results2)):
            for lane in range(1, NUM_LANES + 1):
                data  = results[lane]
                cell  = self._cells[timer_idx][lane]
                place = data['place']
                cell['time'].config(text=data['time'])
                cell['place'].config(
                    text=place,
                    fg=PLACE_COLOR.get(place, DIM_FG),
                )

    # --- race sequence (runs in background thread) ---------------------------

    def _race_thread(self):
        self._racing = True
        self.root.after(0, self._clear_results)
        self.root.after(0, lambda: self._set_status("RACING..."))

        # Reset both timers so stale results don't linger
        # (comment out if your units don't support 'rf')
        self.timer1.reset()
        self.timer2.reset()
        time.sleep(0.05)

        # Fire solenoid to release cars
        self.gate.fire()

        # Countdown in status bar while cars travel the track
        start = time.time()
        while True:
            elapsed   = time.time() - start
            remaining = RACE_WAIT - elapsed
            if remaining <= 0:
                break
            self.root.after(
                0,
                lambda r=remaining: self._set_status(
                    "Racing...  " + ("%.1f" % r) + " s"
                )
            )
            time.sleep(0.25)

        # Read results from both timers
        self.root.after(0, lambda: self._set_status("Reading results..."))
        r1 = self.timer1.read_results()
        r2 = self.timer2.read_results()

        self.root.after(0, lambda: self._apply_results(r1, r2))
        self.root.after(0, lambda: self._set_status(
            "Results -- press button for next race"
        ))
        self._racing = False

    def _simulate_race(self):
        """Dev helper: press F5 to trigger a fake race without GPIO."""
        if self._racing:
            return
        threading.Thread(target=self._race_thread, daemon=True).start()

    # --- button polling ------------------------------------------------------

    def _poll(self):
        if not self._racing and self.gate.button_pressed():
            threading.Thread(target=self._race_thread, daemon=True).start()
        self.root.after(50, self._poll)

    # --- quit ----------------------------------------------------------------

    def _quit(self, _event=None):
        self.gate.cleanup()
        self.timer1.close()
        self.timer2.close()
        self.root.destroy()
        sys.exit(0)


# ===== entry point ===========================================================

def main():
    gate   = GateController()
    timer1 = BestTrackTimer(SERIAL_PORT_1)
    timer2 = BestTrackTimer(SERIAL_PORT_2)

    # Boot-time reset: clear any stale results the timers were holding
    # at power-on.  Without this, the very first race after boot can
    # display old times from before the kiosk was last shut down.
    print("Sending initial reset to both timers...")
    timer1.reset()
    timer2.reset()
    time.sleep(0.2)

    root = tk.Tk()
    root.title("Photo Finish")
    RaceApp(root, gate, timer1, timer2)
    root.mainloop()


if __name__ == "__main__":
    main()

"""
PAX1000 polarimeter driver
===========================

Thin wrapper around the Thorlabs PAX1000 SCPI command set, talking
over USBTMC via PyVISA. Only hard dependency: pyvisa.

PyVISA itself needs a VISA backend to actually open the USB device.
Two options, pick one:
  - NI-VISA (the official Thorlabs Kinesis / NI runtime), if you
    already have it installed - then plain `pip install pyvisa` is
    enough and nothing else changes here.
  - The pure-Python backend: `pip install pyvisa-py pyusb`. No NI-VISA
    needed, works directly against libusb.

If you'd rather plug in your own driver/transport (e.g. you already
have working PAX1000 code), just keep the same method names used
below (get_latest, idn, set_wavelength_nm, ...) and swap this file -
pax1000_webapp.py only talks to this class, nothing else needs to change.

Default resource string (M01219222 unit):
    USB0::0x1313::0x8031::M01219222::INSTR
"""
import math
import threading
import warnings
import time
from typing import Dict, Optional, Tuple

DEFAULT_RESOURCE = "USB0::0x1313::0x8031::M01219222::INSTR"

THORLABS_VID = "0x1313"  # used to filter list_resources() to Thorlabs gear


def discover_resources(existing=None):
    """Find Thorlabs USB instruments (VID 0x1313, which covers the PAX1000
    and its siblings). Returns a list of dicts:
        [{'resource': 'USB0::0x1313::0x8031::M01219222::INSTR',
          'vid': '0x1313', 'pid': '0x8031', 'serial': 'M01219222'}, ...]
    Returns an empty list (never raises) if no backend/devices are found,
    so it's always safe to call from a route handler.

    Args:
        existing: an already-connected PAX1000 instance, if any. When
            given AND connected, its already-known resource string is
            reported directly instead of scanning the USB bus. This
            matters: some pyvisa-py USB backends simply can't enumerate
            a device that's already claimed by an open session - not just
            when a *second* ResourceManager opens it, but at all, no
            matter which RM instance does the asking. Re-scanning the bus
            while connected was the source of two separate bugs (a
            dropped connection, then "no device found" despite being
            connected) - the robust fix is to not touch the bus at all
            in that case. Call this with pax_lock held if `existing` is
            connected, since reading its .resource should be consistent
            with the rest of the app's view of it.
    """
    if existing is not None and existing.connected and not existing.simulate:
        parts = existing.resource.split("::")
        if len(parts) >= 4:
            return [{
                "resource": existing.resource,
                "vid": parts[1],
                "pid": parts[2],
                "serial": parts[3],
            }]
        return []

    try:
        import pyvisa
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            rm = pyvisa.ResourceManager("@py")
            try:
                resources = rm.list_resources()
            finally:
                rm.close()
    except Exception:
        return []

    found = []
    for r in resources:
        parts = r.split("::")
        if len(parts) >= 4 and parts[0].upper().startswith("USB") and parts[1].lower() == THORLABS_VID:
            found.append({
                "resource": r,
                "vid": parts[1],
                "pid": parts[2],
                "serial": parts[3],
            })
    return found

# SENS:CALC measurement modes
MEAS_MODES = {
    0: "IDLE", 1: "H512", 2: "H1024", 3: "H2048",
    4: "F512", 5: "F1024", 6: "F2048",
    7: "D512", 8: "D1024", 9: "D2048",
}

# Field order of the SENS:DATA:LAT? response
LATEST_FIELDS = [
    "revs", "timestamp", "mode", "flags", "tia_range",
    "adc_min", "adc_max", "rev_time", "misadjustment",
    "theta", "eta", "dop", "power",
]


class PAX1000:
    """Universal Python controller for the Thorlabs PAX1000 polarimeter."""

    def __init__(self, resource: str = DEFAULT_RESOURCE, timeout: float = 3.0,
                 simulate: bool = False):
        """
        Args:
            resource: VISA resource string, e.g. USB0::0x1313::0x8031::M01219222::INSTR
            timeout: communication timeout in seconds
            simulate: if True, no hardware is touched - synthetic data is
                      generated instead, useful for developing the web UI
        """
        self.resource = resource
        self.timeout = timeout
        self.simulate = simulate
        self.connected = False
        self._inst = None
        self._rm = None
        self._lock = threading.RLock()
        self._sim_t0 = time.time()

    # -- connection ------------------------------------------------------
    def connect(self, auto_start: bool = True, default_mode: int = 5) -> bool:
        """Open the VISA session and, unless auto_start=False, make sure
        the instrument is actually measuring (waveplate spinning, a real
        SENS:CALC mode instead of IDLE, power auto-ranging on) - mirrors
        the sequence from the known-working reference script. Existing
        settings are left alone if they're already sensible, so this
        won't clobber a setup you've tuned by hand.
        """
        with self._lock:
            if self.simulate:
                self.connected = True
                return True

            # release a stale handle first, otherwise re-opening the same
            # USB device fails with "Resource busy"
            self._release()

            try:
                import pyvisa
                self._rm = pyvisa.ResourceManager("@py")
                self._inst = self._rm.open_resource(self.resource)
                self._inst.timeout = int(self.timeout * 1000)
                self.connected = True   # must be set before the idn() sanity
                                         # check below, or query() refuses to
                                         # talk to an instrument it thinks
                                         # isn't connected yet
                self.idn()
            except Exception as e:
                print(f"\u274c PAX1000 connect failed: {e}")
                self.connected = False
                self._release()  # don't leak a half-open handle -> EBUSY on retry
                return False

        if auto_start:
            try:
                self.ensure_active(default_mode=default_mode)
            except Exception as e:
                print(f"\u26a0\ufe0f  PAX1000 connected, but ensure_active() failed: {e}")
        return True

    def _release(self):
        """Close any open VISA handles without raising."""
        for obj in (self._inst, self._rm):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self._inst = None
        self._rm = None

    def ensure_active(self, default_mode: int = 5, settle_timeout: float = 3.0):
        """Start the waveplate motor and pick a real measurement mode if
        the instrument is currently idle, same sequence as the reference
        script: INP:ROT:STAT 1, SENS:POW:RANG:IND 16, SENS:POW:RANG:AUTO 1,
        SENS:CALC <default_mode>.
        """
        if not self.get_motor_state():
            print("\U0001f300 Starting the waveplate motor... please wait")
            self.set_motor_state(True)

        if self.get_power_range_auto() == 0:
            self.set_power_range_index(16)
            self.set_power_range_auto(1)

        if self.get_mode() == 0:  # IDLE
            self.set_mode(default_mode)

        t0 = time.time()
        while time.time() - t0 < settle_timeout:
            if self.motor_settled():
                break
            time.sleep(0.1)

    def reconnect(self, new_resource: Optional[str] = None) -> bool:
        with self._lock:
            self.close()
            if new_resource:
                self.resource = new_resource
            return self.connect()

    def close(self):
        with self._lock:
            self.connected = False
            self._release()

    # -- raw SCPI passthrough (used directly by the SCPI server, too) -----
    def query(self, cmd: str) -> str:
        with self._lock:
            if self.simulate:
                return self._sim_query(cmd)
            if not self.connected or self._inst is None:
                raise RuntimeError("PAX1000 not connected")
            return self._inst.query(cmd).strip()

    def write(self, cmd: str):
        with self._lock:
            if self.simulate:
                return
            if not self.connected or self._inst is None:
                raise RuntimeError("PAX1000 not connected")
            self._inst.write(cmd)

    # -- high level commands ----------------------------------------------
    def idn(self) -> str:
        return self.query("*IDN?")

    def get_latest(self) -> Dict[str, float]:
        """SENS:DATA:LAT? -> revs, timestamp, mode, flags, tia_range,
        adc_min, adc_max, rev_time, misadjustment, theta, eta, dop, power"""
        raw = self.query("SENS:DATA:LAT?")
        values = []
        for v in raw.split(","):
            v = v.strip()
            try:
                values.append(float(v))
            except ValueError:
                values.append(v)
        return dict(zip(LATEST_FIELDS, values))

    def get_calibration_string(self) -> str:
        return self.query("CAL:STR?")

    # wavelength
    def get_wavelength_nm(self) -> float:
        return float(self.query("SENS:CORR:WAV?")) * 1e9

    def set_wavelength_nm(self, wl_nm: float):
        self.write(f"SENS:CORR:WAV {wl_nm * 1e-9:.6e}")

    def get_wavelength_limits_nm(self) -> Tuple[float, float]:
        """SENS:CORR:WAV? MIN/MAX is standard SCPI convention but was never
        confirmed against real PAX1000 hardware - if the instrument doesn't
        support it, fall back to a generic range rather than risking a
        timed-out/incomplete USBTMC transaction that could corrupt the next
        unrelated read."""
        try:
            lo = float(self.query("SENS:CORR:WAV? MIN")) * 1e9
            hi = float(self.query("SENS:CORR:WAV? MAX")) * 1e9
            return lo, hi
        except Exception:
            return 400.0, 1700.0

    # waveplate motor
    def get_motor_state(self) -> bool:
        return bool(int(float(self.query("INP:ROT:STAT?"))))

    def set_motor_state(self, on: bool):
        self.write(f"INP:ROT:STAT {1 if on else 0}")

    def get_motor_velocity(self) -> float:
        return float(self.query("INP:ROT:VEL?"))

    def set_motor_velocity(self, hz: float):
        self.write(f"INP:ROT:VEL {hz:.0f}")

    def get_motor_velocity_limits(self) -> Tuple[float, float]:
        ext, usb = self.query("INP:ROT:VEL:LIM?").split(",")
        return float(ext), float(usb)

    def motor_settled(self) -> bool:
        return bool(int(float(self.query("INP:ROT:SETT?"))))

    # measurement mode
    def get_mode(self) -> int:
        return int(float(self.query("SENS:CALC?")))

    def set_mode(self, mode: int):
        self.write(f"SENS:CALC {int(mode)}")

    # power range
    def get_power_range_auto(self) -> int:
        return int(float(self.query("SENS:POW:RANG:AUTO?")))

    def set_power_range_auto(self, state: int):
        self.write(f"SENS:POW:RANG:AUTO {int(state)}")  # 0 OFF, 1 ON, 2 ONCE

    def get_power_range_index(self) -> int:
        return int(float(self.query("SENS:POW:RANG:IND?")))

    def set_power_range_index(self, idx: int):
        self.write(f"SENS:POW:RANG:IND {int(idx)}")

    # -- Stokes parameters --------------------------------------------------
    @staticmethod
    def stokes(theta: float, eta: float, dop: float) -> Tuple[float, float, float, float]:
        """Normalised Stokes parameters (S0=1) from azimuth/ellipticity/DOP."""
        s0 = 1.0
        s1 = dop * math.cos(2 * eta) * math.cos(2 * theta)
        s2 = dop * math.cos(2 * eta) * math.sin(2 * theta)
        s3 = dop * math.sin(2 * eta)
        return s0, s1, s2, s3

    @staticmethod
    def stokes_raw(theta: float, eta: float, dop: float, power: float) -> Tuple[float, float, float, float]:
        """Raw Stokes parameters in actual optical power units (W):
        S0 = power, S1..S3 scaled the same way but carrying the DOP and
        power of the beam rather than being normalised to S0=1."""
        s0 = power
        s1 = power * dop * math.cos(2 * eta) * math.cos(2 * theta)
        s2 = power * dop * math.cos(2 * eta) * math.sin(2 * theta)
        s3 = power * dop * math.sin(2 * eta)
        return s0, s1, s2, s3

    @staticmethod
    def decode_temp_flag(flags) -> str:
        """The PAX1000's documented SCPI command set has no numeric
        temperature query - only two warning bits in the scan flags
        (SENS:DATA:LAT?, bits 4 and 5): operating temperature below/above
        the internally configured limit. This decodes those into a status
        string. There is no degrees-C reading available over SCPI."""
        try:
            flags = int(flags)
        except (TypeError, ValueError):
            return "unknown"
        if flags & (1 << 5):
            return "ABOVE_LIMIT"
        if flags & (1 << 4):
            return "BELOW_LIMIT"
        return "OK"

    # -- simulator (no hardware needed) --------------------------------------
    def _sim_query(self, cmd: str) -> str:
        c = cmd.strip().upper()
        t = time.time() - self._sim_t0
        if c.startswith("*IDN?"):
            return "Thorlabs,PAX1000,SIM00000001,0.0.0-sim"
        if "DATA:LAT?" in c:
            theta = 0.35 * math.sin(0.13 * t) + 0.05 * math.sin(2.2 * t)
            eta = 0.20 * math.sin(0.07 * t + 1.0)
            dop = 0.93 + 0.05 * math.sin(0.03 * t)
            power = 1.2e-3 * (1.0 + 0.03 * math.sin(0.5 * t))
            fields = [int(t * 10), int(t * 1000), 4, 0, 8, 12, 4090, 33,
                      0.4 + 0.2 * abs(math.sin(t)), theta, eta, dop, power]
            return ",".join(str(x) for x in fields)
        if c.startswith("CAL:STR?"):
            return "14-Feb-2020"
        if "CORR:WAV?" in c:
            return "4.0e-7" if "MIN" in c else "1.7e-6" if "MAX" in c else "5.89e-7"
        if c.startswith("INP:ROT:STAT?"):
            return "1"
        if c.startswith("INP:ROT:VEL:LIM?"):
            return "200,50"
        if c.startswith("INP:ROT:VEL?"):
            return "33"
        if c.startswith("INP:ROT:SETT?"):
            return "1"
        if "CALC?" in c:
            return "4"
        if "POW:RANG:AUTO?" in c:
            return "1"
        if "POW:RANG:IND?" in c:
            return "10"
        if c.startswith("SYST:ERR?"):
            return '0,"No error"'
        return ""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


if __name__ == "__main__":
    print("\U0001f9ed PAX1000 driver self-test (simulate mode)")
    with PAX1000(simulate=True) as pax:
        print("IDN:", pax.idn())
        d = pax.get_latest()
        print("Latest:", d)
        print("Stokes:", pax.stokes(d["theta"], d["eta"], d["dop"]))

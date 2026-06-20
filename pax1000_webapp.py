"""
PAX1000 Polarimeter Web Application with SCPI Server
======================================================

This application provides:
1. Web interface on port 80 (Flask) - Poincare sphere, polarization
   ellipse, Stokes-parameter history and a settings page, with power /
   DOP / Stokes parameters always visible.
2. SCPI command server on port 5025 (socket server) - real PAX1000
   SCPI commands are forwarded straight to the instrument, plus a
   couple of local convenience commands (POL:STOKES?, POL:HIST?).
3. Background polling thread feeding both of the above from a single,
   lock-protected PAX1000 connection.

Usage:
    python3 pax1000_webapp.py                 # real hardware, port 80
    python3 pax1000_webapp.py --simulate       # no hardware needed
    python3 pax1000_webapp.py --port 8080      # unprivileged port

Web Interface:  http://localhost
SCPI Interface: telnet localhost 5025
"""
import argparse
import csv
import io
import json
import socket
import threading
import time
from collections import deque
from datetime import datetime

from flask import Flask, Response, current_app, jsonify, render_template, request

from pax1000_driver import PAX1000, MEAS_MODES, discover_resources

# ----------------------------------------------------------------------
# Globals
# ----------------------------------------------------------------------
pax = None
pax_lock = threading.Lock()
history = deque(maxlen=500)          # shared ring buffer of samples
last_error = None                      # most recent poll-loop exception, for diagnostics
app = Flask(__name__)


def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0)
    try:
        s.connect(('10.254.254.254', 1))  # doesn't have to be reachable
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def get_mac():
    for iface in ("eth0", "wlan0", "enp0s31f6"):
        try:
            with open(f"/sys/class/net/{iface}/address") as f:
                return f.readline().strip()
        except Exception:
            continue
    return "N/A"


IP_ADDRESS = get_ip()
MAC_ADDRESS = get_mac()


# ----------------------------------------------------------------------
# SCPI server (raw TCP, port 5025) - real PAX1000 commands pass through,
# plus a few local POL:* convenience commands.
# ----------------------------------------------------------------------
class SCPIServer:
    """Raw-socket SCPI server: telnet <host> 5025"""

    def __init__(self, port=5025):
        self.port = port
        self.running = False
        self.server_socket = None

    def start(self):
        self.running = True
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_socket.bind(('0.0.0.0', self.port))
            self.server_socket.listen(5)
            print(f"\U0001f4e1 SCPI server listening on port {self.port}")

            while self.running:
                try:
                    client_socket, address = self.server_socket.accept()
                    t = threading.Thread(target=self._handle_client, args=(client_socket, address))
                    t.daemon = True
                    t.start()
                except Exception as e:
                    if self.running:
                        print(f"\u274c SCPI server error: {e}")
        except Exception as e:
            print(f"\u274c Failed to start SCPI server: {e}")
        finally:
            if self.server_socket:
                self.server_socket.close()

    def _handle_client(self, client_socket, address):
        try:
            client_socket.send(b"PAX1000 SCPI Server Ready\n")
            buffer = ""
            while self.running:
                data = client_socket.recv(1024).decode('utf-8', errors='replace')
                if not data:
                    break
                buffer += data
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line.upper() in ('QUIT', 'EXIT'):
                        client_socket.send(b"Goodbye\n")
                        return
                    response = process_scpi_command(line)
                    if response is not None:
                        client_socket.send((response + "\n").encode('utf-8'))
        except Exception as e:
            print(f"\u274c SCPI client error {address}: {e}")
        finally:
            try:
                client_socket.close()
            except Exception:
                pass

    def stop(self):
        self.running = False
        if self.server_socket:
            self.server_socket.close()


HELP_TEXT = (
    "Any standard PAX1000 SCPI command is forwarded to the device, e.g.\n"
    "  *IDN?\n  SENS:DATA:LAT?\n  SENS:CORR:WAV?\n  INP:ROT:VEL 33\n"
    "Local extensions:\n"
    "  POL:STOKES?       latest normalised Stokes parameters S0,S1,S2,S3 (S0=1)\n"
    "  POL:STOKES:RAW?    latest raw Stokes parameters S0,S1,S2,S3 in watts\n"
    "  POL:DOP?           latest degree of polarization (0..1)\n"
    "  POL:TEMP:STATUS?    OK / ABOVE_LIMIT / BELOW_LIMIT (warning flag, not a\n"
    "                      numeric reading - the PAX1000 doesn't expose one)\n"
    "  POL:CONFIG?         current device settings as one line, so a measurement\n"
    "                      can be reproduced: wavelength, mode, motor, power range\n"
    "  POL:HIST? <n>      last n history rows as CSV\n"
    "  POL:HIST:LEN?      number of samples currently buffered\n"
    "  SYST:HELP?         this text"
)


def process_scpi_command(cmd):
    """Local POL:*/SYST:HELP? extensions, everything else -> instrument."""
    upper = cmd.upper()
    try:
        if upper.startswith("SYST:HELP?"):
            return HELP_TEXT
        if upper.startswith("POL:STOKES:RAW?"):
            if not history:
                return "0,0,0,0"
            s = history[-1]
            return f"{s['rs0']:.6e},{s['rs1']:.6e},{s['rs2']:.6e},{s['rs3']:.6e}"
        if upper.startswith("POL:STOKES?"):
            if not history:
                return "0,0,0,0"
            s = history[-1]
            return f"{s['s0']:.6f},{s['s1']:.6f},{s['s2']:.6f},{s['s3']:.6f}"
        if upper.startswith("POL:DOP?"):
            if not history:
                return "0"
            return f"{history[-1]['dop']:.6f}"
        if upper.startswith("POL:TEMP:STATUS?"):
            if not history:
                return "UNKNOWN"
            return history[-1].get("temp_status", "UNKNOWN")
        if upper.startswith("POL:CONFIG?"):
            with pax_lock:
                if not pax or not pax.connected:
                    return '-1,"PAX1000 not connected"'
                wl = pax.get_wavelength_nm()
                mode = pax.get_mode()
                motor_on = pax.get_motor_state()
                vel = pax.get_motor_velocity()
                auto = pax.get_power_range_auto()
                idx = pax.get_power_range_index()
                return (f"WAVELENGTH_NM={wl:.2f},MODE={mode},MODE_NAME={MEAS_MODES.get(mode, '?')},"
                        f"MOTOR_ON={int(motor_on)},MOTOR_VEL_HZ={vel:.0f},"
                        f"POW_RANGE_AUTO={auto},POW_RANGE_IDX={idx},RESOURCE={pax.resource}")
        if upper.startswith("POL:HIST:LEN?"):
            return str(len(history))
        if upper.startswith("POL:HIST?"):
            parts = cmd.split()
            n = int(parts[1]) if len(parts) > 1 else 100
            rows = list(history)[-n:]
            lines = ["timestamp,theta,eta,dop,power,s0,s1,s2,s3,rs0,rs1,rs2,rs3"]
            for r in rows:
                lines.append(f"{r['t']:.3f},{r['theta']:.6f},{r['eta']:.6f},{r['dop']:.6f},"
                              f"{r['power']:.6e},{r['s0']:.6f},{r['s1']:.6f},{r['s2']:.6f},{r['s3']:.6f},"
                              f"{r['rs0']:.6e},{r['rs1']:.6e},{r['rs2']:.6e},{r['rs3']:.6e}")
            return "\n".join(lines)

        with pax_lock:
            if not pax or not pax.connected:
                return '-1,"PAX1000 not connected"'
            if cmd.endswith("?"):
                return pax.query(cmd)
            pax.write(cmd)
            return ""
    except Exception as e:
        return f'-1,"{e}"'


# ----------------------------------------------------------------------
# Background polling thread
# ----------------------------------------------------------------------
def poll_loop(rate_hz=20, reconnect_interval=3.0, max_consecutive_failures=3):
    global last_error
    period = 1.0 / max(1, rate_hz)
    t0 = time.time()
    last_reconnect_attempt = 0.0
    consecutive_failures = 0
    while True:
        loop_start = time.time()
        try:
            with pax_lock:
                if pax and not pax.connected:
                    if loop_start - last_reconnect_attempt >= reconnect_interval:
                        last_reconnect_attempt = loop_start
                        if pax.connect():
                            consecutive_failures = 0
                            last_error = None
                if pax and pax.connected:
                    d = pax.get_latest()
                    s0, s1, s2, s3 = PAX1000.stokes(d["theta"], d["eta"], d["dop"])
                    rs0, rs1, rs2, rs3 = PAX1000.stokes_raw(d["theta"], d["eta"], d["dop"], d["power"])
                    history.append({
                        "t": loop_start - t0, "theta": d["theta"], "eta": d["eta"],
                        "dop": d["dop"], "power": d["power"],
                        "misadjustment": d.get("misadjustment", 0.0),
                        "flags": d.get("flags", 0),
                        "temp_status": PAX1000.decode_temp_flag(d.get("flags", 0)),
                        "s0": s0, "s1": s1, "s2": s2, "s3": s3,
                        "rs0": rs0, "rs1": rs1, "rs2": rs2, "rs3": rs3,
                    })
                    consecutive_failures = 0
                    last_error = None
        except Exception as e:
            consecutive_failures += 1
            last_error = str(e)
            print(f"\u26a0\ufe0f  poll error ({consecutive_failures}/{max_consecutive_failures}): {e}")
            if consecutive_failures >= max_consecutive_failures:
                # a handful of one-off USBTMC hiccups shouldn't flip the UI
                # to "disconnected" - only do that once it's clearly not coming back
                with pax_lock:
                    if pax:
                        pax.connected = False
            time.sleep(0.5)
        elapsed = time.time() - loop_start
        time.sleep(max(0.0, period - elapsed))


# ----------------------------------------------------------------------
# Flask web interface
# ----------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html', ipa=IP_ADDRESS, mac=MAC_ADDRESS, meas_modes=MEAS_MODES)


@app.route('/css')
def css():
    return current_app.send_static_file('style.css')


@app.route('/help')
def help_page():
    return render_template('help.html', ipa=IP_ADDRESS, mac=MAC_ADDRESS)


@app.route('/api/status')
def api_status():
    with pax_lock:
        connected = bool(pax and pax.connected)
        resource = pax.resource if pax else None
    return jsonify({
        'connected': connected, 'resource': resource,
        'history_len': len(history), 'history_capacity': history.maxlen,
        'last_error': last_error,
        'timestamp': datetime.now().isoformat(),
    })


@app.route('/api/latest')
def api_latest():
    return jsonify(history[-1] if history else None)


@app.route('/api/history')
def api_history():
    n = request.args.get('n', type=int)
    if n is None or n <= 0:
        return jsonify(list(history))
    return jsonify(list(history)[-n:])


@app.route('/api/history/capacity', methods=['POST'])
def api_history_capacity():
    global history
    n = int(request.get_json(force=True).get('n', 500))
    with pax_lock:
        if n <= 0:
            # "Infinite" - deque(maxlen=None) never discards old samples.
            # Genuinely unbounded: fine for a deliberate long capture, but
            # watch memory on an unattended multi-day run.
            history = deque(history, maxlen=None)
        else:
            n = max(1, min(1000000, n))
            history = deque(history, maxlen=n)
    return jsonify({'capacity': history.maxlen})


@app.route('/api/history/clear', methods=['POST'])
def api_history_clear():
    history.clear()
    return jsonify({'success': True})


@app.route('/api/history/download')
def api_history_download():
    rows = list(history)

    buf = io.StringIO()
    buf.write("# PAX1000 polarimeter measurement history\n")
    buf.write(f"# Generated: {datetime.now().isoformat()}\n")
    with pax_lock:
        connected = bool(pax and pax.connected)
        idn = pax.idn() if connected else "n/a (not connected)"
        resource = pax.resource if pax else "n/a"
    buf.write(f"# Device: {idn}\n")
    buf.write(f"# VISA resource: {resource}\n")
    buf.write(f"# Samples: {len(rows)}\n")
    buf.write("#\n")
    buf.write("# Columns:\n")
    buf.write("#   t              seconds since the web app started polling\n")
    buf.write("#   theta, eta     azimuth / ellipticity of the polarization ellipse [rad]\n")
    buf.write("#   dop            degree of polarization, 0..1\n")
    buf.write("#   power          total optical power [W]\n")
    buf.write("#   misadjustment  PAX1000 alignment-quality metric [%], lower is better (<2% recommended)\n")
    buf.write("#   flags          raw PAX1000 scan status flags (see SENS:DATA:LAT? in the SCPI reference)\n")
    buf.write("#   temp_status    OK / ABOVE_LIMIT / BELOW_LIMIT temperature warning flag\n")
    buf.write("#   s0..s3         normalised Stokes parameters (S0=1)\n")
    buf.write("#   rs0..rs3       raw Stokes parameters [W] (rs0 = total power)\n")
    buf.write("#\n")

    fieldnames = ['t', 'theta', 'eta', 'dop', 'power', 'misadjustment', 'flags', 'temp_status',
                  's0', 's1', 's2', 's3', 'rs0', 'rs1', 'rs2', 'rs3']
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow(row)

    filename = f"pax1000_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@app.route('/api/settings')
def api_settings_get():
    try:
        with pax_lock:
            if not pax or not pax.connected:
                return jsonify({'error': 'PAX1000 not connected'}), 503
            wl_lo, wl_hi = pax.get_wavelength_limits_nm()
            vel_ext, vel_usb = pax.get_motor_velocity_limits()
            mode = pax.get_mode()
            return jsonify({
                'idn': pax.idn(),
                'calibration': pax.get_calibration_string(),
                'resource': pax.resource,
                'wavelength_nm': pax.get_wavelength_nm(),
                'wavelength_min_nm': wl_lo, 'wavelength_max_nm': wl_hi,
                'motor_on': pax.get_motor_state(),
                'motor_velocity_hz': pax.get_motor_velocity(),
                'motor_velocity_limit_ext_hz': vel_ext,
                'motor_velocity_limit_usb_hz': vel_usb,
                'motor_settled': pax.motor_settled(),
                'mode': mode, 'mode_name': MEAS_MODES.get(mode, '?'), 'meas_modes': MEAS_MODES,
                'power_range_auto': pax.get_power_range_auto(),
                'power_range_index': pax.get_power_range_index(),
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/settings', methods=['POST'])
def api_settings_post():
    data = request.get_json(force=True)
    errors = {}
    with pax_lock:
        if not pax or not pax.connected:
            return jsonify({'success': False, 'error': 'PAX1000 not connected'}), 503
        setters = {
            'wavelength_nm': lambda v: pax.set_wavelength_nm(float(v)),
            'motor_on': lambda v: pax.set_motor_state(bool(v)),
            'motor_velocity_hz': lambda v: pax.set_motor_velocity(float(v)),
            'mode': lambda v: pax.set_mode(int(v)),
            'power_range_auto': lambda v: pax.set_power_range_auto(int(v)),
            'power_range_index': lambda v: pax.set_power_range_index(int(v)),
        }
        for key, setter in setters.items():
            if key in data:
                try:
                    setter(data[key])
                except Exception as e:
                    errors[key] = str(e)
    if errors:
        return jsonify({'success': False, 'errors': errors}), 400
    return jsonify({'success': True})


@app.route('/api/device/scan')
def api_device_scan():
    with pax_lock:
        devices = discover_resources(existing=pax)
    return jsonify({'devices': devices})


@app.route('/api/device/reconnect', methods=['POST'])
def api_reconnect():
    resource = request.get_json(force=True).get('resource', '').strip()
    with pax_lock:
        try:
            ok = pax.reconnect(new_resource=resource or None)
            return jsonify({'success': ok, 'idn': pax.idn() if ok else None})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/stream')
def api_stream():
    """Optional SSE feed (the bundled UI polls /api/latest instead, but
    this is here for any client that prefers a push feed)."""
    def generate():
        last_t = None
        while True:
            if history and history[-1]['t'] != last_t:
                last_t = history[-1]['t']
                yield f"data: {json.dumps(history[-1])}\n\n"
            else:
                time.sleep(0.03)
    return Response(generate(), mimetype='text/event-stream')


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
def main():
    global pax
    parser = argparse.ArgumentParser(description="PAX1000 polarimeter web + SCPI server")
    parser.add_argument('--port', type=int, default=80, help='web UI port (default 80)')
    parser.add_argument('--scpi-port', type=int, default=5025, help='SCPI TCP port (default 5025)')
    parser.add_argument('--resource', default=None, help='VISA resource string override')
    parser.add_argument('--simulate', action='store_true', help='run without real hardware')
    parser.add_argument('--poll-rate', type=float, default=20, help='polling rate in Hz')
    args = parser.parse_args()

    print("\U0001f680 Starting PAX1000 Web Application with SCPI Server")
    print("=" * 60)

    pax = PAX1000(resource=args.resource or PAX1000().resource, simulate=args.simulate)
    if pax.connect():
        print(f"\u2705 PAX1000 connected: {pax.idn()}")
    else:
        print("\u26a0\ufe0f  Continuing without PAX1000 connection (use Settings tab to reconnect)")

    threading.Thread(target=poll_loop, args=(args.poll_rate,), daemon=True).start()

    scpi_server = SCPIServer(port=args.scpi_port)
    threading.Thread(target=scpi_server.start, daemon=True).start()

    print(f"\U0001f310 Web interface:  http://localhost:{args.port}")
    print(f"\U0001f4e1 SCPI interface: telnet localhost {args.scpi_port}")
    print("\U0001f527 Press Ctrl+C to stop the server")

    try:
        app.run(host='0.0.0.0', port=args.port, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\n\u23f9\ufe0f  Shutting down...")
    finally:
        scpi_server.stop()
        if pax:
            pax.close()


if __name__ == '__main__':
    main()

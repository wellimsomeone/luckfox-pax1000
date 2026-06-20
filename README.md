# pax1000

Web interface + SCPI server for the Thorlabs PAX1000 polarimeter
(`USB0::0x1313::0x8031::M01219222::INSTR`).

```
pip install -r requirements.txt
python3 pax1000_webapp.py
```

Web UI: `http://localhost` (port 80, needs root or
`setcap cap_net_bind_service=+ep`). SCPI: `telnet <host> 5025`.

No hardware at hand? `python3 pax1000_webapp.py --simulate --port 8080`.

`pyvisa` needs a VISA backend to actually talk to the USB device:
either NI-VISA if you already have it, or `pip install pyvisa-py
pyusb` for the pure-Python route (no NI-VISA needed). If you'd rather
use your own existing driver code, just give it the same method names
as `PAX1000` in `pax1000_driver.py` (`get_latest`, `idn`,
`set_wavelength_nm`, ...) and drop it in - `pax1000_webapp.py` only
talks to that one class.

### Troubleshooting

**`Permission denied`** when not running as root: your user doesn't
have permission to open the USB device. Either run with `sudo`, or
add a udev rule so you don't have to:
```
# /etc/udev/rules.d/99-thorlabs-pax.rules
SUBSYSTEM=="usb", ATTRS{idVendor}=="1313", ATTRS{idProduct}=="8031", MODE="0666"
```
then `sudo udevadm control --reload-rules && sudo udevadm trigger`.

**`[Errno 16] Resource busy`** on every (re)connect attempt: something
else still has the device open - another instance of this script
(check `ps aux | grep pax1000_webapp`), the Thorlabs PAX1000 software,
or a leaked handle from an older build of this app (fixed now: a
failed connect used to leave the USB interface claimed, so every retry
after that failed with EBUSY too). If it still happens, unplug/replug
the polarimeter or `kill` any stray python processes holding it open.

Files:
```
pax1000_driver.py    PAX1000 SCPI/VISA driver (swap this out if needed)
pax1000_webapp.py     Flask app + SCPI passthrough server, single file
templates/index.html   sphere / ellipse / history / settings tabs
templates/help.html     SCPI command reference
static/style.css         site CSS (same look as /luckfox)
```

Add to `/etc/rc.local` to autostart:
```
python3 /home/pico/pax1000/pax1000_webapp.py
```

# Spike #1: usbmux throughput (the make-or-break)

The whole `ipaddisplay` project lives or dies on one number: how many Mbps the
`usbmux`-over-USB transport sustains between the Windows host and the iPad. If
it's ~300+ Mbps at low latency, the design is green. If it's badly starved, we
rethink the transport before writing a line of capture/encode code.

## Two stages

### Stage A — AFC throughput probe (NO Mac, runnable now)
`afc_throughput.py` pushes/pulls a large file over **AFC** (Apple File Conduit),
which rides the same usbmux multiplexing + USB bulk pipe a custom display socket
would use. It's a slightly conservative proxy, but it answers the go/no-go
question today without an iPad app or a Mac.

Thresholds (host->device write Mbps):
- **>= ~300** GREEN  — proceed to build the pipeline.
- **~150-300** YELLOW — workable at native 60Hz with tuned bitrate + dirty-rects.
- **< ~150** RED    — rethink transport before investing further.

### Stage B — precise socket echo (NEEDS a Mac for the iPad app)
A tiny iPad app listens on a TCP port; the host connects through usbmux
(`iproxy`/libusbmuxd) and runs a sustained bidirectional blast + ping-pong to get
exact throughput *and* round-trip latency. This is the real measurement, but
building the iPad app requires Xcode = macOS. Blocked until a Mac is available
(see "The Mac problem").

## Where to run Stage A

The iPad plugs into **Windows**, and Apple's usbmux service lives there (it ships
with the **Apple Devices** app, formerly iTunes). So run the probe on Windows,
not in WSL2 (WSL2 has no USB passthrough to the iPad by default).

```powershell
# In Windows PowerShell (not WSL2):
#  1. Install the "Apple Devices" app (Microsoft Store) or iTunes — provides usbmuxd + USB driver.
#  2. Plug in the iPad, unlock it, tap "Trust This Computer".
python -m pip install pymobiledevice3
python afc_throughput.py --size-mb 256 --runs 3
```

(Alternative: forward the iPad into WSL2 with `usbipd-win` + run usbmuxd in
Linux. More moving parts; Apple devices over usbip can be finicky. Prefer the
Windows path.)

## Interpreting the result

AFC carries protocol overhead a raw socket doesn't, so a real socket stream
should **meet or slightly beat** the AFC number — treat AFC Mbps as a
conservative floor on the true usbmux ceiling. The ceiling is often a usbmux
*protocol* limit (~300-400 Mbps historically), not a cable limit; a
Thunderbolt/USB4 iPad does not guarantee more.

## The Mac problem (project-level, surfaced during this spike)

Building/signing/deploying the iPad app needs **Xcode**, which is macOS-only.
The dev machine here is Windows. Options, cheapest first:
- Borrow/use any Mac occasionally (the iPad app is small; build + deploy is quick).
- A used Mac mini (Apple Silicon) as a build box.
- A cloud Mac (MacStadium / MacinCloud / GitHub Actions macOS runners) for builds.

Stage A does not need this. Stage B and the real product do. Decide before
committing to the full build.

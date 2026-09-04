# Rosa RSSI Portal Tracker

A small Wi-Fi localization demo built for a controlled cybersecurity/maker experiment.

The project combines a harmless captive portal running on GhostESP/Banshee with a Python RSSI tracker on a ClockworkPi uConsole. The portal is intentionally non-credentialed: it does not ask for usernames, passwords, personal data, or any other input. Pressing the button only reveals a local "snack requested" message.

The tracker scans for nearby Wi-Fi access points, lets you select a target BSSID, then uses RSSI from received 802.11 management frames to help you move toward or away from the selected transmitter. A fullscreen cyber-style interface shows live RSSI, signal history, peak signal, sample age, a stabilized homing indicator, and an acoustic ping that increases in rate as signal strength rises.

## Demo concept

1. Start the harmless `ROSA HACKS YOU` test portal on your own GhostESP/Banshee device.
2. Connect only your own test device to the portal.
3. Select the portal AP/BSSID in the uConsole tracker.
4. Use the RSSI trend and acoustic ping to locate the transmitter.
5. Deliver the requested dog snack.

## Files

- `rf_target_tracker.py` — fullscreen RSSI tracker for Linux
- `portal/rosa_hacks_you.html` — harmless local captive-portal page

## Tracker requirements

**A monitor-mode capable Wi-Fi receiver is required.** The tracker relies on passive 802.11 packet reception and RSSI/radiotap information after a target is selected. A normal Wi-Fi interface that cannot operate in Linux monitor mode will not provide live packet-based tracking. Linux `iw` exposes monitor as a distinct interface mode, and `tcpdump` can capture from compatible Wi-Fi interfaces in monitor mode.

Recommended architecture:

- **Transmitter / test AP:** GhostESP/Banshee or another AP you own or are authorized to test
- **Receiver:** Linux computer such as a ClockworkPi uConsole
- **Wi-Fi receiver:** dedicated adapter with Linux monitor-mode support
- **Example used for this project:** ALFA AWUS036ACM (MT7612U / `mt76x2u`)

Using a dedicated receiver is recommended because switching an adapter into monitor mode can disconnect that adapter from its normal Wi-Fi network.

System packages:

```bash
sudo apt install python3-tk iw tcpdump alsa-utils
```

Run:

```bash
sudo python3 rf_target_tracker.py --iface wlan1
```

The selected Wi-Fi interface is temporarily switched into monitor mode during packet-based tracking and restored to managed mode when tracking stops or the program exits.

## Portal

Place `rosa_hacks_you.html` in the GhostESP custom portal directory used by your firmware/build and start it as a local test portal. The page contains no form fields and performs no data submission or storage.

## Important limitation

RSSI is a scalar signal-strength measurement. It does **not** provide a true absolute 360° bearing. The on-screen arrow is a relative homing indicator based on whether signal strength is improving, worsening, or inconclusive while you move.

## Safety and intended use

This repository is intended only for controlled demonstrations on hardware and networks you own or are explicitly authorized to test. The included portal does not collect credentials or personal data. Do not use the project to impersonate third-party networks, force users off legitimate networks, or collect information without authorization.

## License

MIT

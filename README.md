# PikaStats Desktop Overlay

PikaStats Desktop Overlay is a Windows desktop panel for the [PikaStats ViaProxy plugin](https://github.com/elpolake/pikastats-plugin). It shows players detected in your live BedWars roster, their stats, and their recent punishment counts and entries.

## Features

- Shows the current BedWars player roster, ordered by a score calculated from FKDR, WLR, and KDR.
- Shows each player's FKDR, WLR, and KDR at a glance.
- Click a player to see detailed stats and punishment counts and entries.
- Supports copying visible text with Ctrl+C. Clickable punishment URLs open in your default browser.
- Lets you change the BedWars mode and stats period, refresh the roster, move or resize the panel, and hide or show it with a global hotkey.

## Requirements

- Windows 10 or later.
- Python 3.10 or newer.
- ViaProxy with the PikaStats plugin installed and running.
- A PikaNetwork API key configured in the plugin. The overlay itself does not store or need the key.

## Install and run

1. Install and configure the [PikaStats ViaProxy plugin](https://github.com/elpolake/pikastats-plugin).
2. Install Python 3.10 or newer, including the Python launcher.
3. Open a terminal in this folder and install dependencies:

~~~powershell
py -3 -m pip install -r requirements.txt
~~~

4. Start the overlay:

~~~powershell
.\start.bat
~~~

You can also run **py -3 main.py**. Keep ViaProxy running while using the overlay. The overlay connects locally to **127.0.0.1:8765**.

## Controls

- Press **F8** to hide or show the overlay.
- Drag the header to move it.
- Drag the resize grip in the lower-right corner to change its size.
- Click **X** to close the overlay.
- Click **MODE** to cycle through quads, solo, doubles, triples, and all modes.
- Click **TIME** to cycle through all-time, weekly, monthly, and yearly stats.
- Click the reload button to refresh the roster and player stats.
- Click a player card to open detailed stats and punishments. Use **PLAYERS** to return to the roster.
- Select text and press **Ctrl+C** to copy it. Click a punishment URL to open it in your browser.

Set **click_through** to **false** to interact with the panel. It is false by default.

## Configuration

The first run creates **config.json** in this folder. The file is local and excluded from Git. Close the overlay before editing it; restart the overlay to load manual changes.

Default settings:

~~~json
{
  "overlay": {
    "enabled": true,
    "x": 24,
    "y": 180,
    "width": 326,
    "height": 420,
    "opacity": 0.84,
    "font_size": 13,
    "hotkey": "F8",
    "click_through": false
  },
  "websocket": {
    "host": "127.0.0.1",
    "port": 8765
  },
  "score": {
    "fkdr_weight": 1.0,
    "wlr_weight": 1.0,
    "kdr_weight": 0.5,
    "fkdr_cap": 4.0,
    "wlr_cap": 4.0,
    "kdr_cap": 4.0
  },
  "categories": {
    "high": 4.0,
    "good": 2.5,
    "decent": 1.5
  }
}
~~~

The score is the sum of capped FKDR times its weight, capped WLR times its weight, and capped KDR times its weight. The category thresholds set the high, good, and decent labels; lower scores are low. Missing stats are shown as unknown.

The hotkey accepts a key such as **F8** or a combination such as **CTRL+F8** or **SHIFT+F8**. It supports CTRL, SHIFT, and ALT modifiers, function keys F1 to F24, and several named keys including SPACE, INSERT, HOME, END, PAUSE, SCROLLLOCK, TAB, and GRAVE.

The WebSocket host must remain local. The overlay only connects to **127.0.0.1** or **localhost**; the default port is **8765**. If you change the plugin's **websocket-port**, set the overlay's **websocket.port** to the same port.

## Troubleshooting

- **The overlay says ViaProxy is disconnected:** start ViaProxy and confirm the PikaStats plugin loaded. Match the plugin port and overlay port in their settings.
- **The roster is empty:** enter a live BedWars game and wait for the plugin to detect players.
- **Stats or punishments are missing:** confirm the plugin has a valid PikaNetwork API key. Nicked players or hidden profiles may not have stats.
- **The overlay will not start:** install the dependencies again with **py -3 -m pip install -r requirements.txt** and check the terminal for an error.
- **The hotkey does not toggle the panel:** check the **hotkey** value in **config.json** and use a supported key name.

## Development

Dependencies are listed in **requirements.txt**. To start the source version on Windows, install them and run **py -3 main.py**. The overlay uses PySide6 for the interface and websocket-client to receive updates from the ViaProxy plugin.

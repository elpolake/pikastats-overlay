# PikaStats Desktop Overlay

Standalone Windows desktop overlay for the PikaStats ViaProxy plugin. It displays the live BedWars roster and player stats, and opens detailed stats and punishments when you click a player.

## Run

1. Install Python 3.10 or newer.
2. Open a terminal in this folder and run `pip install -r requirements.txt`.
3. Install the matching PikaStats plugin in ViaProxy and start ViaProxy.
4. Run `start.bat` or `python main.py`.

The overlay connects to the plugin on `127.0.0.1:8765`. It creates `config.json` with default settings on first launch; that local config is ignored by Git. Press **F8** to hide or show the panel. Drag the header to move it and use the corner grip to resize it. Set `click_through` to `false` to use the buttons and player cards.

Use the **MODE** and **TIME** buttons to change the stats scope, and the reload button to refresh the roster. Click a player card to see detailed BedWars stats and punishment history. Text can be selected and copied with Ctrl+C; punishment URLs are clickable and open in your default browser.

## Configuration

The overlay writes its settings to `config.json`. The `overlay` section controls position, dimensions, opacity, font size, hotkey, and click-through behavior. `websocket` sets the local plugin endpoint. `score` configures the per-stat weights and caps; `categories` sets score thresholds.

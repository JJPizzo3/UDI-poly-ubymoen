# UByMoen

# U by Moen (UByMoen)

eisy / IoX plugin for the **U by Moen Smart Shower** (Alexa / Android account).

## Features
* Shower **On / Off**
* **Set temperature** (whole degrees Fahrenheit, 60 up to the shower's maximum, normally 115)
* **Start a preset** -- the picker shows your preset names from the Moen app
* **Toggle each outlet** (Outlet 1-4, in the order the shower reports them)
* Status: power, mode (Off / Heating / Ready / Paused / Paused by Preset), current and target temperature, active preset, time remaining, outlet states
* Live updates over the same Pusher WebSocket the Moen Android app uses, with REST polling as a fallback

## Setup
1. Install the plugin.
2. Open its configuration page and enter your U by Moen **email** and **password**.
3. Showers on the account are created as nodes automatically (or press **Discover Showers**).

## Limitations
* Unofficial: Moen has no published API. This follows the community project https://github.com/WeaveHubHQ/ha-u-by-moen (MIT) and may break if Moen changes its cloud.
* Cloud only: the eisy needs internet access; there is no local-LAN control.
* Alexa/Android accounts only; HomeKit-only systems are not supported.
* The unit of "Time Remaining" reported by Moen is not documented; it is shown as received and treated as seconds.
* Preset *creation/editing* is not supported -- manage presets in the Moen app.
* Presets 3-10 are started by sending the full preset definition (as the Android app does); presets 1 and 2 use the controller's compact form.
* Not affiliated with or endorsed by Moen or Fortune Brands.

## License Notice

This plugin's development referenced the following source(s), whose license is not on the standard permissive allowlist. Review their license terms before relying on or redistributing this plugin:

- [New device type for showers · home-assistant/architecture · Discussion #899 · GitHub](https://github.com/home-assistant/architecture/discussions/899) -- license: unknown

See LICENSE.md for full third-party attribution.

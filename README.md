# Tuya Laser Vacuum Map (Home Assistant)

Live map and room/zone cleaning for Tuya laser (LDS) robot vacuums, **fully local** after setup.

- Opens the robot's Tuya **P2P map channel on your LAN** (the same channel the app uses) and decodes
  the map (Tuya map v2), room names and the cleaning path.
- Renders a clean "floor plan" map as a camera entity, with calibration points for
  [xiaomi-vacuum-map-card](https://github.com/PiotrMachowski/lovelace-xiaomi-vacuum-map-card).
- Services `tuya_laser_map.clean_rooms`, `clean_zone`, `goto` send Tuya laser-protocol frames on the
  robot's `command_trans` DP over the LAN.

Setup signs in once with your Tuya Smart / Smart Life account to read the device's local key and P2P
password (the account password is not stored). Use it next to tuya-local / LocalTuya for the normal
vacuum controls.

Tested on: Cecotec Conga 9590 Spin Revolution (Tuya product `q3yo7zqlfxkr0cv0`).

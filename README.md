# Axpert Setting Control — Home Assistant add-on repository

This is a separate test add-on based on the existing Axpert local monitor, with MQTT setting-control support.

## Safety defaults
- Add-on boot is **manual**.
- `enable_setting_commands` defaults to **false**.
- It uses `/dev/hidraw0`, so **never start it while the existing Axpert Inverter Local add-on is running**. Only one process can own the inverter USB device.
- The add-on has a unique slug and MQTT client ID so it can be installed separately, but do not run it concurrently with the existing add-on.
- Before cutover, record the current add-on options and take a Home Assistant backup. The MQTT password must be entered in the add-on UI; do not put credentials in this repository.

## Setting request format
Publish JSON with MQTT retain disabled to `power/axpert/set`, for example:

`{"id":"test-1","setting":"output_source_priority","value":"01"}`

Supported setting keys and values:
- `output_source_priority`: `00`, `01`, `02` (POP)
- `charger_source_priority`: `01`, `02`, `03` (PCP)
- `ac_input_range`: `00`, `01` (PGR)

Results are published to `power/axpert/command_result`. Setting writes are rejected unless explicitly enabled in add-on options. The code does not accept arbitrary raw commands.

## Install as a repository
1. Create a new GitHub repository, e.g. `ha-axpert-setting-control`.
2. Upload `repository.json` and the `axpert_setting_control/` directory to the repository root.
3. Replace `REPLACE_WITH_YOUR_GITHUB_REPOSITORY_URL` in `repository.json` with the repository URL, then commit.
4. Add that GitHub repository URL in Home Assistant Settings → Apps → App store → ⋮ → Repositories.
5. Install **Axpert Setting Control (Test)** but leave it stopped until a planned cutover. Do not enable setting commands yet.

The repository is prepared locally; it has not been published to GitHub and has not been installed on the live Home Assistant system.

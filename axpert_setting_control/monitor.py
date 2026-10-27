#!/usr/bin/env python3

import argparse
from binascii import unhexlify
import json
import os
import time
import queue
import re

import crcmod
import paho.mqtt.client as mqtt


BATTERY_TYPES = {
    "0": "AGM",
    "1": "Flooded",
    "2": "User",
    "8": "Lib Protocol"
}
VOLTAGE_RANGES = {
    "0": "Appliance",
    "1": "UPS",
}
OUTPUT_SOURCES = {
    "0": "utility",
    "1": "solar",
    "2": "battery",
}
CHARGER_SOURCES = {
    "0": "utility first",
    "1": "solar first",
    "2": "solar + utility",
    "3": "solar only",
}
MACHINE_TYPES = {
    "00": "Grid tie",
    "01": "Off Grid",
    "10": "Hybrid",
}
TOPOLOGIES = {
    "0": "transformerless",
    "1": "transformer",
}
OUTPUT_MODES = {
    "0": "single machine output",
    "1": "parallel output",
    "2": "Phase 1 of 3 Phase output",
    "3": "Phase 2 of 3 Phase output",
    "4": "Phase 3 of 3 Phase output",
}
PV_OK_CONDITIONS = {
    "0": "As long as one unit of inverters has connect PV, parallel system will consider PV OK",
    "1": "Only All of inverters have connect PV, parallel system will consider PV OK",
}
PV_POWER_BALANCE = {
    "0": "PV input max current will be the max charged current",
    "1": "PV input max power will be the sum of the max charged power and loads power",
}


def mqtt_connect(*, server, username, password, client_id):
    client = mqtt.Client(client_id=client_id)
    client.username_pw_set(username, password)
    client.connect(server)
    return client

def _drain_device(file, *, duration=0.25):
    """Discard stale HID bytes for a short, bounded period."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        try:
            pending = os.read(file, 256)
            if pending:
                print(f"Discarded pending USB data: {pending!r}", flush=True)
            else:
                time.sleep(0.02)
        except BlockingIOError:
            time.sleep(0.02)
        except OSError as exc:
            # EAGAIN is normal for nonblocking HID; other errors should surface.
            if getattr(exc, "errno", None) in (11, 35):
                time.sleep(0.02)
            else:
                raise


def serial_command(device, command, *, retries=2):
    command_bytes = command.encode()
    crc_func = crcmod.predefined.mkCrcFun("xmodem")
    crc_bytes = bytes.fromhex(f"{crc_func(command_bytes):04x}")
    last_error = None

    for attempt in range(retries + 1):
        file = None
        response = b""
        try:
            print(
                f"Sending command {command} "
                f"(attempt {attempt + 1}/{retries + 1})",
                flush=True,
            )
            file = os.open(device, os.O_RDWR | os.O_NONBLOCK)
            _drain_device(file, duration=0.20)

            if command == "QPIGS2":
                # Preserve the empirically verified split-write timing for PV2.
                time.sleep(0.35)
                os.write(file, command_bytes + crc_bytes)
                time.sleep(0.35)
                os.write(file, b"\x0d" + b"\x00" * 7)
                time.sleep(0.25)
            else:
                os.write(file, command_bytes + crc_bytes + b"\x0d")

            # Use both a total deadline and an inter-byte idle deadline. If a
            # response stalls mid-frame, recover instead of waiting 20 seconds
            # and immediately firing another command into a stuck stream.
            total_deadline = time.monotonic() + 8.0
            idle_deadline = time.monotonic() + 1.5
            while b"\r" not in response:
                now = time.monotonic()
                if now >= total_deadline or (response and now >= idle_deadline):
                    raise RuntimeError(f"Incomplete USB response: {response!r}")
                try:
                    chunk = os.read(file, 256)
                    if chunk:
                        print(f"USB READ CHUNK: {chunk!r}", flush=True)
                        response += chunk
                        idle_deadline = time.monotonic() + 1.5
                    else:
                        time.sleep(0.02)
                except BlockingIOError:
                    time.sleep(0.02)

            print(f"USB RAW RESPONSE: {response!r}", flush=True)
            start = response.find(b"(")
            if start == -1:
                raise RuntimeError(f"No Axpert response marker found: {response!r}")
            frame = response[start:].split(b"\r", 1)[0]
            if len(frame) < 3:
                raise RuntimeError(f"Axpert response frame too short: {frame!r}")
            return frame[1:-2].decode()

        except Exception as exc:
            last_error = exc
            print(
                f"Command {command} failed on attempt {attempt + 1}: {exc}",
                flush=True,
            )
        finally:
            if file is not None:
                os.close(file)

        if attempt < retries:
            # Give the inverter/HID endpoint time to settle before retrying.
            delay = 2.0 * (attempt + 1)
            print(f"Waiting {delay:.1f}s before retrying {command}", flush=True)
            time.sleep(delay)

    raise RuntimeError(
        f"Command {command} failed after {retries + 1} attempts: {last_error}"
    ) from last_error


def get_serial_number(device):
    return serial_command(device, "QID")


def get_parallel_data(device):
    response = serial_command(device, "QPGS0")
    try:
        terms = response.split(" ")
        if len(terms) < 27:
            raise RuntimeError("Received fewer than 27 terms")

        return {
            "SerialNumber": int(terms[1]),
            "Mode": "grid" if terms[2] == "L" else "solar" if terms[2] == "B" else None,
            "GridVoltage": float(terms[4]),
            "GridFrequency": float(terms[5]),
            "OutputVoltage": float(terms[6]),
            "OutputFrequency": float(terms[7]),
            "OutputAparentPower": int(terms[8]),
            "OutputActivePower": int(terms[9]),
            "LoadPercentage": int(terms[10]),
            "BatteryVoltage": float(terms[11]),
            "BatteryChargingCurrent": int(terms[12]),
            "BatteryCapacity": float(terms[13]),
            "PvInputVoltage": float(terms[14]),
            "TotalChargingCurrent": int(terms[15]),
            "TotalAcOutputApparentPower": int(terms[16]),
            "TotalAcOutputActivePower": int(terms[17]),
            "TotalAcOutputPercentage": int(terms[18]),
            "InverterStatus": terms[19],
            "OutputMode": int(terms[20]),
            "ChargerSourcePriority": int(terms[21]),
            "MaxChargeCurrent": int(terms[22]),
            "MaxChargerRange": int(terms[23]),
            "MaxAcChargerCurrent": int(terms[24]),
            "PvInputCurrentForBattery": int(terms[25]),
            "BatteryDischargeCurrent": int(terms[26]),
        }
    except Exception as e:
        raise RuntimeError(f"Error parsing parallel_data ({response})") from e


def get_data(device):
    response = serial_command(device, "QPIGS")
    try:
        terms = response.split(" ")
        if len(terms) < 20:
            raise RuntimeError("Received fewer than 20 terms")

        return {
            "BusVoltage": float(terms[7]),
            "BatteryChargingCurrent": int(terms[9]),
            "InverterHeatsinkTemperature": float(terms[11]),
            "PvInputCurrent": float(terms[12]),
            "PvInputVoltage": float(terms[13]),
            "BatteryVoltageFromScc": float(terms[14]),
            "BatteryDischargeCurrent": int(terms[15]),
            "DeviceStatus": terms[16],
            "PvInputPower": int(terms[19]),
        }
    except Exception as e:
        raise RuntimeError(f"Error parsing data ({response})") from e

def get_pv2_data(device):
    response = serial_command(device, "QPIGS2")
    try:
        terms = response.split()
        if len(terms) < 3:
            raise RuntimeError("Received fewer than 3 PV2 terms")

        return {
            "Pv2InputCurrent": float(terms[0]),
            "Pv2InputVoltage": float(terms[1]),
            "Pv2InputPower": int(terms[2]),
        }
    except Exception as e:
        raise RuntimeError(f"Error parsing PV2 data ({response})") from e


def get_settings(device):
    response = serial_command(device, "QPIRI")
    try:
        terms = response.split(" ")
        if len(terms) < 26:
            raise RuntimeError("Received fewer than 26 terms")

        return {
            "AcInputVoltage": float(terms[0]),
            "AcInputCurrent": float(terms[1]),
            "AcOutputVoltage": float(terms[2]),
            "AcOutputFrequency": float(terms[3]),
            "AcOutputCurrent": float(terms[4]),
            "AcOutputApparentPower": int(terms[5]),
            "AcOutputActivePower": int(terms[6]),
            "BatteryVoltage": float(terms[7]),
            "BatteryRechargeVoltage": float(terms[8]),
            "BatteryUnderVoltage": float(terms[9]),
            "BatteryBulkVoltage": float(terms[10]),
            "BatteryFloatVoltage": float(terms[11]),
            "BatteryType": BATTERY_TYPES[terms[12]],
            "MaxAcChargingCurrent": int(terms[13]),
            "MaxChargingCurrent": int(terms[14]),
            "InputVoltageRange": VOLTAGE_RANGES[terms[15]],
            "OutputSourcePriority": OUTPUT_SOURCES[terms[16]],
            "ChargerSourcePriority": CHARGER_SOURCES[terms[17]],
            "MaxParallelUnits": int(terms[18]),
            "MachineType": MACHINE_TYPES[terms[19]],
            "Topology": TOPOLOGIES[terms[20]],
            "OutputMode": OUTPUT_MODES[terms[21]],
            "BatteryRedischargeVoltage": float(terms[22]),
            "PvOkCondition": PV_OK_CONDITIONS[terms[23]],
            "PvPowerBalance": PV_POWER_BALANCE[terms[24]],
            "MaxBatteryCvChargingTime": int(terms[25]),
        }
    except Exception as e:
        raise RuntimeError(f"Error parsing settings ({response})") from e


def send_data(client, topic, data):
    try:
        client.publish(topic, data, 0, True)
    except Exception as e:
        raise RuntimeError("Error sending data to mqtt server") from e



# MQTT setting-control API. Only explicitly allowlisted commands are accepted.
# Values are deliberately narrow; do not expose arbitrary raw serial commands.
SETTING_COMMANDS = {
    "output_source_priority": {"prefix": "POP", "values": {"00", "01", "02"}},
    "charger_source_priority": {"prefix": "PCP", "values": {"01", "02", "03"}},
    "ac_input_range": {"prefix": "PGR", "values": {"00", "01"}},
}


def validate_setting_request(payload, enabled):
    """Return (request_id, command, error). Never accept arbitrary commands."""
    request_id = "unknown"
    try:
        request = json.loads(payload)
        if not isinstance(request, dict):
            raise ValueError("Payload must be a JSON object")
        request_id = str(request.get("id", "unknown"))[:80]
        if not enabled:
            raise ValueError("Setting commands are disabled in add-on options")
        setting = request.get("setting")
        value = request.get("value")
        if setting not in SETTING_COMMANDS:
            raise ValueError("Unsupported setting; allowed: " + ", ".join(SETTING_COMMANDS))
        value = str(value)
        definition = SETTING_COMMANDS[setting]
        if value not in definition["values"]:
            raise ValueError(f"Invalid value for {setting}; allowed values: {', '.join(sorted(definition['values']))}")
        return request_id, definition["prefix"] + value, None
    except Exception as exc:
        return request_id, None, str(exc)


def handle_command_message(client, message, command_queue, enabled, result_topic):
    payload = message.payload.decode("utf-8", "replace")
    request_id, command, error = validate_setting_request(payload, enabled)

    # A retained MQTT command can be replayed automatically after add-on restart.
    # Never execute retained setting writes, even when otherwise valid.
    if getattr(message, "retain", False):
        error = "Retained MQTT commands are not permitted; publish setting requests with retain=false"

    if error:
        result = {"id": request_id, "status": "rejected", "error": error}
        client.publish(result_topic, json.dumps(result), qos=1, retain=False)
        print(f"Rejected MQTT setting request {request_id}: {error}", flush=True)
        return
    try:
        command_queue.put_nowait((request_id, command))
    except queue.Full:
        result = {"id": request_id, "status": "rejected", "error": "Setting command queue is full; try again later"}
        client.publish(result_topic, json.dumps(result), qos=1, retain=False)
        print(f"Rejected MQTT setting request {request_id}: command queue full", flush=True)
        return
    print(f"Queued MQTT setting request {request_id}: {command[:3]} (value hidden in log)", flush=True)


def process_one_setting_command(device, client, command_queue, result_topic):
    """Write one allowlisted setting, then verify it via QPIRI readback."""
    try:
        request_id, command = command_queue.get_nowait()
    except queue.Empty:
        return

    setting_by_prefix = {
        "POP": ("OutputSourcePriority", OUTPUT_SOURCES),
        "PCP": ("ChargerSourcePriority", CHARGER_SOURCES),
        "PGR": ("InputVoltageRange", VOLTAGE_RANGES),
    }
    try:
        response = serial_command(device, command, retries=0)
        accepted = response.strip().upper().startswith("ACK")
        result = {"id": request_id, "response": response}
        if not accepted:
            result["status"] = "rejected_by_inverter"
        else:
            result["status"] = "acknowledged_readback_pending"
            prefix, value = command[:3], command[3:]
            field, value_map = setting_by_prefix[prefix]
            expected = value_map.get(value.lstrip("0") or "0")
            # Allow the inverter time to apply the setting before querying QPIRI.
            time.sleep(0.5)
            try:
                actual = get_settings(device)[field]
                result.update({"expected": expected, "actual": actual})
                result["status"] = "verified" if actual == expected else "acknowledged_but_readback_mismatch"
            except Exception as readback_error:
                result.update({"expected": expected, "status": "acknowledged_readback_failed", "readback_error": str(readback_error)})
        print(f"Setting request {request_id} finished with status {result['status']}", flush=True)
    except Exception as exc:
        result = {"id": request_id, "status": "error", "error": str(exc)}
        print(f"Setting request {request_id} failed: {exc}", flush=True)
    client.publish(result_topic, json.dumps(result), qos=1, retain=False)

def main(
    *,
    device,
    mqtt_server,
    mqtt_user,
    mqtt_pass,
    mqtt_client_id,
    mqtt_topic_settings,
    mqtt_topic_parallel,
    mqtt_topic,
    mqtt_topic_command="power/axpert/set",
    mqtt_topic_command_result="power/axpert/command_result",
    enable_setting_commands=False,
    sleep_query=0,
    sleep_iteration=0,
):
    client = mqtt_connect(
        server=mqtt_server,
        username=mqtt_user,
        password=mqtt_pass,
        client_id=mqtt_client_id,
    )
    command_queue = queue.Queue(maxsize=10)

    def on_connect(mqtt_client, userdata, flags, rc):
        if rc == 0:
            mqtt_client.subscribe(mqtt_topic_command, qos=1)
            print(f"Subscribed to setting requests: {mqtt_topic_command}", flush=True)
        else:
            print(f"MQTT command subscription failed with rc={rc}", flush=True)

    def on_message(mqtt_client, userdata, message):
        handle_command_message(
            mqtt_client, message, command_queue, enable_setting_commands,
            mqtt_topic_command_result,
        )

    client.on_connect = on_connect
    client.on_message = on_message
    client.loop_start()

    print("Querying inverter serial number...", flush=True)
    # USB may not be ready immediately when the add-on/container starts.
    # Keep retrying instead of exiting permanently during startup.
    while True:
        try:
            serial_number = get_serial_number(device)
            break
        except Exception as e:
            print(
                f"WARNING: could not read inverter serial number; "
                f"retrying in 5 seconds: {e}",
                flush=True,
            )
            time.sleep(5)

    print(f"Reading from inverter {serial_number}\n", flush=True)
    mqtt_topic = mqtt_topic.replace("{sn}", str(serial_number))

    while True:
        start = time.time()

        # A single failed USB query must not terminate the monitoring process.
        try:
            data = json.dumps(get_parallel_data(device))
            print("parallel_data", data, "\n", flush=True)
            send_data(client, mqtt_topic_parallel, data)
        except Exception as e:
            print(f"WARNING: parallel-data query/publish failed; will retry next cycle: {e}", flush=True)
        time.sleep(max(0.15, sleep_query))

        # Read PV1/inverter data independently, so a PV2 read failure does not
        # prevent the existing PV1 sensors from updating.
        pv_data = None
        try:
            pv_data = get_data(device)
        except Exception as e:
            print(f"WARNING: PV1/inverter-data query failed; will retry next cycle: {e}", flush=True)

        if pv_data is not None:
            try:
                pv_data.update(get_pv2_data(device))
                pv_data["TotalPvInputPower"] = (
                    pv_data["PvInputPower"] + pv_data["Pv2InputPower"]
                )
            except Exception as e:
                # Publish PV1 readings even if the optional PV2 query fails.
                print(f"WARNING: PV2 query failed; publishing PV1 data only: {e}", flush=True)

            data = json.dumps(pv_data)
            print("data", data, "\n", flush=True)
            try:
                send_data(client, mqtt_topic, data)
            except Exception as e:
                print(f"WARNING: inverter-data MQTT publish failed: {e}", flush=True)
        time.sleep(max(0.15, sleep_query))

        try:
            data = json.dumps(get_settings(device))
            print("settings", data, "\n", flush=True)
            send_data(client, mqtt_topic_settings, data)
        except Exception as e:
            print(f"WARNING: settings query/publish failed; will retry next cycle: {e}", flush=True)
        time.sleep(max(0.15, sleep_query))

        # Commands share this process and USB device with telemetry; no second
        # process is permitted to open /dev/hidraw0 for setting changes.
        process_one_setting_command(device, client, command_queue, mqtt_topic_command_result)

        time.sleep(max(0, sleep_iteration - (time.time() - start)))


if __name__ == "__main__":
    def env(var, val=None):
        return {"default": os.environ.get(var)} if os.environ.get(var) else \
               {"default": val} if val is not None else \
               {"required": True}
    parser = argparse.ArgumentParser(description="""
        Monitor inverter parameters and send them to an MQTT server.
        Arguments can also be set using their corresponding environment variables.
    """)
    parser.add_argument("--device", **env("DEVICE"), help="Inverter IO device")
    parser.add_argument("--mqtt-server", **env("MQTT_SERVER"), help="MQTT server address")
    parser.add_argument("--mqtt-user", **env("MQTT_USER"), help="MQTT username")
    parser.add_argument("--mqtt-pass", **env("MQTT_PASS"), help="MQTT password")
    parser.add_argument("--mqtt-client-id", **env("MQTT_CLIENT_ID"), help="MQTT client id")
    parser.add_argument("--mqtt-topic-settings", **env("MQTT_TOPIC_SETTINGS"), help="MQTT topic for settings")
    parser.add_argument("--mqtt-topic-parallel", **env("MQTT_TOPIC_PARALLEL"), help="MQTT topic for parallel data")
    parser.add_argument("--mqtt-topic", **env("MQTT_TOPIC"), help="MQTT topic for data")
    parser.add_argument("--mqtt-topic-command", **env("MQTT_TOPIC_COMMAND", "power/axpert/set"), help="MQTT topic for setting requests")
    parser.add_argument("--mqtt-topic-command-result", **env("MQTT_TOPIC_COMMAND_RESULT", "power/axpert/command_result"), help="MQTT topic for setting results")
    parser.add_argument("--enable-setting-commands", type=lambda x: str(x).lower() in ("1", "true", "yes", "on"), **env("ENABLE_SETTING_COMMANDS", False), help="Enable allowlisted setting writes")
    parser.add_argument("--sleep-query", type=float, **env("SLEEP_QUERY", 1), help="Seconds between queries")
    parser.add_argument("--sleep-iteration", type=float, **env("SLEEP_ITERATION", 5), help="Seconds between iteration starts")
    args = parser.parse_args()

    main(
        device=args.device,
        mqtt_server=args.mqtt_server,
        mqtt_user=args.mqtt_user,
        mqtt_pass=args.mqtt_pass,
        mqtt_client_id=args.mqtt_client_id,
        mqtt_topic_settings=args.mqtt_topic_settings,
        mqtt_topic_parallel=args.mqtt_topic_parallel,
        mqtt_topic=args.mqtt_topic,
        mqtt_topic_command=args.mqtt_topic_command,
        mqtt_topic_command_result=args.mqtt_topic_command_result,
        enable_setting_commands=args.enable_setting_commands,
        sleep_query=args.sleep_query,
        sleep_iteration=args.sleep_iteration,
    )

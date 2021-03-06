# Author: Kevin Köck
# Copyright Kevin Köck 2019 Released under the MIT license
# Created on 2019-03-31

"""
Datasheet: https://www.mpja.com/download/hc-sr04_ultrasonic_module_user_guidejohn.pdf

Warning: My sensor only read distances reliable with flat surfaces within 80cm. 
Above that the results were fluctuating heavily.

example config:
{
    package: .sensors.hcsr04
    component: HCSR04
    constructor_args: {
        pin_trigger: D5
        pin_echo: D6
        # timeout: 30000        # optional, defaults to 30000 which is around sensor maximum distance
        # temp_sensor: "ds18"   # optional, name of temperature sensor if temperature compensated measurement is needed
        # precision: 2          # precision of the distance value published
        # offset: 0             # offset for distance value to compensate bad sensor reading offsets
        # interval_publish: 600   #optional, defaults to 600. Set to interval_reading to publish with every reading
        # interval_reading: 120   # optional, defaults to 120. -1 means do not automatically read sensor and publish values
        # mqtt_topic: null      # optional, distance gets published to this topic
        # mqtt_topic_interval: null     # optional, topic need to have /set at the end. Interval can be changed here
        # value_template: "{{ 60.0 - float(value) }}" # optional, can be used to measure the reverse distance (e.g. water level)
        # friendly_name: "Distance" # optional, friendly name for homeassistant gui by mqtt discovery
        # discover: true            # optional, if false no discovery message for homeassistant will be sent.
        # expose_intervals: Expose intervals to mqtt so they can be changed remotely
        # intervals_topic: if expose_intervals then use this topic to change intervals. Defaults to <home>/<device-id>/<COMPONENT_NAME><_unit_index>/interval/set. Send a dictionary with keys "reading" and/or "publish" to change either/both intervals.
    }
}
# interval change can't be discovered as homeassistant doesn't offer a type
"""

__updated__ = "2019-11-02"
__version__ = "0.8"

from pysmartnode.components.machine.pin import Pin
from pysmartnode.utils.component.sensor import ComponentSensor, SENSOR_TEMPERATURE, \
    VALUE_TEMPLATE_FLOAT
from pysmartnode import config
from pysmartnode import logging
import uasyncio as asyncio
import machine
import time
import gc

COMPONENT_NAME = "hcsr04"

DISCOVERY_DISTANCE = '"unit_of_meas":"cm",' \
                     '"val_tpl":"{{ value|float }}",' \
                     '"ic":"mdi:axis-arrow",'

_log = logging.getLogger(COMPONENT_NAME)
_mqtt = config.getMQTT()
gc.collect()

_unit_index = -1


class HCSR04(ComponentSensor):
    def __init__(self, pin_trigger, pin_echo, timeout=30000, temp_sensor: ComponentSensor = None,
                 precision=2, offset=0,
                 interval_publish=None, interval_reading=None, mqtt_topic=None,
                 value_template=None, friendly_name=None,
                 discover=True, expose_intervals=False, intervals_topic=None):
        """
        HC-SR04 ultrasonic sensor.
        Be sure to connect it to 5V but use a voltage divider to connect the Echo pin to an ESP.
        :param pin_trigger: pin number/object of trigger pin
        :param pin_echo: pin number/object of echo pin
        :param timeout: reading timeout, corresponds to sensor limit range of 4m
        :param temp_sensor: temperature sensor object
        :param precision: int, precision of distance value published and returned
        :param offset: float, distance value offset, shouldn't be needed
        :param interval_publish: seconds, set to interval_reading to publish every reading. -1 for not publishing.
        :param interval_reading: seconds, set to -1 for not reading/publishing periodically. >0 possible for reading, 0 not allowed for reading..
        :param mqtt_topic: distance mqtt topic
        :param value_template: optional template can be used to measure the reverse distance (e.g. water level)
        :param friendly_name: friendly name for homeassistant gui by mqtt discovery, defaults to "Distance"
        :param discover: boolean, if the device should publish its discovery
        :param expose_intervals: Expose intervals to mqtt so they can be changed remotely
        :param intervals_topic: if expose_intervals then use this topic to change intervals.
        Defaults to <home>/<device-id>/<COMPONENT_NAME><_unit_index>/interval/set
        Send a dictionary with keys "reading" and/or "publish" to change either/both intervals.
        """
        global _unit_index
        _unit_index += 1
        super().__init__(COMPONENT_NAME, __version__, _unit_index, discover, interval_publish,
                         interval_reading, mqtt_topic, _log, expose_intervals, intervals_topic)
        self._tr = Pin(pin_trigger, mode=machine.Pin.OUT)
        self._tr.value(0)
        self._ec = Pin(pin_echo, mode=machine.Pin.IN)
        self._to = timeout
        self.checkSensorType(temp_sensor, SENSOR_TEMPERATURE)
        self._temp: ComponentSensor = temp_sensor
        self._addSensorType("distance", precision, offset, value_template or VALUE_TEMPLATE_FLOAT,
                            "cm", friendly_name, discovery_type=DISCOVERY_DISTANCE)

    def _pulse(self) -> int:
        """
        Send a pulse and wait for the echo pin using machine.time_pulse_us() to measure us.
        :return: int
        """
        tr = self._tr
        tr.value(0)
        time.sleep_us(5)
        tr.value(1)
        time.sleep_us(10)
        tr.value(0)
        try:
            return machine.time_pulse_us(self._ec, 1, self._to)
        except OSError as e:
            if e.args[0] == 100:  # TIMEOUT
                raise OSError("Object too far")
            raise e

    async def _read(self):
        if self._temp is not None:
            temp = await self._temp.getValue(SENSOR_TEMPERATURE, publish=self._intrd > 5,
                                             timeout=5 if self._intrd > 20 else 0)
            if temp is None:  # only log errors if reading interval allows it
                await _log.asyncLog("warn",
                                    "Couldn't read temp sensor, using fallback calculation",
                                    timeout=10 if self._intrd > 20 else 0)
        else:
            temp = None
        val = []
        warnings = 0  # probably not going to happen that both warning types occur at the same time
        warning = "minimum distance reached or different problem"
        for _ in range(20):
            try:
                pt = self._pulse()
                if pt > 175:  # ~3cm, sensor minimum distance, often read although other problems
                    val.append(pt)
                else:
                    warnings += 1
            except OSError as e:
                warning = e
                warnings += 1
            await asyncio.sleep_ms(10)
        if warnings > 10:  # half sensor readings are bad
            await _log.asyncLog("error",
                                "Too many bad sensor readings, error:", warning,
                                timeout=10 if self._intrd > 20 else 0)
            return
        # removing extreme values
        val.remove(max(val))
        val.remove(max(val))
        val.remove(min(val))
        val.remove(min(val))
        pt = 0
        for v in val:
            pt += v
        pt /= len(val)
        if temp is None:
            dt = (pt / 2) / 29.14
        else:
            dt = (pt / 2) * ((331.5 + (0.6 * temp)) / 10000)
        if dt < 0:
            await _log.asyncLog("warn", "Sensor reading <0", timeout=10 if self._intrd > 20 else 0)
            return
        await self._setValue("distance", dt, timeout=10 if self._intrd > 20 else 0)

from homeassistant.helpers import entity
from homeassistant.core import HomeAssistant
from homeassistant.util import dt
import logging
from suntime import Sun
from datetime import date, timedelta


class RightLight:
    """RightLight object to control a single light or light group"""

    def __init__(self, ent: entity, hass: HomeAssistant) -> None:
        self._entity = ent
        self._hass = hass

        self._mode = "Off"
        self.today = None

        self._logger = logging.getLogger(f"RightLight({self._entity})")

        self.trip_points = {}

        self._ct_high = 5000
        self._ct_scalar = 0.35

        self._br_over_ct_mult = 6

        self.on_transition = 0.1
        self.on_color_transition = 0.1
        self.off_transition = 0.1
        self.dim_transition = 0.1

        # Store callback for cancelling scheduled next event
        self._currSched = []

        cd = self._hass.config.as_dict()
        self.sun = Sun(cd["latitude"], cd["longitude"])

        self._getNow()

    async def turn_on(self, **kwargs) -> None:
        """
        Turns on a RightLight-controlled entity

        :key brightness: The master brightness control
        :key brightness_override: Additional brightness to add on to RightLight's calculated brightness
        :key mode: One of the trip_point key names (Normal, Vivid, Bright, One, Two)
        """
        # Cancel any pending eventloop schedules
        self._cancelSched()

        self._getNow()

        self._mode = kwargs.get("mode", "Normal")
        self._brightness = kwargs.get("brightness", 255)
        self._brightness_override = kwargs.get("brightness_override", 0)

        # Find trip points around current time
        for next in range(0, len(self.trip_points[self._mode])):
            if self.trip_points[self._mode][next][0] >= self.now:
                break
        prev = next - 1

        # Calculate how far through the trip point span we are now
        prev_time = self.trip_points[self._mode][prev][0]
        next_time = self.trip_points[self._mode][next][0]
        time_ratio = (self.now - prev_time) / (next_time - prev_time)
        time_rem = (next_time - self.now).seconds

        self._logger.error(f"Now: {self.now}")
        self._logger.error(
            f"Prev/Next: {prev}, {next}, {prev_time}, {next_time}, {time_ratio}"
        )

        if self._mode == "Normal":
            # Compute br/ct for previous point
            br_max_prev = self.trip_points["Normal"][prev][2] / 255
            br_prev = br_max_prev * (self._brightness + self._brightness_override)

            ct_max_prev = self.trip_points["Normal"][prev][1]
            ct_delta_prev = (
                (self._ct_high - ct_max_prev) * (1 - br_max_prev) * self._ct_scalar
            )
            ct_prev = ct_max_prev - ct_delta_prev

            # Compute br/ct for next point
            br_max_next = self.trip_points["Normal"][next][2] / 255
            br_next = br_max_next * (self._brightness + self._brightness_override)

            ct_max_next = self.trip_points["Normal"][next][1]
            ct_delta_next = (
                (self._ct_high - ct_max_next) * (1 - br_max_next) * self._ct_scalar
            )
            ct_next = ct_max_next - ct_delta_next

            self._logger.error(f"Prev/Next: {br_prev}/{ct_prev}, {br_next}/{ct_next}")

            # Scale linearly to current time
            br = (br_next - br_prev) * time_ratio + br_prev
            ct = (ct_next - ct_prev) * time_ratio + ct_prev

            if br > 255:
                br_over = br - 255
                br = 255
                ct = ct + br_over * self._br_over_ct_mult
            if br_next > 255:
                br_next = 255

            self._logger.error(f"Final: {br}/{ct} -> {time_rem}sec")

            # Turn on light to interpolated values
            await self._hass.services.async_call(
                "light",
                "turn_on",
                {
                    "entity_id": self._entity,
                    "brightness": br,
                    "kelvin": ct,
                    "transition": self.on_transition,
                },
                blocking=True,
                limit=2,
            )

            # Transition to next values
            self._hass.loop.call_later(
                self.on_transition + 0.5,
                self._hass.loop.create_task,
                self._turn_on_specific(
                    {
                        "entity_id": self._entity,
                        "brightness": br_next,
                        "kelvin": ct_next,
                        "transition": time_rem,
                    }
                ),
            )

            # Schedule another turn_on at next_time to start the next transition
            ret = self._hass.loop.call_later(
                (next_time - self.now).seconds + 1,
                self._hass.loop.create_task,
                self.turn_on(
                    brightness=self._brightness,
                    brightness_override=self._brightness_override,
                ),
            )
            self._addSched(ret)

        else:
            prev_rgb = self.trip_points[self._mode][prev][1]
            next_rgb = self.trip_points[self._mode][next][1]

            self._logger.error(f"Prev/Next: {prev_rgb}/{next_rgb}")

            r_now = prev_rgb[0] + (next_rgb[0] - prev_rgb[0]) * time_ratio
            g_now = prev_rgb[1] + (next_rgb[1] - prev_rgb[1]) * time_ratio
            b_now = prev_rgb[2] + (next_rgb[2] - prev_rgb[2]) * time_ratio
            now_rgb = [r_now, g_now, b_now]

            self._logger.error(f"Final: {now_rgb} -> {time_rem}sec")

            # Turn on light to interpolated values
            await self._hass.services.async_call(
                "light",
                "turn_on",
                {
                    "entity_id": self._entity,
                    "brightness": 255,
                    "rgb_color": now_rgb,
                    "transition": self.on_color_transition,
                },
                blocking=True,
                limit=2,
            )

            # Transition to next values
            self._hass.loop.call_later(
                self.on_color_transition + 0.5,
                self._hass.loop.create_task,
                self._turn_on_specific(
                    {
                        "entity_id": self._entity,
                        "brightness": 255,
                        "rgb_color": next_rgb,
                        "transition": time_rem,
                    }
                ),
            )

            # Schedule another turn on at next_time to start the next transition
            ret = self._hass.loop.call_later(
                (next_time - self.now).seconds + 1,
                self._hass.loop.create_task,
                self.turn_on(mode=self._mode),
            )
            self._addSched(ret)

    async def _turn_on_specific(self, data) -> None:
        """Disables RightLight functionality and sets light to values in 'data' variable"""
        self._logger.error(f"_turn_on_specific: {data}")
        await self._hass.services.async_call("light", "turn_on", data)

    async def turn_on_specific(self, data) -> None:
        """External version of _turn_on_specific that runs twice to ensure successful transition"""
        await self.disable()

        data["transition"] = 0.2
        if not "brightness" in data:
            data["brightness"] = 255

        await self._turn_on_specific(data)
        self._hass.loop.call_later(
            0.6, self._hass.loop.create_task, self._turn_on_specific(data)
        )

    async def disable_and_turn_off(self):
        # Cancel any pending eventloop schedules
        self._cancelSched()

        self._brightness = 0
        await self._hass.services.async_call(
            "light",
            "turn_off",
            {"entity_id": self._entity, "transition": self.off_transition},
        )

    async def disable(self):
        # Cancel any pending eventloop schedules
        self._cancelSched()

    def _cancelSched(self):
        for ret in self._currSched:
            ret.cancel()

    def _addSched(self, ret):
        # FIFO of event callbacks to ensure all are properly cancelled
        if len(self._currSched) >= 3:
            self._currSched.pop(0)
        self._currSched.append(ret)

    def _getNow(self):
        self.now = dt.now()
        rerun = date.today() != self.today
        self.today = date.today()

        if rerun:
            self.sunrise = dt.as_local(self.sun.get_sunrise_time())
            self.sunset = dt.as_local(self.sun.get_sunset_time())
            self.sunrise = self.sunrise.replace(
                day=self.now.day, month=self.now.month, year=self.now.year
            )
            self.sunset = self.sunset.replace(
                day=self.now.day, month=self.now.month, year=self.now.year
            )
            self.midnight_early = self.now.replace(
                microsecond=0, second=0, minute=0, hour=0
            )
            self.midnight_late = self.now.replace(
                microsecond=0, second=59, minute=59, hour=23
            )

            self.defineTripPoints()

    def defineTripPoints(self):
        self.trip_points["Normal"] = []
        timestep = timedelta(minutes=2)

        self.trip_points["Normal"].append(
            [self.midnight_early, 2500, 150]
        )  # Midnight morning
        self.trip_points["Normal"].append(
            [self.sunrise - timedelta(minutes=60), 2500, 120]
        )  # Sunrise - 60
        self.trip_points["Normal"].append(
            [self.sunrise - timedelta(minutes=30), 2700, 170]
        )  # Sunrise - 30
        self.trip_points["Normal"].append([self.sunrise, 3200, 155])  # Sunrise
        self.trip_points["Normal"].append(
            [self.sunrise + timedelta(minutes=30), 4700, 255]
        )  # Sunrise + 30
        self.trip_points["Normal"].append(
            [self.sunset - timedelta(minutes=90), 4200, 255]
        )  # Sunset - 90
        self.trip_points["Normal"].append(
            [self.sunset - timedelta(minutes=30), 3200, 255]
        )  # Sunset = 30
        self.trip_points["Normal"].append([self.sunset, 2700, 255])  # Sunset
        self.trip_points["Normal"].append(
            [self.now.replace(microsecond=0, second=0, minute=30, hour=22), 2500, 255]
        )  # 10:30
        self.trip_points["Normal"].append(
            [self.midnight_late, 2500, 150]
        )  # Midnight night

        vivid_trip_points = [
            [255, 0, 0],
            [202, 0, 127],
            [130, 0, 255],
            [0, 0, 255],
            [0, 90, 190],
            [0, 200, 200],
            [0, 255, 0],
            [255, 255, 0],
            [255, 127, 0],
        ]

        bright_trip_points = [
            [255, 100, 100],
            [202, 80, 127],
            [150, 70, 255],
            [90, 90, 255],
            [60, 100, 190],
            [70, 200, 200],
            [80, 255, 80],
            [255, 255, 0],
            [255, 127, 70],
        ]

        one_trip_points = [[0, 104, 255], [255, 0, 255]]

        two_trip_points = [[255, 0, 255], [0, 104, 255]]

        # Loop to create vivid trip points
        temp = self.midnight_early
        this_ptr = 0
        self.trip_points["Vivid"] = []
        while temp < self.midnight_late:
            self.trip_points["Vivid"].append([temp, vivid_trip_points[this_ptr]])

            temp = temp + timestep

            this_ptr = this_ptr + 1
            if this_ptr >= len(vivid_trip_points):
                this_ptr = 0

        # Loop to create bright trip points
        temp = self.midnight_early
        this_ptr = 0
        self.trip_points["Bright"] = []
        while temp < self.midnight_late:
            self.trip_points["Bright"].append([temp, bright_trip_points[this_ptr]])

            temp = temp + timestep

            this_ptr = this_ptr + 1
            if this_ptr >= len(bright_trip_points):
                this_ptr = 0

        # Loop to create 'one' trip points
        temp = self.midnight_early
        this_ptr = 0
        self.trip_points["One"] = []
        while temp < self.midnight_late:
            self.trip_points["One"].append([temp, one_trip_points[this_ptr]])

            temp = temp + timestep

            this_ptr = this_ptr + 1
            if this_ptr >= len(one_trip_points):
                this_ptr = 0

        # Loop to create 'two' trip points
        temp = self.midnight_early
        this_ptr = 0
        self.trip_points["Two"] = []
        while temp < self.midnight_late:
            self.trip_points["Two"].append([temp, two_trip_points[this_ptr]])

            temp = temp + timestep

            this_ptr = this_ptr + 1
            if this_ptr >= len(two_trip_points):
                this_ptr = 0

    def getColorModes(self):
        return self.trip_points.keys()

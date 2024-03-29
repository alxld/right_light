from homeassistant.helpers import entity
from homeassistant.core import HomeAssistant
from homeassistant.util import dt
import logging, random, asyncio
from suntime import Sun
from datetime import date, timedelta


class RightLight:
    """RightLight object to control a single light or light group"""

    def __init__(self, ent: entity, hass: HomeAssistant, debug=False) -> None:
        self._entity = ent
        self._hass = hass
        self._debug = debug

        self._mode = "Off"
        self.today = None

        self._logger = logging.getLogger(f"RightLight({self._entity})")

        self.trip_points = {}

        self._ct_high = 5000
        self._ct_scalar = 0.35

        self._br_over_ct_mult = 6

        self.on_transition = 0.2
        self.on_color_transition = 0.2
        self.off_transition = 0.2
        self.dim_transition = 0.2

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
        :key mode: One of the trip_point key names (Normal, Vivid, Fun1, Fun2, Bright, One, Two)
        """
        # Cancel any pending eventloop schedules
        nocancel = kwargs.get("nocancel", False)
        if not nocancel:
            self._cancelSched()

        self._getNow()

        self._mode = kwargs.get("mode", "Normal")
        self._brightness = kwargs.get("brightness", 255)
        self._brightness_override = kwargs.get("brightness_override", 0)
        this_transition = kwargs.get("transition", self.on_transition)

        if self._debug:
            self._logger.error(f"RightLight turn_on: {kwargs}")
            self._logger.error(
                f"RightLight turn_on: {self._mode}, {self._brightness}, {self._brightness_override}"
            )

        if self._brightness == 0:
            await self.disable_and_turn_off(**kwargs)
            return

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

        if self._debug:
            self._logger.error(f"Now: {self.now}")
            self._logger.error(
                f"Prev/Next: {prev}, {next}, {prev_time}, {next_time}, {time_ratio}"
            )

        if self._mode == "Normal":
            # Compute br/ct for previous point
            br_max_prev = self.trip_points["Normal"][prev][1][1] / 255
            br_prev = br_max_prev * (self._brightness + self._brightness_override)

            ct_max_prev = self.trip_points["Normal"][prev][1][0]
            ct_delta_prev = (
                (self._ct_high - ct_max_prev) * (1 - br_max_prev) * self._ct_scalar
            )
            ct_prev = ct_max_prev - ct_delta_prev

            # Compute br/ct for next point
            br_max_next = self.trip_points["Normal"][next][1][1] / 255
            br_next = br_max_next * (self._brightness + self._brightness_override)

            ct_max_next = self.trip_points["Normal"][next][1][0]
            ct_delta_next = (
                (self._ct_high - ct_max_next) * (1 - br_max_next) * self._ct_scalar
            )
            ct_next = ct_max_next - ct_delta_next

            if self._debug:
                self._logger.error(
                    f"Prev/Next: {br_prev}/{ct_prev}, {br_next}/{ct_next}"
                )

            # Scale linearly to current time
            br = (br_next - br_prev) * time_ratio + br_prev
            ct = (ct_next - ct_prev) * time_ratio + ct_prev

            if br > 255:
                br_over = br - 255
                br = 255
                ct = ct + br_over * self._br_over_ct_mult
            if br_next > 255:
                br_next = 255

            if self._debug:
                self._logger.error(f"Final: {br}/{ct} -> {time_rem}sec")

            # Turn on light to interpolated values
            await self._hass.services.async_call(
                "light",
                "turn_on",
                {
                    "entity_id": self._entity,
                    "brightness": br,
                    "kelvin": ct,
                    # "transition": self.on_transition,
                    "transition": this_transition,
                    # "effect": "stop_effect",
                },
                blocking=True,
                # limit=2,
            )

            # Transition to next values, but schedule it some random time in the near future
            #            ret = self._hass.loop.create_task(
            #                self.delay_run(
            #                    random.randrange(1, 5),
            #                    self.turn_on_specific,
            #                    {
            #                        "entity_id": self._entity,
            #                        "brightness": br_next,
            #                        "kelvin": ct_next,
            #                        "transition": time_rem,
            #                    },
            #                )
            #            )
            #            self._addSched(ret)
            #            await ret

            ret = self._hass.loop.create_task(
                self.delay_run(
                    random.randrange(1, 5),
                    self._hass.services.async_call,
                    "light",
                    "turn_on",
                    {
                        "entity_id": self._entity,
                        "brightness": br_next,
                        "kelvin": ct_next,
                        "transition": time_rem,
                    },
                )
            )
            self._addSched(ret)

            # Schedule another turn_on at next_time to start the next transition
            # Add 1 second to ensure next event is after trigger point
            ret = self._hass.loop.create_task(
                self.delay_run(
                    (next_time - self.now).seconds + 1,
                    self.turn_on,
                    brightness=self._brightness,
                    brightness_override=self._brightness_override,
                    nocancel=True,
                )
            )
            self._addSched(ret)
            # await ret

        else:
            prev_rgb = self.trip_points[self._mode][prev][1]
            next_rgb = self.trip_points[self._mode][next][1]

            if self._debug:
                self._logger.error(f"Prev/Next: {prev_rgb}/{next_rgb}")

            r_now = prev_rgb[0] + (next_rgb[0] - prev_rgb[0]) * time_ratio
            g_now = prev_rgb[1] + (next_rgb[1] - prev_rgb[1]) * time_ratio
            b_now = prev_rgb[2] + (next_rgb[2] - prev_rgb[2]) * time_ratio
            now_rgb = [r_now, g_now, b_now]

            if self._debug:
                self._logger.error(f"Final: {now_rgb} -> {time_rem}sec")

            # Turn on light to interpolated values
            await self._hass.services.async_call(
                "light",
                "turn_on",
                {
                    "entity_id": self._entity,
                    "brightness": self._brightness,
                    "rgb_color": now_rgb,
                    # "transition": self.on_color_transition,
                    "transition": this_transition,
                },
                blocking=True,
                # limit=2,
            )

            # Transition to next values
            # ret = self._hass.loop.create_task(
            #    self.delay_run(
            #        random.randrange(10, 30),
            #        self.turn_on_specific,
            #        {
            #            "entity_id": self._entity,
            #            "brightness": 255,
            #            "rgb_color": next_rgb,
            #            "transition": time_rem,
            #        },
            #    )
            # )
            # self._addSched(ret)
            # await ret

            ret = self._hass.loop.create_task(
                self.delay_run(
                    random.randrange(1, 5),
                    self._hass.services.async_call,
                    "light",
                    "turn_on",
                    {
                        "entity_id": self._entity,
                        "brightness": self._brightness,
                        "rgb_color": next_rgb,
                        "transition": time_rem,
                    },
                )
            )
            self._addSched(ret)

            # Schedule another turn on at next_time to start the next transition
            ret = self._hass.loop.create_task(
                self.delay_run(
                    (next_time - self.now).seconds + 1,
                    self.turn_on,
                    mode=self._mode,
                    nocancel=True,
                    brightness=self._brightness,
                )
            )
            self._addSched(ret)
            # await ret

    # Helper function to be used to create a task and run a coroutine in the future
    async def delay_run(self, seconds, coro, *args, **kwargs):
        if self._debug:
            self._logger.error(
                f"delay_run: s:{seconds}, c:{coro}, a:{args}, k:{kwargs}"
            )
        await asyncio.sleep(seconds)
        # await self._hass.loop.sleep(seconds)
        await coro(*args, **kwargs)

    # async def _turn_on_specific(self, data) -> None:
    #    """Disables RightLight functionality and sets light to values in 'data' variable"""
    #    if self._debug:
    #        self._logger.error(f"_turn_on_specific: {data}")
    #    await self._hass.services.async_call("light", "turn_on", data)

    async def turn_on_specific(self, data) -> None:
        """External version of _turn_on_specific that runs twice to ensure successful transition"""
        if self._debug:
            self._logger.error(f"turn_on_specific: {data}")
        await self.disable()

        if not "transition" in data:
            data["transition"] = self.on_transition
        if not "brightness" in data:
            data["brightness"] = 255

        # await self._turn_on_specific(data)
        await self._hass.services.async_call("light", "turn_on", data)

        # Removing second call - if things break, this may be why
        # self._hass.loop.call_later(
        #    0.6, self._hass.loop.create_task, self._turn_on_specific(data)
        # )

    async def disable_and_turn_off(self, **kwargs):
        # Cancel any pending eventloop schedules
        if self._debug:
            self._logger.error(f"turn_off")
        self._cancelSched()

        self._brightness = 0

        await self._hass.services.async_call(
            "light",
            "turn_off",
            {
                "entity_id": self._entity,
                "transition": kwargs.get("transition", self.off_transition),
            },
        )

    async def disable(self):
        # Cancel any pending eventloop schedules
        self._cancelSched()

    def _cancelSched(self):
        if self._debug:
            self._logger.error(f"_cancelSched: {len(self._currSched)}")
        while self._currSched:
            ret = self._currSched.pop(0)
            ret.cancel()
        # for ret in self._currSched:
        #    ret.cancel()
        if self._debug:
            self._logger.error(f"_cancelSched End: {len(self._currSched)}")

    def _addSched(self, ret):
        # FIFO of event callbacks to ensure all are properly cancelled
        # if len(self._currSched) >= 3:
        #    self._currSched.pop(0)
        if self._debug:
            self._logger.error(f"_addSched: {len(self._currSched)}")
        self._currSched.append(ret)
        if self._debug:
            self._logger.error(f"_addSched End: {len(self._currSched)}")

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
            self.ten_thirty = self.now.replace(
                microsecond=0, second=0, minute=30, hour=22
            )
            self.midnight_late = self.now.replace(
                microsecond=0, second=59, minute=59, hour=23
            )

            self.defineTripPoints()

    def defineTripPoints(self):
        self.trip_points["Normal"] = []
        timestep = timedelta(minutes=2)

        # In debug mode, add in drastic changes every two minutes to increase observability
        if self._debug == 2:
            debug_trip_points = [[2500, 120], [4000, 255]]
            self.trip_points["Normal"] = self.enumerateTripPoints(
                timestep / 8, debug_trip_points
            )
        else:
            self.trip_points["Normal"].append(
                [self.midnight_early, [2500, 150]]
            )  # Midnight morning
            self.trip_points["Normal"].append(
                [self.sunrise - timedelta(minutes=60), [2500, 120]]
            )  # Sunrise - 60
            self.trip_points["Normal"].append(
                [self.sunrise - timedelta(minutes=30), [2700, 170]]
            )  # Sunrise - 30
            self.trip_points["Normal"].append([self.sunrise, [3200, 155]])  # Sunrise
            self.trip_points["Normal"].append(
                [self.sunrise + timedelta(minutes=30), [4700, 255]]
            )  # Sunrise + 30
            self.trip_points["Normal"].append(
                [self.sunset - timedelta(minutes=90), [4200, 255]]
            )  # Sunset - 90
            self.trip_points["Normal"].append(
                [self.sunset - timedelta(minutes=30), [3200, 255]]
            )  # Sunset = 30
            self.trip_points["Normal"].append([self.sunset, [3000, 255]])  # Sunset
            self.trip_points["Normal"].append([self.ten_thirty, [2700, 255]])  # 10:30
            self.trip_points["Normal"].append(
                [self.midnight_late, [2500, 150]]
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

        calm_trip_points = [
            [255, 0, 0],
            [202, 0, 127],
            [130, 0, 255],
            [0, 0, 255],
            [0, 90, 190],
            [0, 200, 200],
            [0, 255, 0],
            [255, 127, 0],
        ]

        one_trip_points = [[0, 104, 255], [255, 0, 255]]

        two_trip_points = [[255, 0, 255], [0, 104, 255]]

        # Loop to create vivid trip points
        self.trip_points["Vivid"] = self.enumerateTripPoints(
            timestep, vivid_trip_points
        )

        # Faster timestep for Fun1 mode
        self.trip_points["Fun1"] = self.enumerateTripPoints(
            timestep / 8, vivid_trip_points
        )

        # Faster timestep for Fun2 mode, time shifted from Fun1
        self.trip_points["Fun2"] = self.enumerateTripPoints(
            timestep / 8, vivid_trip_points[1:] + [vivid_trip_points[0]]
        )

        # Loop to create bright trip points
        self.trip_points["Bright"] = self.enumerateTripPoints(
            timestep, bright_trip_points
        )

        # Loop to create calm trip points
        self.trip_points["Calm"] = self.enumerateTripPoints(timestep, calm_trip_points)

        # Loop to create 'one' trip points
        self.trip_points["One"] = self.enumerateTripPoints(timestep, one_trip_points)

        # Loop to create 'two' trip points
        self.trip_points["Two"] = self.enumerateTripPoints(timestep, two_trip_points)

    def getColorModes(self):
        return list(self.trip_points.keys())

    def enumerateTripPoints(self, time_step, trip_points):
        temp = self.midnight_early
        this_ptr = 0
        toreturn = []
        while temp < self.midnight_late:
            toreturn.append([temp, trip_points[this_ptr]])

            temp = temp + time_step

            this_ptr += 1
            if this_ptr >= len(trip_points):
                this_ptr = 0

        return toreturn

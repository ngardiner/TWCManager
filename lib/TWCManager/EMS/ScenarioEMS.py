"""
ScenarioEMS - Test-only EMS module for scenario-based integration testing.

This module is disabled by default. It is only active when:
  1. config sets sources.ScenarioEMS.enabled = true
  2. The TWCM_SCENARIO_FILE environment variable points to a scenario JSON file

Step advancement is controlled externally by writing an integer step index to
TWCM_SCENARIO_STEP_FILE (default: /tmp/twcm_scenario_step). The test runner
writes the desired step index; this module reads it on each poll and applies
the corresponding EMS values.
"""

import json
import logging
import os
from pathlib import Path

from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("ScenarioEMS", "EMS")

STEP_FILE_DEFAULT = "/tmp/twcm_scenario_step"


class ScenarioEMS:
    consumptionWatts = 0
    generationWatts = 0

    _scenario = None
    _timeline = []
    _current_step = -1

    def __init__(self, master):
        self.master = master
        classname = self.__class__.__name__

        # EMS modules live under config["sources"] per translateModuleNameToConfig
        self._config = master.config.get("sources", {}).get(classname, {})

        # Only load when explicitly enabled — this is a test-only module
        if not self._config.get("enabled", False):
            master.releaseModule("lib.TWCManager.EMS", classname)
            return

        scenario_path = os.environ.get("TWCM_SCENARIO_FILE", "")
        if not scenario_path or not Path(scenario_path).exists():
            logger.warning(
                "ScenarioEMS enabled but TWCM_SCENARIO_FILE not set or not found "
                "(%s) — unloading",
                scenario_path,
            )
            master.releaseModule("lib.TWCManager.EMS", classname)
            return

        self._step_file = self._config.get("stepFile", STEP_FILE_DEFAULT)

        self._load_scenario(scenario_path)

    def _load_scenario(self, path):
        try:
            with open(path) as f:
                self._scenario = json.load(f)
        except Exception as e:
            logger.error("ScenarioEMS: failed to load scenario from %s: %s", path, e)
            return

        self._timeline = self._scenario.get("timeline", [])

        # Seed initial EMS values from initial_conditions if present
        initial_ems = self._scenario.get("initial_conditions", {}).get("ems", {})
        self.generationWatts = initial_ems.get("generation_watts", 0)
        self.consumptionWatts = initial_ems.get("consumption_watts", 0)

        logger.log(
            logging.INFO,
            "ScenarioEMS: loaded '%s' with %d timeline steps",
            self._scenario.get("metadata", {}).get("id", path),
            len(self._timeline),
        )

    def _read_current_step(self):
        """Read the step index from the signal file written by the test runner."""
        try:
            raw = Path(self._step_file).read_text().strip()
            return int(raw)
        except (FileNotFoundError, ValueError):
            return -1

    def _apply_step(self, step_index):
        """Apply EMS values for the given step index."""
        if step_index < 0 or step_index >= len(self._timeline):
            return

        step = self._timeline[step_index]
        ems = step.get("ems", {})

        self.generationWatts = ems.get("generation_watts", self.generationWatts)
        self.consumptionWatts = ems.get("consumption_watts", self.consumptionWatts)

        logger.log(
            logging.INFO,
            "ScenarioEMS: step %d ('%s') — generation=%.0fW consumption=%.0fW",
            step_index,
            step.get("label", ""),
            self.generationWatts,
            self.consumptionWatts,
        )

    def update(self):
        """Called each poll cycle. Advances step if the signal file has changed."""
        if self._scenario is None:
            return

        step = self._read_current_step()
        if step != self._current_step:
            self._current_step = step
            self._apply_step(step)

    def getConsumption(self):
        self.update()
        return self.consumptionWatts

    def getGeneration(self):
        # update() already ran in getConsumption() this cycle
        return self.generationWatts

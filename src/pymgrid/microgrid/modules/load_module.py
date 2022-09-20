from src.pymgrid.microgrid.modules.base import BaseTimeSeriesMicrogridModule
import numpy as np


class LoadModule(BaseTimeSeriesMicrogridModule):
    module_type = ('load', 'fixed')

    def __init__(self, time_series, loss_load_cost, raise_errors=False):
        super().__init__(time_series,
                         raise_errors=raise_errors,
                         provided_energy_name=None,
                         absorbed_energy_name='load_met')

        self.loss_load_cost = loss_load_cost
        self.name = ('load', None)

    def update(self, external_energy_change, as_source=False, as_sink=False):
        assert as_sink, f'Class {self.__class__.__name__} is a sink.'

        loss_load, loss_load_cost = self._get_loss_load(external_energy_change)

        try:
            done = False
            next_load = -1.0 * self._time_series[self.current_step + 1]
        except IndexError:
            done = True
            next_load = np.nan

        info = {'absorbed_energy': external_energy_change, 'loss_load': loss_load}

        return next_load, loss_load_cost, done, info

    def _get_loss_load(self, load_met):
        loss_load = self.current_load - load_met
        loss_load_cost = -1.0 * loss_load * self.loss_load_cost
        assert loss_load >= 0
        return loss_load, loss_load_cost

    @property
    def state_components(self):
        return np.array("load")

    @property
    def max_consumption(self):
        return self.current_load

    @property
    def current_load(self):
        return self._time_series[self._current_step]

    @property
    def is_sink(self):
        return True

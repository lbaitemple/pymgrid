import sys
import time
from copy import deepcopy

import cvxpy as cp
import numpy as np
import pandas as pd
from pymgrid.algos.Control import ControlOutput, HorizonOutput
from pymgrid.utils.DataGenerator import return_underlying_data
from scipy.sparse import csr_matrix


class NonModularModelPredictiveControl:

    """
    A class to run Model Predictive Control using the model outlined in the pymgrid paper

    Parameters:
        microgrid: Microgrid.Microgrid
            The underlying microgrid on which MPC will be run

    Attributes:
    --------------
    microgrid: Microgrid.Microgrid
        The underlying microgrid

    horizon: int
        The forecast horizon being used in MPC

    has_genset: bool
        Whether the microgrid has a genset or not

    p_vars: cvxpy.Variable, shape ((7+self.has_genset)*horizon,)
        Vector of all of the controls, at all timesteps. See P in pymgrid paper for details.

    u_genset: None or cvxpy.Variable, shape (self.horizon,)
        Boolean vector variable denoting the status of the genset (on or off) at each timestep if
        the genset exists. If not genset, u_genset = None.

    costs: cvxpy.Parameter, shape ((7+self.has_genset)*self.horizon,)
        Parameter vector of all of the respective costs, at all timesteps. See C in pymgrid paper for details.

    equality_rhs: cvxpy.Parameter, shape (2 * self.horizon,)
        Parameter vector contraining the RHS of the equality constraint equation. See b in pymgrid paper for details.

    inequality_rhs: cvxpy.Parameter, shape (8 * self.horizon,)
        Parameter vector contraining the RHS of the inequality constraint equation. See d in pymgrid paper for details.

    problem: cvxpy.problems.problem.Problem
        The constraint optimization problem to solve


    """
    def __init__(self, microgrid):
        self.microgrid = microgrid
        self.horizon = microgrid.horizon
        if self.microgrid.architecture['genset']==1:
            self.has_genset = True
        else:
            self.has_genset = False

        if self.has_genset:
            self.p_vars = cp.Variable((8*self.horizon,), pos=True)
            self.u_genset = cp.Variable((self.horizon,), boolean=True)
            self.costs = cp.Parameter(8 * self.horizon)
            self.inequality_rhs = cp.Parameter(9 * self.horizon)

        else:
            self.p_vars = cp.Variable((7*self.horizon,), pos=True)
            self.u_genset = None
            self.costs = cp.Parameter(7 * self.horizon, nonneg=True)
            self.inequality_rhs = cp.Parameter(8 * self.horizon)

        self.equality_rhs = cp.Parameter(2 * self.horizon)  # rhs

        parameters = self._parse_microgrid()

        self.problem = self._create_problem(*parameters)

    def _parse_microgrid(self):
        """
        Protected helper function.
        Parses the microgrid in self.microgrid to extract the parameters necessary to run MPC.
        :return:
            eta: float
                battery efficiency
            battery_capacity: float
                battery capacity for normalization
            fuel_cost: float
                fuel cost for the genset
            cost_battery_cycle: float
                cost of cycling the battery
            cost_loss_load: float
                cost of loss load
            p_genset_min: float
                minimum production of the genset
            p_genset_max: float
                maximum production of the genset
        """

        parameters = self.microgrid.parameters

        eta = parameters['battery_efficiency'].values[0]
        battery_capacity = parameters['battery_capacity'].values[0]

        if self.microgrid.architecture['genset'] == 1:
            fuel_cost = parameters['fuel_cost'].values[0]
        else:
            fuel_cost = 0

        cost_battery_cycle = parameters['battery_cost_cycle'].values[0]
        cost_loss_load = parameters['cost_loss_load'].values[0]
        cost_co2 = parameters['cost_co2'].values[0]

        if self.has_genset:
            p_genset_min = parameters['genset_pmin'].values[0] * parameters['genset_rated_power'].values[0]
            p_genset_max = parameters['genset_pmax'].values[0] * parameters['genset_rated_power'].values[0]
            genset_co2 = parameters['genset_co2'].values[0]

        else:
            p_genset_min = 0
            p_genset_max = 0
            genset_co2 = 0

        return eta, battery_capacity, fuel_cost, cost_battery_cycle, cost_loss_load, p_genset_min, p_genset_max, cost_co2, genset_co2

    def _create_problem(self, eta, battery_capacity, fuel_cost, cost_battery_cycle, cost_loss_load,
                        p_genset_min, p_genset_max, cost_co2, genset_co2):

        """
        Protected, automatically called on initialization.

        Defines the constrainted optimization problem to be stored in self.problem.
        The parameters defined here do not change between timesteps.

        :param eta: float
            battery efficiency
        :param battery_capacity: float
            battery capacity for normalization
        :param fuel_cost: float
            fuel cost for the genset
        :param cost_battery_cycle: float
            cost of cycling the battery
        :param cost_loss_load: float
            cost of loss load
        :param p_genset_min: float
            minimum production of the genset
        :param p_genset_max: float
            maximum production of the genset
        :return :
            problem: cvxpy.problems.problem.Problem
                The constrainted optimization problem to be solved at each step of the MPC.
        """

        delta_t = 1

        # Define matrix Y
        if self.has_genset:
            Y = np.zeros((self.horizon, self.horizon * 8))

            Y[0, 3] = -1.0 * eta * delta_t/battery_capacity
            Y[0, 4] = delta_t / (eta * battery_capacity)
            Y[0, 7] = 1

            gamma = np.zeros(16)
            gamma[7] = -1
            gamma[11] = -1.0 * eta * delta_t/battery_capacity
            gamma[12] = delta_t / (eta * battery_capacity)
            gamma[15] = 1

            for j in range(1, self.horizon):
                start = (j - 1) * 8

                Y[j, start:start + 16] = gamma
        else:
            Y = np.zeros((self.horizon, self.horizon * 7))
            Y[0, 2] = -1.0 * eta * delta_t / battery_capacity
            Y[0, 3] = delta_t / (eta * battery_capacity)
            Y[0, 6] = 1

            gamma = np.zeros(14)
            gamma[6] = -1
            gamma[9] = -1.0 * eta * delta_t/battery_capacity
            gamma[10] = delta_t / (eta * battery_capacity)
            gamma[13] = 1

            for j in range(1, self.horizon):
                start = (j - 1) * 7

                Y[j, start:start + 14] = gamma

        # done with Y
        if self.has_genset:
            X = np.zeros((self.horizon, self.horizon * 8))

            alpha = np.ones(8)
            alpha[2] = -1
            alpha[3] = -1
            alpha[5] = -1
            alpha[7] = 0

            for j in range(self.horizon):
                start = j * 8
                X[j, start:start + 8] = alpha

        else:

            X = np.zeros((self.horizon, self.horizon * 7))

            alpha = np.ones(7)
            alpha[1] = -1
            alpha[2] = -1
            alpha[4] = -1
            alpha[6] = 0

            for j in range(self.horizon):
                start = j * 7
                X[j, start:start + 7] = alpha

        A = np.concatenate((X, Y))  # lhs
        A = csr_matrix(A)

        # Define inequality constraints

        # Inequality lhs
        # This is for one timestep

        C_block = np.zeros((9, 8))
        C_block[0, 0] = 1
        C_block[1, 7] = 1
        C_block[2, 7] = -1
        C_block[3, 3] = 1
        C_block[4, 4] = 1
        C_block[5, 1] = 1
        C_block[6, 2] = 1
        C_block[7, 5] = 1
        C_block[8, 6] = 1

        if not self.has_genset:             # drop the first column if no genset
            C_block = C_block[1:, 1:]

        # For all timesteps
        block_lists = [[C_block if i == j else np.zeros(C_block.shape) for i in range(self.horizon)] for j in
                       range(self.horizon)]
        C = np.block(block_lists)
        C = csr_matrix(C)

        # Inequality rhs

        constraints = [A @ self.p_vars == self.equality_rhs, C @ self.p_vars <= self.inequality_rhs]

        if self.has_genset:
            constraints.extend((p_genset_min * self.u_genset <= self.p_vars[:: 8],
                                self.p_vars[:: 8] <= p_genset_max * self.u_genset))

        # Define  objective
        if self.has_genset:
            cost_vector = np.array([fuel_cost + cost_co2 * genset_co2, 0, 0,
                                cost_battery_cycle, cost_battery_cycle, 0, cost_loss_load, 0])
        else:
            cost_vector = np.array([0, 0,
                                    cost_battery_cycle, cost_battery_cycle, 0, cost_loss_load, 0])

        costs_vector = np.concatenate([cost_vector] * self.horizon)

        self.costs.value = costs_vector

        objective = cp.Minimize(self.costs @ self.p_vars)

        return cp.Problem(objective, constraints)

    def _set_parameters(self, load_vector, pv_vector, grid_vector, import_price, export_price,
                        e_max, e_min, p_max_charge, p_max_discharge,
                        p_max_import, p_max_export, soc_0, p_genset_max, cost_co2, grid_co2, genset_co2,):

        """
        Protected, called by set_and_solve.
        Sets the time-varying (and some static) parameters in the optimization problem at any given timestep.

        :param load_vector: np.ndarray, shape (self.horizon,)
            load values over the horizon
        :param pv_vector: np.ndarray, shape (self.horizon,)
            pv values over the horizon
        :param grid_vector: np.ndarray, shape (self.horizon,)
            grid values (boolean) over the horizon
        :param import_price: np.ndarray, shape (self.horizon,)
            import prices over the horizon
        :param export_price: np.ndarray, shape (self.horizon,)
            export prices over the horizon
        :param e_max: float
            maximum state of charge of the battery
        :param e_min: float
            minimum state of charge of the battery
        :param p_max_charge: float
            maximum amount of power the battery can charge in one timestep
        :param p_max_discharge: float
            maximum amount of power the battery can discharge in one timestep
        :param p_max_import: float
            maximum amount of power that can be imported in one timestep
        :param p_max_export: float
            maximum amount of power that can be exported in one timestep
        :param soc_0: float
            state of charge of the battery at the timestep just preceding the current horizon
        :return:
            None
        """

        if not isinstance(load_vector,np.ndarray):
            raise TypeError('load_vector must be np.ndarray')
        if not isinstance(pv_vector,np.ndarray):
            raise TypeError('pv_vector must be np.ndarray')
        if not isinstance(grid_vector,np.ndarray):
            raise TypeError('grid_vector must be np.ndarray')
        if not isinstance(import_price,np.ndarray):
            raise TypeError('import_price must be np.ndarray')
        if not isinstance(export_price,np.ndarray):
            raise TypeError('export_price must be np.ndarray')

        if len(load_vector.shape) != 1 and load_vector.shape[0]!=self.horizon:
            raise ValueError('Invalid load_vector, must be of shape ({},)'.format(self.horizon))
        if len(pv_vector.shape) != 1 and pv_vector.shape[0]!=self.horizon:
            raise ValueError('Invalid pv_vector, must be of shape ({},)'.format(self.horizon))
        if len(grid_vector.shape) != 1 and grid_vector.shape[0]!=self.horizon:
            raise ValueError('Invalid grid_vector, must be of shape ({},)'.format(self.horizon))
        if len(import_price.shape) != 1 and import_price.shape[0]!=self.horizon:
            raise ValueError('Invalid import_price, must be of shape ({},)'.format(self.horizon))
        if len(export_price.shape) != 1 and export_price.shape[0]!=self.horizon:
            raise ValueError('Invalid export_price, must be of shape ({},)'.format(self.horizon))

        # Set equality rhs
        equality_rhs_vals = np.zeros(self.equality_rhs.shape)
        equality_rhs_vals[:self.horizon] = load_vector-pv_vector
        equality_rhs_vals[self.horizon] = soc_0
        self.equality_rhs.value = equality_rhs_vals

        # Set inequality rhs
        if self.has_genset:
            inequality_rhs_block = np.array([p_genset_max, e_max, -e_min, p_max_charge, p_max_discharge,
                                             np.nan, np.nan, np.nan, np.nan])
        else:
            inequality_rhs_block = np.array([e_max, -e_min, p_max_charge, p_max_discharge,
                                         np.nan, np.nan, np.nan, np.nan])

        inequality_rhs_vals = np.concatenate([inequality_rhs_block]*self.horizon)

        # set d7-d10
        if self.has_genset:
            inequality_rhs_vals[5::9] = p_max_import * grid_vector
            inequality_rhs_vals[6::9] = p_max_export * grid_vector
            inequality_rhs_vals[7::9] = pv_vector
            inequality_rhs_vals[8::9] = load_vector

        else:
            inequality_rhs_vals[4::8] = p_max_import * grid_vector
            inequality_rhs_vals[5::8] = p_max_export * grid_vector
            inequality_rhs_vals[6::8] = pv_vector
            inequality_rhs_vals[7::8] = load_vector

        if np.isnan(inequality_rhs_vals).any():
            raise RuntimeError('There are still nan values in inequality_rhs_vals, something is wrong')

        self.inequality_rhs.value = inequality_rhs_vals

        # Set costs
        if self.has_genset:
            self.costs.value[1::8] = import_price.reshape(-1) + grid_co2.reshape(-1) * cost_co2
            self.costs.value[2::8] = export_price.reshape(-1)
        else:
            self.costs.value[0::7] = import_price.reshape(-1) + grid_co2.reshape(-1) * cost_co2
            self.costs.value[1::7] = export_price.reshape(-1)

        if np.isnan(self.costs.value).any():
            raise RuntimeError('There are still nan values in self.costs.value, something is wrong')

    def set_and_solve(self, load_vector, pv_vector, grid_vector, import_price, export_price, e_max, e_min, p_max_charge,
                      p_max_discharge, p_max_import, p_max_export, soc_0, p_genset_max, cost_co2, grid_co2, genset_co2, iteration=None, total_iterations=None, return_steps=0):
        """
        Sets the parameters in the problem and then solves the problem.
            Specifically, sets the right-hand sides b and d from the paper of the
            equality and inequality equations, respectively, and the costs vector by calling _set_parameters, then
            solves the problem and returns a control dictionary


        :param load_vector: np.ndarray, shape (self.horizon,)
            load values over the horizon
        :param pv_vector: np.ndarray, shape (self.horizon,)
            pv values over the horizon
        :param grid_vector: np.ndarray, shape (self.horizon,)
            grid values (boolean) over the horizon
        :param import_price: np.ndarray, shape (self.horizon,)
            import prices over the horizon
        :param export_price: np.ndarray, shape (self.horizon,)
            export prices over the horizon
        :param e_max: float
            maximum state of charge of the battery
        :param e_min: float
            minimum state of charge of the battery
        :param p_max_charge: float
            maximum amount of power the battery can charge in one timestep
        :param p_max_discharge: float
            maximum amount of power the battery can discharge in one timestep
        :param p_max_import: float
            maximum amount of power that can be imported in one timestep
        :param p_max_export: float
            maximum amount of power that can be exported in one timestep
        :param soc_0: float
            state of charge of the battery at the timestep just preceding the current horizon
        :param p_genset_max: float
            maximum amount of production of the genset
        :param iteration: int
            Current iteration, used for verbosity
        :param total_iterations:
            Total iterations, used for verbosity
        :return:
            control_dict, dict
            dictionary of the controls of the first timestep, as MPC does.
        """

        self._set_parameters(load_vector, pv_vector, grid_vector, import_price, export_price,
                        e_max, e_min, p_max_charge, p_max_discharge,
                        p_max_import, p_max_export, soc_0, p_genset_max, cost_co2, grid_co2, genset_co2,)

        self.problem.solve(warm_start=True)

        if self.problem.status == 'infeasible':
            print(self.problem.status)
            print('Infeasible problem on step {} of {}, retrying with GLPK_MI solver'.format(iteration,total_iterations))
            self.problem.solve(solver = cp.CVXOPT)
            if self.problem.status == 'infeasible':
                print('Failed again')
            else:
                print('Optimizer found with GLPK_MI solver')

        if return_steps == 0:
            if self.has_genset:
                control_dict = {'battery_charge': self.p_vars.value[3],
                                'battery_discharge': self.p_vars.value[4],
                                'genset': self.p_vars.value[0],
                                'grid_import': self.p_vars.value[1],
                                'grid_export': self.p_vars.value[2],
                                'loss_load': self.p_vars.value[6],
                                'pv_consummed': pv_vector[0] - self.p_vars.value[5],
                                'pv_curtailed': self.p_vars.value[5],
                                'load': load_vector[0],
                                'pv': pv_vector[0]}
            else:
                control_dict = {'battery_charge': self.p_vars.value[2],
                                'battery_discharge': self.p_vars.value[3],
                                'grid_import': self.p_vars.value[0],
                                'grid_export': self.p_vars.value[1],
                                'loss_load': self.p_vars.value[5],
                                'pv_consummed': pv_vector[0] - self.p_vars.value[4],
                                'pv_curtailed': self.p_vars.value[4],
                                'load': load_vector[0],
                                'pv': pv_vector[0]}

            return control_dict

        else:
            if return_steps > self.microgrid.horizon:
                raise ValueError('return_steps cannot be greater than horizon')

            control_dicts = []

            if self.has_genset:
                for j in range(return_steps):
                    start_index = j*8

                    control_dict = {'battery_charge': self.p_vars.value[start_index+3],
                                    'battery_discharge': self.p_vars.value[start_index+4],
                                    'genset': self.p_vars.value[start_index],
                                    'grid_import': self.p_vars.value[start_index+1],
                                    'grid_export': self.p_vars.value[start_index+2],
                                    'loss_load': self.p_vars.value[start_index+6],
                                    'pv_consummed': pv_vector[j] - self.p_vars.value[start_index+5],
                                    'pv_curtailed': self.p_vars.value[start_index+5],
                                    'load': load_vector[j],
                                    'pv': pv_vector[j]}

                    control_dicts.append(control_dict)

            else:
                for j in range(return_steps):
                    start_index = j * 7

                    control_dict = {'battery_charge': self.p_vars.value[start_index + 2],
                                    'battery_discharge': self.p_vars.value[start_index + 3],
                                    'grid_import': self.p_vars.value[start_index],
                                    'grid_export': self.p_vars.value[start_index + 1],
                                    'loss_load': self.p_vars.value[start_index + 5],
                                    'pv_consummed': pv_vector[j] - self.p_vars.value[start_index + 4],
                                    'pv_curtailed': self.p_vars.value[start_index + 4],
                                    'load': load_vector[j],
                                    'pv': pv_vector[j]}

                    control_dicts.append(control_dict)

            return control_dicts

    def run_mpc_on_sample(self, sample, forecast_steps=None, verbose=False):
        """
        Runs MPC on a sample over a number of iterations

        :param sample: pd.DataFrame, shape (8760,3)
            sample to run the MPC on. Must contain columns 'load', 'pv', and 'grid'.
        :param forecast_steps: int, default None
            Number of steps to run MPC on. If None, runs over 8760-self.horizon steps
        :param verbose: bool
            Whether to discuss progress
        :return:
            output, ControlOutput
                dict-like containing the DataFrames ('action', 'status', 'production', 'cost'),
                but with an ordering defined via comparing the costs.
        """
        if not isinstance(sample, pd.DataFrame):
            raise TypeError('sample must be of type pd.DataFrame, is {}'.format(type(sample)))

        sample = sample.iloc[:self.microgrid._data_length]

        # dataframes, copied API from _baseline_linprog
        self.microgrid.reset()
        baseline_linprog_action = deepcopy(self.microgrid._df_record_control_dict)
        baseline_linprog_update_status = deepcopy(self.microgrid._df_record_state)
        baseline_linprog_record_production = deepcopy(self.microgrid._df_record_actual_production)
        baseline_linprog_cost = deepcopy(self.microgrid._df_record_cost)
        baseline_linprog_co2 = deepcopy(self.microgrid._df_record_co2)

        T = len(sample)
        horizon = self.microgrid.horizon

        if forecast_steps is None:
            num_iter = T - horizon
        else:
            assert forecast_steps <= T - horizon, 'forecast steps can\'t look past horizon'
            num_iter = forecast_steps

        t0 = time.time()
        old_control_dict = None

        for i in range(num_iter):

            if verbose and i % 100 == 0:
                ratio = i / num_iter
                sys.stdout.write("\r Progress of current MPC: %d%%\n" % (100 * ratio))
                sys.stdout.flush()

            if self.microgrid.architecture['grid'] == 0:
                temp_grid = np.zeros(horizon)
                price_import = np.zeros(horizon)
                price_export = np.zeros(horizon)
                p_max_import = 0
                p_max_export = 0
                grid_co2 = np.zeros(horizon)
            else:
                temp_grid = sample.loc[i:i + horizon - 1, 'grid'].values
                price_import = self.microgrid._grid_price_import.iloc[i:i + horizon].values
                price_export = self.microgrid._grid_price_export.iloc[i:i + horizon].values
                grid_co2 = self.microgrid._grid_co2.iloc[i:i + horizon].values
                p_max_import = self.microgrid.parameters['grid_power_import'].values[0]
                p_max_export = self.microgrid.parameters['grid_power_export'].values[0]

                if temp_grid.shape != price_export.shape and price_export.shape != price_import.shape:
                    raise RuntimeError('I think this is a problem')


            e_min = self.microgrid.parameters['battery_soc_min'].values[0]
            e_max = self.microgrid.parameters['battery_soc_max'].values[0]
            p_max_charge = self.microgrid.parameters['battery_power_charge'].values[0]
            p_max_discharge = self.microgrid.parameters['battery_power_discharge'].values[0]

            soc_0 = baseline_linprog_update_status['battery_soc'][-1]

            cost_co2 = self.microgrid.parameters['cost_co2'].values[0]

            if self.has_genset:
                p_genset_max = self.microgrid.parameters['genset_pmax'].values[0] *\
                           self.microgrid.parameters['genset_rated_power'].values[0]
                genset_co2 = self.microgrid.parameters['genset_co2'].values[0]
            else:
                p_genset_max = None
                genset_co2 = None

            # Solve one step of MPC
            control_dict = self.set_and_solve(sample.loc[i:i + horizon - 1, 'load'].values,
                                              sample.loc[i:i + horizon - 1, 'pv'].values, temp_grid, price_import,
                                              price_export, e_max, e_min, p_max_charge, p_max_discharge, p_max_import,
                                              p_max_export, soc_0, p_genset_max, cost_co2, grid_co2, genset_co2, iteration = i, total_iterations = num_iter)

            if control_dict is not None:
                baseline_linprog_action = self.microgrid._record_action(control_dict, baseline_linprog_action)
                baseline_linprog_record_production = self.microgrid._record_production(control_dict,
                                                                                       baseline_linprog_record_production,
                                                                                       baseline_linprog_update_status)
                old_control_dict = control_dict.copy()

            elif old_control_dict is not None:
                print('Using previous controls')
                baseline_linprog_action = self.microgrid._record_action(old_control_dict, baseline_linprog_action)
                baseline_linprog_record_production = self.microgrid._record_production(old_control_dict,
                                                                                       baseline_linprog_record_production,
                                                                                       baseline_linprog_update_status)
            else:
                raise RuntimeError('Fell through, was unable to solve for control_dict and could not find previous control dict')

            if self.microgrid.architecture['grid'] == 1:
                baseline_linprog_co2 = self.microgrid._record_co2(
                    {i: baseline_linprog_record_production[i][-1] for i in baseline_linprog_record_production},
                    baseline_linprog_co2,
                    self.microgrid._grid_co2.iloc[i].values[0],
                )

                baseline_linprog_update_status = self.microgrid._update_status(
                    {i: baseline_linprog_record_production[i][-1] for i in baseline_linprog_record_production},
                    baseline_linprog_update_status,
                    sample.at[i + 1, 'load'],
                    sample.at[i + 1, 'pv'],
                    sample.at[i + 1, 'grid'],
                    self.microgrid._grid_price_import.iloc[i + 1].values[0],
                    self.microgrid._grid_price_export.iloc[i + 1].values[0],
                    self.microgrid._grid_co2.iloc[i + 1].values[0],
                )

                baseline_linprog_cost = self.microgrid._record_cost(
                    {i: baseline_linprog_record_production[i][-1] for i in baseline_linprog_record_production},
                    baseline_linprog_cost,
                    baseline_linprog_co2,
                    self.microgrid._grid_price_import.iloc[i, 0],
                    self.microgrid._grid_price_export.iloc[i, 0])
            else:
                baseline_linprog_co2 = self.microgrid._record_co2(
                    {i: baseline_linprog_record_production[i][-1] for i in baseline_linprog_record_production},
                    baseline_linprog_co2,
                )

                baseline_linprog_update_status = self.microgrid._update_status(
                    {i: baseline_linprog_record_production[i][-1] for i in baseline_linprog_record_production},
                    baseline_linprog_update_status,
                    sample.at[i + 1, 'load'],
                    sample.at[i + 1, 'pv']
                )
                baseline_linprog_cost = self.microgrid._record_cost(
                    {i: baseline_linprog_record_production[i][-1] for i in baseline_linprog_record_production},
                    baseline_linprog_cost,
                    baseline_linprog_co2,
                )

        names = ('action', 'status', 'production', 'cost', 'co2')

        dfs = (baseline_linprog_action, baseline_linprog_update_status,
               baseline_linprog_record_production, baseline_linprog_cost, baseline_linprog_co2)

        if verbose:
            print('Total time: {} minutes'.format(round((time.time()-t0)/60, 2)))

        return ControlOutput(names, dfs, 'mpc')

    def run_mpc_on_microgrid(self, forecast_steps=None, verbose=False, **kwargs):
        """
        Function that allows MPC to be run on self.microgrid by first parsing its data

        :param forecast_steps: int, default None
            Number of steps to run MPC on. If None, runs over 8760-self.horizon steps
        :param verbose: bool
            Whether to discuss progress
        :return:
            output, ControlOutput
                dict-like containing the DataFrames ('action', 'status', 'production', 'cost'),
                but with an ordering defined via comparing the costs.
        """

        sample = return_underlying_data(self.microgrid)
        sample = sample.reset_index(drop=True)
        return self.run_mpc_on_sample(sample, forecast_steps=forecast_steps, verbose=verbose)

    def mpc_single_step(self, sample, previous_output, current_step):

        if not isinstance(previous_output, ControlOutput):
            raise TypeError('previous_output must be ControlOutput, unless first_step is True')

        # baseline_linprog_update_status = pd.DataFrame(previous_output['status'].iloc[-1].squeeze()).transpose()

        horizon = self.microgrid.horizon

        if self.microgrid.architecture['grid'] == 0:
            temp_grid = np.zeros(horizon)
            price_import = np.zeros(horizon)
            price_export = np.zeros(horizon)
            grid_co2 = np.zeros(horizon)
            p_max_import = 0
            p_max_export = 0
        else:
            temp_grid = sample.loc[current_step:current_step + horizon - 1, 'grid'].values
            price_import = self.microgrid._grid_price_import.iloc[current_step:current_step + horizon].values
            price_export = self.microgrid._grid_price_export.iloc[current_step:current_step + horizon].values
            grid_co2 = self.microgrid._grid_co2.iloc[current_step:current_step + horizon].values
            p_max_import = self.microgrid.parameters['grid_power_import'].values[0]
            p_max_export = self.microgrid.parameters['grid_power_export'].values[0]

            if temp_grid.shape != price_export.shape and price_export.shape != price_import.shape:
                raise RuntimeError('I think this is a problem')

        e_min = self.microgrid.parameters['battery_soc_min'].values[0]
        e_max = self.microgrid.parameters['battery_soc_max'].values[0]
        p_max_charge = self.microgrid.parameters['battery_power_charge'].values[0]
        p_max_discharge = self.microgrid.parameters['battery_power_discharge'].values[0]
        soc_0 = previous_output['status']['battery_soc'][-1]

        cost_co2 = self.microgrid.parameters['cost_co2'].values[0]

        if self.has_genset:
            p_genset_max = self.microgrid.parameters['genset_pmax'].values[0] * \
                           self.microgrid.parameters['genset_rated_power'].values[0]
            genset_co2 = self.microgrid.parameters['genset_co2'].values[0]
        else:
            p_genset_max = None
            genset_co2 = 0

        # Solve one step of MPC
        control_dicts = self.set_and_solve(sample.loc[current_step:current_step + horizon - 1, 'load'].values,
                                          sample.loc[current_step:current_step + horizon - 1, 'pv'].values, temp_grid, price_import,
                                          price_export, e_max, e_min, p_max_charge, p_max_discharge, p_max_import,
                                          p_max_export, soc_0, p_genset_max, cost_co2, grid_co2, genset_co2, iteration=current_step, return_steps=self.microgrid.horizon)

        if any([d is None for d in control_dicts]):
            for j, d in enumerate(control_dicts):
                if d is None:
                    raise TypeError('control_dict number {} is None'.format(j))

        return HorizonOutput(control_dicts, self.microgrid, current_step)
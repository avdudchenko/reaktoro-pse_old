import reaktoro as rkt

import numpy as np

from reaktoro_pse.core.reaktoro_state import ReaktoroState
from reaktoro_pse.core.reaktoro_outputs import (
    ReaktoroOutputSpec,
)
from reaktoro_pse.core.reaktoro_inputs import (
    ReaktoroInputSpec,
)
from reaktoro_pse.core.reaktoro_jacobian import (
    ReaktoroJacobianSpec,
)
import cyipopt
import idaes.logger as idaeslog
import time

__author__ = "Alexander Dudchenko, Ben Knueven, Ilayda Akkor"

_log = idaeslog.getLogger(__name__)
"""class to setup reaktor solver for reaktoro"""


class ReaktoroSolver:
    def __init__(
        self,
        reaktoro_state,
        reaktoro_input_specs,
        reaktoro_output_specs,
        reaktoro_jacobian_specs,
        block_name=None,
    ):
        self.blockName = block_name
        self.state = reaktoro_state
        if isinstance(self.state, ReaktoroState) == False:
            raise TypeError("Reator jacobian require rektoroState class")
        self.input_specs = reaktoro_input_specs
        if isinstance(self.input_specs, ReaktoroInputSpec) == False:
            raise TypeError("Reator outputs require ReaktoroOutputSpec class")
        self.output_specs = reaktoro_output_specs
        if isinstance(self.output_specs, ReaktoroOutputSpec) == False:
            raise TypeError("Reator outputs require ReaktoroOutputSpec class")
        self.jacbian_specs = reaktoro_jacobian_specs
        if isinstance(self.jacbian_specs, ReaktoroJacobianSpec) == False:
            raise TypeError("Reator outputs require ReaktoroOutputSpec class")
        self.solver_options = rkt.EquilibriumOptions()
        self.presolve_options = rkt.EquilibriumOptions()

        existing_constraints = self.input_specs.equilibrium_specs.namesConstraints()

        existing_variables = self.input_specs.equilibrium_specs.namesControlVariables()
        _log.debug(f"rktSolver inputs: {existing_variables}")
        _log.debug(f"rktSolver constraints: {existing_constraints}")
        self.solver = rkt.EquilibriumSolver(self.input_specs.equilibrium_specs)
        self.conditions = rkt.EquilibriumConditions(self.input_specs.equilibrium_specs)

        self.sensitivity = rkt.EquilibriumSensitivity(
            self.input_specs.equilibrium_specs
        )
        self.set_solver_options()
        self._sequential_fails = 0
        self._max_fails = 30

    def set_solver_options(
        self,
        epsilon=1e-32,
        tolerance=1e-8,
        presolve=False,
        presolve_tolerance=1e-8,
        presolve_epsilon=1e-12,
        max_iters=500,
        presolve_max_iters=500,
        hessian_type="J.tJ",
    ):
        """configuration for reaktro solver

        Keyword arguments:
        tolerance -- reaktoro solver tolerance (default: 1e-32)
        max_iters -- maxium iterations for reaktoro solver (default: 200)
        presolve -- presolve the model useing "presolve_tolerance" first (default: False)
        presolve_tolerance -- presolve tolerance if enabled (default: 1e-12)
        presolve_max_iters -- maximum iterations for presolve call if enabled (default: 200)
        """
        self.solver_options.epsilon = epsilon
        self.solver_options.optima.maxiters = max_iters

        # self.rktSolverOptions.optima.output.active = True
        self.solver_options.optima.convergence.tolerance = tolerance

        self.presolve_options.epsilon = presolve_epsilon
        self.presolve_options.optima.maxiters = presolve_max_iters

        # self.rktPresolveOptions.optima.output.active = True
        self.presolve_options.optima.convergence.tolerance = presolve_tolerance
        self.presolve = presolve
        self.solver.setOptions(self.solver_options)
        self.hessian_type = hessian_type
        if self.input_specs.assert_charge_neutrality:
            self.conditions.charge(0)
        if "pressure" not in self.state.inputs:
            self.conditions.setLowerBoundPressure(0.0, "bar")
            self.conditions.setLowerBoundPressure(100, "bar")
        # if "GaseousPhase" in [
        #     phase.name() for phase in self.state.state.system().phases()
        # ]:
        #     self.conditions.phaseVolume(f"GaseousPhase", 1e-3)  # , "m3")

    def update_specs(self, params):
        for input_key in self.input_specs.rkt_inputs.rkt_input_list:
            input_obj = self.input_specs.rkt_inputs[input_key]
            if params is None:
                value = input_obj.get_value(update_temp=True)
            else:
                value = params.get(input_key)
                input_obj.currentValue = value
            unit = input_obj.main_unit
            # _log.info(
            #     f"spec-input: {input_obj.get_rkt_input_name()},{input_key},{value},{unit}"
            # )
            if input_key == "temperature":
                self.conditions.temperature(value, unit)
            elif input_key == "pressure":
                self.conditions.pressure(value, unit)
            else:
                # TODO figure out how deal with units...
                self.conditions.set(input_obj.get_rkt_input_name(), value)

    def get_outputs(self):
        output_arr = []
        for key, obj in self.output_specs.rkt_outputs.items():
            output_arr.append(
                self.output_specs.evaluate_property(obj, update_values_in_object=True)
            )
        return output_arr

    def get_jacobian(self):
        self.tempJacobianMatrix = self.sensitivity.dudw()
        self.jacbian_specs.update_jacobian_absolute_values()
        jac_matrix = []
        for input_key in self.input_specs.rkt_inputs.rkt_input_list:
            input_obj = self.input_specs.rkt_inputs[input_key]
            jac_row = self.jacbian_specs.get_jacobian(
                self.tempJacobianMatrix, input_obj
            )
            jac_matrix.append(jac_row)

        return np.array(jac_matrix).T  # needs to transpose

    def solve_reaktoro_block(
        self,
        params=None,
        display=False,
        presolve=False,
    ):
        """here we solve reaktor model and return the jacobian matrix and solution, as
        well as update relevant reaktoroSpecs"""
        ts = time.time()
        self.update_specs(params)

        result = self.try_solve(presolve)
        self.outputs = self.get_outputs()
        self.jacobian_matrix = self.get_jacobian()
        if result.succeeded() == False or display:
            _log.info(
                f"warning, solve was not successful for {self.blockName}, fail# {self._sequential_fails}"
            )
            self._sequential_fails += 1
            if self._sequential_fails > self._max_fails:
                assert False
            raise cyipopt.CyIpoptEvaluationError
        else:
            self._sequential_fails = 0
        return self.jacobian_matrix, self.outputs

    def try_solve(self, presolve=False):
        if self.presolve or presolve:
            """solve to loose tolerance first if selected"""
            self.solver.setOptions(self.presolve_options)
            r = self.solver.solve(
                self.state.state,
                self.sensitivity,
                self.conditions,
            )
            if r.succeeded() == False:
                _log.info(f"presolve {r.succeeded()}")
        self.solver.setOptions(self.solver_options)
        result = self.solver.solve(
            self.state.state,
            self.sensitivity,
            self.conditions,
        )
        _jac_phases = [phase.name() for phase in self.state.state.system().phases()]

        # print(self.state.state.props())
        # if "GaseousPhase" in _jac_phases:
        #     print(self.state.state.props())
        #     #     print(_jac_phases)
        #     print(self.state.state.props().volume())
        #     print(self.state.state.props().phaseProps("GaseousPhase").pressure())
        #     print(self.state.state.props().phaseProps("GaseousPhase").volume())
        self.output_specs.update_supported_props()
        return result

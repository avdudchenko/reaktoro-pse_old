###############################################################################
# #################################################################################
# # WaterTAP Copyright (c) 2020-2024, The Regents of the University of California,
# # through Lawrence Berkeley National Laboratory, Oak Ridge National Laboratory,
# # National Renewable Energy Laboratory, and National Energy Technology
# # Laboratory (subject to receipt of any required approvals from the U.S. Dept.
# # of Energy). All rights reserved.
# #
# # Please see the files COPYRIGHT.md and LICENSE.md for full copyright and license
# # information, respectively. These files are also available online at the URL
# # "https://github.com/watertap-org/reaktoro-pse/"
# #################################################################################
###############################################################################
from reaktoro_pse.reaktoro_block import ReaktoroBlock


from pyomo.environ import (
    ConcreteModel,
    Var,
    Constraint,
    assert_optimal_termination,
    units as pyunits,
)
from watertap.core.solvers import get_solver
from pyomo.util.calc_var_value import calculate_variable_from_constraint

import idaes.core.util.scaling as iscale

"""
This examples demonstrates how reaktoro graybox can be used to enthalpy and water vapor pressure. 

NOTE: For water vapor calculations, pay attention to speciation and assumptions. Please
refere to these two discussions:

https://github.com/reaktoro/reaktoro/discussions/398
https://github.com/reaktoro/reaktoro/discussions/285


Key assumptions:
Assumes that process concentrating the feed does not alter the pH. 
This might be a good assumptions for process such as RO, but might be a poor
assumption for evaporative processes. 
"""


def main():
    m = build_simple_precipitation()
    initialize(m)
    setup_optimization(m)
    solve(m)
    return m


def build_simple_precipitation():
    m = ConcreteModel()
    m.feed_composition = Var(
        ["H2O", "Mg", "Na", "Cl", "SO4", "Ca", "HCO3"],
        initialize=1,
        units=pyunits.mol / pyunits.s,
    )
    m.feed_composition.construct()
    m.feed_composition["H2O"].fix(55)
    m.feed_composition["Mg"].fix(0.01)
    m.feed_composition["Na"].fix(0.025)
    m.feed_composition["Cl"].fix(0.025)
    m.feed_composition["Ca"].fix(0.002)
    m.feed_composition["HCO3"].fix(0.01)
    m.feed_composition["SO4"].fix(0.02)
    m.feed_temperature = Var(initialize=293.15, units=pyunits.K)
    m.feed_temperature.fix()
    m.feed_pressure = Var(initialize=1e5, units=pyunits.Pa)
    m.feed_pressure.fix()
    m.feed_pH = Var(initialize=7, bounds=(4, 12), units=pyunits.dimensionless)
    m.feed_pH.fix()
    m.precipitator_composition = Var(
        list(m.feed_composition.keys()),
        initialize=1,
        units=pyunits.mol / pyunits.s,
    )
    m.sludge_water_content = Var(initialize=0.8)
    m.sludge_water_content.fix()
    m.treated_composition = Var(
        list(m.feed_composition.keys()),
        initialize=1,
        units=pyunits.mol / pyunits.s,
    )

    m.sludge_composition = Var(
        list(m.feed_composition.keys()),
        initialize=1,
        units=pyunits.mol / pyunits.s,
    )
    m.precipitator_temperature = Var(
        initialize=273.15 + 50, bounds=(273.15, 273.15 + 99), units=pyunits.K
    )
    m.precipitator_temperature.fix()
    m.cooled_treated_temperature = Var(
        initialize=273.15 + 12.5, bounds=(273.15, 273.15 + 99), units=pyunits.K
    )
    m.Q_heating = Var(initialize=0, units=pyunits.J / pyunits.s)
    m.Q_recoverable = Var(initialize=0, units=pyunits.J / pyunits.s)
    m.Q_recovery_eff = Var(initialize=0.5, units=pyunits.dimensionless)
    m.Q_recovery_eff.fix()
    """ we only need enthalpy - can also request output pH, and pass it to precipitator 
    but dont need to - assume the temperature is not impacting pH"""
    m.feed_properties = Var(
        [
            ("molarEnthalpy", None),
            ("vaporPressure", "H2O(g)"),
            ("specificHeatCapacityConstP", None),
        ],
        initialize=1,
    )
    m.precipitation_properties = Var(
        [
            ("speciesAmount", "Calcite"),
            ("speciesAmount", "Anhydrite"),
            ("specificHeatCapacityConstP", None),
            ("molarEnthalpy", None),
            ("pH", None),
            ("vaporPressure", "H2O(g)"),
        ],
        initialize=1e-5,
    )
    m.treated_properties = Var(
        [
            ("molarEnthalpy", None),
            ("specificHeatCapacityConstP", None),
        ],
        initialize=1,
    )
    m.cooled_treated_properties = Var(
        [
            ("molarEnthalpy", None),
            ("vaporPressure", "H2O(g)"),
            ("specificHeatCapacityConstP", None),
        ],
        initialize=1,
    )
    reactant_dict = {
        "Ca": (1, "Calcite"),
        "HCO3": (1, "Calcite"),
        "Ca": (1, "Anhydrite"),
        "SO4": (1, "Anhydrite"),
    }
    m.eq_Q_heating = Constraint(
        expr=m.Q_heating
        == (
            m.precipitation_properties[("molarEnthalpy", None)]
            * sum([obj for key, obj in m.precipitator_composition.items()])
            - m.feed_properties[("molarEnthalpy", None)]
            * sum([obj for key, obj in m.feed_composition.items()])
        )
    )
    m.eq_Q_recoverable = Constraint(
        expr=m.Q_recoverable
        == (
            m.treated_properties[("molarEnthalpy", None)]
            * sum([obj for key, obj in m.treated_composition.items()])
            - m.cooled_treated_properties[("molarEnthalpy", None)]
            * sum([obj for key, obj in m.treated_composition.items()])
        )
    )
    m.eq_Q_equlaity = Constraint(expr=m.Q_heating * m.Q_recovery_eff == m.Q_recoverable)

    # connecting feed to precipitator composition
    @m.Constraint(list(m.feed_composition.keys()))
    def eq_precipitator_composition(fs, key):
        if key == "H2O":
            return m.precipitator_composition["H2O"] == m.feed_composition["H2O"]
        else:
            return m.precipitator_composition[key] == m.feed_composition[key]

    @m.Constraint(list(m.feed_composition.keys()))
    def eq_treated_composition(fs, key):
        return (
            m.precipitator_composition[key] - m.sludge_composition[key]
            == m.treated_composition[key]
        )

    # calculate sludge composition - we are not tracking solids that form, but rather
    # apparat species that would make them in addition those present in aqueous phase of the
    # sludge.

    @m.Constraint(list(m.feed_composition.keys()))
    def eq_sludge_composition(fs, key):
        if key == "H2O":
            # water flow is percent of total solids
            solid_mass = [
                m.precipitation_properties[("speciesAmount", "Calcite")],
                m.precipitation_properties[("speciesAmount", "Anhydrite")],
            ]
            return (
                m.precipitator_composition["H2O"]
                * m.sludge_water_content
                * sum(solid_mass)
                == m.sludge_composition["H2O"]
            )
        elif key in reactant_dict:
            return (
                m.precipitation_properties[("speciesAmount", reactant_dict[key][1])]
                * reactant_dict[key][0]
                + m.sludge_composition["H2O"]
                * m.treated_composition[key]
                / m.treated_composition["H2O"]
                == m.sludge_composition[key]
            )
        else:
            return (
                m.sludge_composition["H2O"]
                * m.treated_composition[key]
                / m.treated_composition["H2O"]
                == m.sludge_composition[key]
            )

    """ we have to use super critical database to enthalpy data as 
    our default PhreeqC data base with pitzer data file does not contain 
    enthalpy information - please refer to reaktoro documentation on supported data bases """
    """ we also need to define an ion translation dicionary for this data base 
    as default translator only support PhreeqCdata base with pitzer data file notation 
     - once again refer to reaktoro documentation on specific data base and species to define translation dictionory 
     - this dict should connect the name of species you are supplying to name of species 
     in the data base file"""

    translation_dict = {
        "H2O": "H2O(aq)",
        "Mg": "Mg+2",
        "Na": "Na+",
        "Cl": "Cl-",
        "SO4": "SO4-2",
        "Ca": "Ca+2",
        "HCO3": "HCO3-",
    }
    """ note how we included nitrogen as one of gas species, this will prevent 
        PengRobinson EOS from forcing all of the water into vapor phase (refer to NOTE above)"""
    m.eq_feed_properties = ReaktoroBlock(
        composition=m.feed_composition,
        temperature=m.feed_temperature,
        pressure=m.feed_pressure,
        pH=m.feed_pH,
        outputs=m.feed_properties,
        aqueous_phase_activity_model="ActivityModelPitzer",
        mineral_phases=["Calcite", "Anhydrite"],
        gas_phases=["H2O(g)", "N2(g)"],
        gas_phase_activity_model="ActivityModelPengRobinson",
        database="SupcrtDatabase",  # need to specify new data base to use
        database_file="supcrtbl",  # need to specify specific data base file to use
        species_to_rkt_species_dict=translation_dict,
        convert_to_rkt_species=True,
        dissolve_species_in_reaktoro=False,
        jacobian_user_scaling={
            ("molarEnthalpy", None): 1,
            ("specificHeatCapacityConstP", None): 1,
        },
    )

    # """ need to get precipitator enthalpy to find required power input """
    m.eq_precipitation_properties = ReaktoroBlock(
        composition=m.precipitator_composition,
        temperature=m.precipitator_temperature,
        pressure=m.feed_pressure,  # assume all systems operate at same pressure - not
        pH=m.feed_pH,
        outputs=m.precipitation_properties,
        aqueous_phase_activity_model="ActivityModelPitzer",
        mineral_phases=["Calcite", "Anhydrite"],
        gas_phases=["H2O(g)", "N2(g)"],
        gas_phase_activity_model="ActivityModelPengRobinson",
        database="SupcrtDatabase",  # need to specify new data base to use
        database_file="supcrtbl",  # need to specify specific data base file to use
        species_to_rkt_species_dict=translation_dict,
        convert_to_rkt_species=True,
        dissolve_species_in_reaktoro=False,
        build_speciation_block=True,
        jacobian_user_scaling={
            ("molarEnthalpy", None): 1,
            ("specificHeatCapacityConstP", None): 1,
        },
    )
    m.eq_treated_properties = ReaktoroBlock(
        composition=m.treated_composition,
        temperature=m.precipitator_temperature,
        pressure=m.feed_pressure,  # assume all systems operate at same pressure - not
        pH=m.precipitation_properties[("pH", None)],
        outputs=m.treated_properties,
        aqueous_phase_activity_model="ActivityModelPitzer",
        mineral_phases=["Calcite", "Anhydrite"],
        gas_phases=["H2O(g)", "N2(g)"],
        gas_phase_activity_model="ActivityModelPengRobinson",
        database="SupcrtDatabase",  # need to specify new data base to use
        database_file="supcrtbl",  # need to specify specific data base file to use
        species_to_rkt_species_dict=translation_dict,
        convert_to_rkt_species=True,
        dissolve_species_in_reaktoro=False,
        jacobian_user_scaling={
            ("molarEnthalpy", None): 1,
            ("specificHeatCapacityConstP", None): 1,
        },
    )
    m.eq_cooled_treated_properties = ReaktoroBlock(
        composition=m.treated_composition,
        temperature=m.cooled_treated_temperature,
        pressure=m.feed_pressure,  # assume all systems operate at same pressure - not
        pH=m.precipitation_properties[("pH", None)],
        outputs=m.cooled_treated_properties,
        aqueous_phase_activity_model="ActivityModelPitzer",
        mineral_phases=["Calcite", "Anhydrite"],
        gas_phases=["H2O(g)", "N2(g)"],
        gas_phase_activity_model="ActivityModelPengRobinson",
        database="SupcrtDatabase",  # need to specify new data base to use
        database_file="supcrtbl",  # need to specify specific data base file to use
        species_to_rkt_species_dict=translation_dict,
        convert_to_rkt_species=True,
        dissolve_species_in_reaktoro=False,
        jacobian_user_scaling={
            ("molarEnthalpy", None): 1,
            ("specificHeatCapacityConstP", None): 1,
        },
        # presolve=True, # when solids are include, presolving can help with stability
    )
    scale_model(m)
    return m


def scale_model(m):
    for key in m.feed_composition:
        iscale.set_scaling_factor(
            m.feed_composition[key], 1 / m.feed_composition[key].value
        )
        iscale.set_scaling_factor(
            m.precipitator_composition[key], 1 / m.feed_composition[key].value
        )
        iscale.set_scaling_factor(
            m.sludge_composition[key], 1 / m.feed_composition[key].value
        )
        iscale.set_scaling_factor(
            m.treated_composition[key], 1 / m.feed_composition[key].value
        )
        iscale.constraint_scaling_transform(
            m.eq_sludge_composition[key], 1 / m.feed_composition[key].value
        )
        iscale.constraint_scaling_transform(
            m.eq_treated_composition[key], 1 / m.feed_composition[key].value
        )
        iscale.constraint_scaling_transform(
            m.eq_precipitator_composition[key], 1 / m.feed_composition[key].value
        )

    iscale.set_scaling_factor(
        m.precipitation_properties[("speciesAmount", "Calcite")], 1e5
    )
    iscale.set_scaling_factor(
        m.precipitation_properties[("speciesAmount", "Anhydrite")], 1e5
    )
    iscale.set_scaling_factor(
        m.precipitation_properties[("molarEnthalpy", None)], 1 / 1e4
    )
    iscale.set_scaling_factor(m.feed_properties[("molarEnthalpy", None)], 1 / 1e4)
    iscale.set_scaling_factor(m.treated_properties[("molarEnthalpy", None)], 1 / 1e4)
    iscale.set_scaling_factor(
        m.cooled_treated_properties[("molarEnthalpy", None)], 1 / 1e4
    )
    iscale.set_scaling_factor(m.feed_temperature, 1 / 100)
    iscale.set_scaling_factor(m.precipitator_temperature, 1 / 100)
    iscale.set_scaling_factor(m.cooled_treated_temperature, 1 / 100)
    iscale.set_scaling_factor(m.Q_heating, 1 / 1e4)
    iscale.set_scaling_factor(m.Q_recoverable, 1 / 1e4)
    iscale.constraint_scaling_transform(m.eq_Q_heating, 1 / 1e4)
    iscale.constraint_scaling_transform(m.eq_Q_recoverable, 1 / 1e4)


def initialize(m):
    """prop feed to precipitation comp"""
    for key in m.eq_precipitator_composition:
        calculate_variable_from_constraint(
            m.precipitator_composition[key], m.eq_precipitator_composition[key]
        )
    """ initialize feed and precipitaiton properties
    This will also get us initial precipitation amounts"""
    m.eq_feed_properties.initialize()
    m.eq_precipitation_properties.initialize()
    """ get sludge flow volume first """
    calculate_variable_from_constraint(
        m.sludge_composition["H2O"], m.eq_sludge_composition["H2O"]
    )

    """ we wrote lazy formulation and cant explicitly calculate 
    treated or sludge ion composition, lets for initialization assume 
    that treated comp is same as feed composition and use that to estimate 
    initial sludge comp"""

    for key, obj in m.feed_composition.items():
        if key == "H2O":
            calculate_variable_from_constraint(
                m.treated_composition[key], m.eq_treated_composition[key]
            )
        else:
            m.treated_composition[key].value = obj.value

    m.eq_treated_properties.initialize()
    m.eq_cooled_treated_properties.initialize()
    solve(m)


def setup_optimization(m):
    m.Q_heating.fix(165000)
    m.precipitator_temperature.unfix()


def solve(m):
    cy_solver = get_solver(solver="cyipopt-watertap")
    cy_solver.options["max_iter"] = 100
    # only enable if avaialbe !
    # cy_solver.options["linear_solver"] = "ma27"
    result = cy_solver.solve(m, tee=True)
    assert_optimal_termination(result)
    display_results(m)
    return result


def display_results(m):
    print("result")
    print(
        f"Feed temp {m.feed_temperature.value-273.15}, precipitator temp {m.precipitator_temperature.value-273.15}, treated temp {m.cooled_treated_temperature.value-273.15}."
    )
    print(
        f"""Feed vapor pressure {m.feed_properties[("vaporPressure", "H2O(g)")].value} (Pa)"""
    )
    print(
        f"""Precipitator vapor pressure {m.precipitation_properties[("vaporPressure", "H2O(g)")].value} (Pa),"""
    )
    print(
        f"""Treated vapor pressure {m.cooled_treated_properties[("vaporPressure", "H2O(g)")].value} (Pa)."""
    )
    print(
        f"Q heating {m.Q_heating.value/1000} kJ/s, Q recoverable {m.Q_recoverable.value/1000} kJ/s"
    )
    print(
        f"Specific heat input {m.Q_heating.value/1000/(m.precipitator_temperature.value-m.feed_temperature.value)/(55*18.015/1000)} kJ/K/kg"
    )
    print(
        f'Specific heat capacity feed {m.feed_properties[("specificHeatCapacityConstP", None)].value}, (J/K/kg) precipitator {m.precipitation_properties[("specificHeatCapacityConstP", None)].value}(J/K/kg)'
    )
    print(
        f'Calcite precipitation {m.precipitation_properties[("speciesAmount", "Calcite")].value} mol/s'
    )
    print(f'precipitator pH {m.precipitation_properties[("pH", None)].value}')


if __name__ == "__main__":
    main()

"""Interfaces with GROMACS."""
import math
from pathlib import Path
from typing import IO, TYPE_CHECKING, Dict, List, Set, Tuple, Union

import numpy as np
from openff.units import unit

from openff.interchange.components.mdtraj import (
    _iterate_angles,
    _iterate_impropers,
    _iterate_pairs,
    _iterate_propers,
    _OFFBioTop,
    _store_bond_partners,
)
from openff.interchange.exceptions import UnsupportedExportError
from openff.interchange.models import PotentialKey, TopologyKey, VirtualSiteKey

if TYPE_CHECKING:
    from openff.interchange.components.interchange import Interchange


def to_gro(openff_sys: "Interchange", file_path: Union[Path, str], decimal=8):
    """
    Write a GROMACS coordinate (.gro) file.

    See https://manual.gromacs.org/documentation/current/reference-manual/file-formats.html#gro
    for more details, including the recommended C-style one-liners

    This code is partially copied from InterMol, see
    https://github.com/shirtsgroup/InterMol/tree/v0.1/intermol/gromacs

    """
    if isinstance(file_path, str):
        path = Path(file_path)
    if isinstance(file_path, Path):
        path = file_path

    # Explicitly round here to avoid ambiguous things in string formatting
    rounded_positions = np.round(openff_sys.positions, decimal)
    rounded_positions = rounded_positions.to(unit.nanometer).magnitude

    n = decimal

    n_particles = openff_sys.positions.shape[0]

    typemap = _build_typemap(openff_sys)
    virtual_site_map = _build_virtual_site_map(openff_sys)
    n_particles += len(virtual_site_map)

    with open(path, "w") as gro:
        gro.write("Generated by OpenFF\n")
        gro.write(f"{n_particles}\n")
        for atom in openff_sys.topology.mdtop.atoms:
            res = atom.residue
            residue_idx = (res.index + 1) % 100000
            # TODO: After topology refactor, ensure this matches residue names
            # in the topology file (unsure if this is necessary?)
            residue_name = res.name[:5]
            atom_name = typemap[atom.index]
            atom_index = (atom.index + 1) % 100000
            # TODO: Make sure these are in nanometers
            gro.write(
                f"%5d%-5s%5s%5d%{n+5}.{n}f%{n+5}.{n}f%{n+5}.{n}f\n"
                % (
                    residue_idx,
                    residue_name,
                    atom_name,
                    atom_index,
                    rounded_positions[atom.index, 0],
                    rounded_positions[atom.index, 1],
                    rounded_positions[atom.index, 2],
                )
            )

        for virtual_site_key in virtual_site_map:
            atom_name = "VS"
            residue_idx = 1
            residue_name = ""
            atom_index = virtual_site_map[virtual_site_key]
            gro.write(
                f"%5d%-5s%5s%5d%{n+5}.{n}f%{n+5}.{n}f%{n+5}.{n}f\n"
                % (
                    residue_idx,
                    residue_name,
                    atom_name,
                    atom_index,
                    0.0,
                    0.0,
                    0.0,
                )
            )

        if openff_sys.box is None:
            box = 11 * np.eye(3)
        else:
            box = openff_sys.box.to(unit.nanometer).magnitude

        # Check for rectangular
        if (box == np.diag(np.diagonal(box))).all():
            for i in range(3):
                gro.write(f"{box[i, i]:11.7f}")
        else:
            for i in range(3):
                gro.write(f"{box[i, i]:11.7f}")
            for i in range(3):
                for j in range(3):
                    if i != j:
                        gro.write(f"{box[i, j]:11.7f}")

        gro.write("\n")


def from_gro(file_path: Union[Path, str]) -> "Interchange":
    """Read coordinates and box information from a GROMACS GRO (.gro) file."""
    if isinstance(file_path, str):
        path = Path(file_path)
    if isinstance(file_path, Path):
        path = file_path

    # Infer coordinate precision
    def _infer_coord_precision(file_path: Union[Path, str]) -> int:
        """
        Infer decimal precision of coordinates by parsing periods in atoms lines.
        """
        with open(file_path) as file_in:
            file_in.readline()
            file_in.readline()
            atom_line = file_in.readline()
            period_indices = [i for i, x in enumerate(atom_line) if x == "."]
            spacing_between_periods = period_indices[-1] - period_indices[-2]
            precision = spacing_between_periods - 5
            return precision

    precision = _infer_coord_precision(file_path)
    coordinate_width = precision + 5
    # Column numbers in file separating x, y, z coords of each atom.
    # Default (3 decimals of precision -> 8 columns) are 20, 28, 36, 44
    coordinate_columns = [
        20,
        20 + coordinate_width,
        20 + 2 * coordinate_width,
        20 + 3 * coordinate_width,
    ]

    with open(path) as gro_file:
        # Throe away comment / name line
        gro_file.readline()
        n_atoms = int(gro_file.readline())

        unitless_coordinates = np.zeros((n_atoms, 3))
        for coordinate_index in range(n_atoms):
            line = gro_file.readline()
            _ = int(line[:5])  # residue_index
            _ = line[5:10]  # residue_name
            _ = line[10:15]  # atom_name
            _ = int(line[15:20])  # atom_index
            x = float(line[coordinate_columns[0] : coordinate_columns[1]])
            y = float(line[coordinate_columns[1] : coordinate_columns[2]])
            z = float(line[coordinate_columns[2] : coordinate_columns[3]])
            unitless_coordinates[coordinate_index] = np.array([x, y, z])

        coordinates = unitless_coordinates * unit.nanometer

        box_line = gro_file.readline()

        parsed_box = [float(val) for val in box_line.split()]

        if len(parsed_box) == 3:
            box = parsed_box * np.eye(3) * unit.nanometer

        from openff.interchange.components.interchange import Interchange

        interchange = Interchange()
        interchange.box = box
        interchange.positions = coordinates

        return interchange


def to_top(openff_sys: "Interchange", file_path: Union[Path, str]):
    """
    Write a GROMACS topology (.top) file.

    See https://manual.gromacs.org/documentation/current/reference-manual/file-formats.html#top
    for more details.

    This code is partially copied from InterMol, see
    https://github.com/shirtsgroup/InterMol/tree/v0.1/intermol/gromacs

    """
    if isinstance(file_path, str):
        path = Path(file_path)
    if isinstance(file_path, Path):
        path = file_path

    with open(path, "w") as top_file:
        top_file.write("; Generated by OpenFF Interchange\n")
        _write_top_defaults(openff_sys, top_file)
        typemap = _build_typemap(openff_sys)
        virtual_site_map = _build_virtual_site_map(openff_sys)
        _write_atomtypes(openff_sys, top_file, typemap, virtual_site_map)
        # TODO: Write [ nonbond_params ] section

        # TODO: De-duplicate based on molecules
        # TODO: Handle special case of water
        _write_moleculetype(top_file)
        _write_atoms(top_file, openff_sys, typemap, virtual_site_map)
        _write_valence(top_file, openff_sys)
        _write_virtual_sites(
            top_file,
            openff_sys,
            virtual_site_map,
        )
        _write_system(top_file, openff_sys)


def from_top(top_file: IO, gro_file: IO):
    """Read the contents of a GROMACS Topology (.top) file."""
    import mdtraj as md
    from intermol.forces import HarmonicAngle, HarmonicBond
    from intermol.gromacs.gromacs_parser import GromacsParser
    from openff.units.openmm import from_openmm

    from openff.interchange.components.base import (
        BaseAngleHandler,
        BaseBondHandler,
        BaseElectrostaticsHandler,
        BasevdWHandler,
    )
    from openff.interchange.components.interchange import Interchange
    from openff.interchange.components.potentials import Potential

    intermol_system = GromacsParser(top_file, gro_file).read()

    interchange = Interchange()

    interchange.box_vectors = intermol_system.box_vector
    interchange.positions = from_openmm([a.position for a in intermol_system.atoms])

    vdw_handler = BasevdWHandler(
        scale_14=intermol_system.lj_correction,
        mixing_rule=intermol_system.combination_rule,
    )

    if vdw_handler.mixing_rule == "Multiply-Sigeps":
        vdw_handler.mixing_rule = "geometric"

    electrostatics_handler = BaseElectrostaticsHandler(
        scale_14=intermol_system.coulomb_correction
    )

    bond_handler = BaseBondHandler()
    angle_handler = BaseAngleHandler()

    # TODO: Store atomtypes on a minimal topology, not as a list
    atomtypes: List = [atom.atomtype[0] for atom in intermol_system.atoms]

    topology = md.Topology()
    default_chain = topology.add_chain()
    default_residue = topology.add_residue(name="FOO", chain=default_chain)

    for atom in intermol_system.atoms:
        topology.add_atom(
            name=atom.atomtype[0],
            element=md.element.Element.getByMass(atom.mass[0]._value),
            residue=default_residue,
            serial=atom.index - 1,
        )
        topology_key = TopologyKey(atom_indices=(atom.index - 1,))
        vdw_key = PotentialKey(id=atom.atomtype[0], associated_handler="vdW")
        electrostatics_key = PotentialKey(
            id=atom.atomtype[0], associated_handler="Electrostatics"
        )

        # Intermol has an abstraction layer for multiple states, though only one is implemented
        charge = from_openmm(atom.charge[0])
        sigma = atom.sigma[0]
        epsilon = atom.epsilon[0]

        vdw_handler.slot_map[topology_key] = vdw_key
        electrostatics_handler.slot_map[topology_key] = electrostatics_key

        vdw_handler.potentials[vdw_key] = Potential(
            parameters={"sigma": sigma, "epsilon": epsilon}
        )
        electrostatics_handler.potentials[electrostatics_key] = Potential(
            parameters={"charge": charge}
        )

    for molecule_type in intermol_system.molecule_types.values():
        for bond_force in molecule_type.bond_forces:
            if type(bond_force) != HarmonicBond:
                raise Exception

            topology.add_bond(
                atom1=topology._atoms[bond_force.atom1 - 1],
                atom2=topology._atoms[bond_force.atom2 - 1],
            )

            topology_key = TopologyKey(
                atom_indices=(val - 1 for val in [bond_force.atom1, bond_force.atom2]),
            )
            potential_key = PotentialKey(
                id=f"{atomtypes[bond_force.atom1-1]}-{atomtypes[bond_force.atom2-1]}",
                associated_handler="Bonds",
            )

            bond_handler.slot_map[topology_key] = potential_key

            if potential_key not in bond_handler:
                potential = Potential(
                    parameters={
                        "k": from_openmm(bond_force.k),
                        "length": from_openmm(bond_force.length),
                    }
                )

                bond_handler.potentials[potential_key] = potential

        for angle_force in molecule_type.angle_forces:
            if type(angle_force) != HarmonicAngle:
                raise Exception

            topology_key = TopologyKey(
                atom_indices=(
                    val - 1
                    for val in [angle_force.atom1, angle_force.atom2, angle_force.atom3]
                ),
            )
            potential_key = PotentialKey(
                id=(
                    f"{atomtypes[angle_force.atom1-1]}-{atomtypes[angle_force.atom2-1]}-"
                    f"{atomtypes[angle_force.atom3-1]}"
                ),
                associated_handler="Angles",
            )

            angle_handler.slot_map[topology_key] = potential_key

            if potential_key not in angle_handler:
                potential = Potential(
                    parameters={
                        "k": from_openmm(angle_force.k),
                        "angle": from_openmm(angle_force.theta),
                    }
                )

                angle_handler.potentials[potential_key] = potential

    interchange.handlers["vdW"] = vdw_handler
    interchange.handlers["Electrostatics"] = electrostatics_handler
    interchange.handlers["Bonds"] = bond_handler
    interchange.handlers["Angles"] = angle_handler

    interchange.topology = _OFFBioTop(mdtop=topology)

    return interchange


def _write_top_defaults(openff_sys: "Interchange", top_file: IO):
    """Write [ defaults ] section."""
    top_file.write("[ defaults ]\n")
    top_file.write("; nbfunc\tcomb-rule\tgen-pairs\tfudgeLJ\tfudgeQQ\n")

    if "vdW" in openff_sys.handlers:
        nbfunc = 1
        scale_lj = openff_sys["vdW"].scale_14
        gen_pairs = "no"
        handler_key = "vdW"
    elif "Buckingham-6" in openff_sys.handlers:
        nbfunc = 2
        gen_pairs = "no"
        scale_lj = openff_sys["Buckingham-6"].scale_14
        handler_key = "Buckingham-6"
    else:
        raise UnsupportedExportError(
            "Could not find a handler for short-ranged vdW interactions that is compatible "
            "with GROMACS. Looked for handlers named `vdW` and `Buckingham-6`."
        )

    mixing_rule = openff_sys[handler_key].mixing_rule
    if mixing_rule == "lorentz-berthelot":
        comb_rule = 2
    elif mixing_rule == "geometric":
        comb_rule = 3
    elif mixing_rule == "buckingham" and handler_key == "Buckingham-6":
        # TODO: Not clear what the compatibility is here. `comb-rule` only applies to LJ terms.
        #  The documentation lists the combination rule for Buckingham potentials, but it does not
        #  seem like GROMACS will do this automatically, and needs to be implemented manully via
        #  [ nonbond_params ].
        # https://manual.gromacs.org/current/reference-manual/topologies/parameter-files.html#non-bonded-parameters
        # https://gromacs.bioexcel.eu/t/how-to-use-buckingham-function/1181/4
        comb_rule = 2
    else:
        raise UnsupportedExportError(
            f"Mixing rule `{mixing_rule} not compatible with GROMACS and/or not supported "
            "by current exporter. Supported values are `lorentez-berthelot` and `geometric`."
        )

    top_file.write(
        "{:6d}\t{:6d}\t{:6s} {:8.6f} {:8.6f}\n\n".format(
            nbfunc,
            comb_rule,
            gen_pairs,
            scale_lj,
            openff_sys.handlers["Electrostatics"].scale_14,
        )
    )


def _build_typemap(openff_sys: "Interchange") -> Dict:
    typemap = dict()
    elements: Dict[str, int] = dict()

    for atom in openff_sys.topology.mdtop.atoms:
        element_symbol = atom.element.symbol
        # TODO: Use this key to condense, see parmed.openmm._process_nobonded
        # parameters = _get_lj_parameters([*parameters.values()])
        # key = tuple([*parameters.values()])

        if element_symbol not in elements.keys():
            elements[element_symbol] = 1
        else:
            elements[element_symbol] += 1

        atom_type = f"{element_symbol}{elements[element_symbol]}"
        typemap[atom.index] = atom_type

    return typemap


def _build_virtual_site_map(interchange: "Interchange") -> Dict[VirtualSiteKey, int]:
    """
    Construct a mapping between the VirtualSiteKey objects found in a SMIRNOFFVirtualSiteHandler and particle indices.
    """
    virtual_site_topology_index_map: Dict[VirtualSiteKey, int] = dict()

    if "VirtualSites" not in interchange.handlers:
        return virtual_site_topology_index_map

    n_atoms = interchange.topology.mdtop.n_atoms

    for index, virtual_site_key in enumerate(
        interchange["VirtualSites"].slot_map.keys()
    ):
        virtual_site_topology_index_map[virtual_site_key] = n_atoms + 1 + index

    return virtual_site_topology_index_map


def _write_atomtypes(
    openff_sys: "Interchange",
    top_file: IO,
    typemap: Dict,
    virtual_site_map: Dict,
):
    """Write [ atomtypes ] section."""
    if "vdW" in openff_sys.handlers:
        if "Buckingham-6" in openff_sys.handlers:
            raise UnsupportedExportError(
                "Cannot mix 12-6 and Buckingham potentials in GROMACS"
            )
        else:
            _write_atomtypes_lj(openff_sys, top_file, typemap, virtual_site_map)
    else:
        if "Buckingham-6" in openff_sys.handlers:
            _write_atomtypes_buck(openff_sys, top_file, typemap)
        else:
            raise UnsupportedExportError("No vdW interactions found")


def _write_atomtypes_lj(
    openff_sys: "Interchange",
    top_file: IO,
    typemap: Dict,
    virtual_site_map: Dict,
):
    """Write the [ atomtypes ] section when all atoms use the LJ potential."""
    top_file.write("[ atomtypes ]\n")
    top_file.write(
        ";type, bondingtype, atomic_number, mass, charge, ptype, sigma, epsilon\n"
    )

    for atom_idx, atom_type in typemap.items():
        atom = openff_sys.topology.mdtop.atom(atom_idx)
        mass = atom.element.mass
        atomic_number = atom.element.atomic_number
        parameters = _get_lj_parameters(openff_sys, atom_idx)
        sigma = parameters["sigma"].to(unit.nanometer).magnitude
        epsilon = parameters["epsilon"].to(unit.Unit("kilojoule / mole")).magnitude
        # top.write('{0:<11s} {1:5s} {2:6d} {3:18.8f} {4:18.8f} {5:5s}'.format(
        top_file.write(
            "{:<11s} {:6s} {:6d} {:.16g} {:.16g} {:5s} {:.16g} {:.16g}\n".format(
                atom_type,  # atom type
                "XX",  # atom "bonding type", i.e. bond class
                atomic_number,
                mass,
                0.0,  # charge, overriden later in [ atoms ]
                "A",  # ptype
                sigma,
                epsilon,
            )
        )
        top_file.write("\n")

    for virtual_site_key in virtual_site_map:
        atom_type = "VS"
        atomic_number = 0
        mass = 0.0

        vdw_handler = openff_sys.handlers["vdW"]
        pot_key = vdw_handler.slot_map[virtual_site_key]
        parameters = vdw_handler.potentials[pot_key].parameters
        sigma = parameters["sigma"].to(unit.nanometer).magnitude
        epsilon = parameters["epsilon"].to(unit.Unit("kilojoule / mole")).magnitude

        top_file.write(
            "{:<11s} {:6d} {:.16g} {:.16g} {:5s} {:.16g} {:.16g}\n".format(
                atom_type,  # atom type
                # "XX",  # atom "bonding type", i.e. bond class
                atomic_number,
                mass,
                0.0,  # charge, overriden later in [ atoms ]
                "A",  # ptype
                sigma,
                epsilon,
            )
        )


def _write_atomtypes_buck(openff_sys: "Interchange", top_file: IO, typemap: Dict):
    """Write the [ atomtypes ] section when all atoms use the Buckingham-6 potential."""
    top_file.write("[ atomtypes ]\n")
    top_file.write(
        ";type, bondingtype, atomic_number, mass, charge, ptype, sigma, epsilon\n"
    )

    for atom_idx, atom_type in typemap.items():
        atom = openff_sys.topology.atom(atom_idx)
        parameters = _get_buck_parameters(openff_sys, atom_idx)
        a = parameters["A"].to(unit.Unit("kilojoule / mol")).magnitude
        b = parameters["B"].to(1 / unit.nanometer).magnitude
        c = parameters["C"].to(unit.Unit("kilojoule / mol * nanometer ** 6")).magnitude

        top_file.write(
            "{:<11s} {:6d} {:.16g} {:.16g} {:5s} {:.16g} {:.16g} {:.16g}".format(
                atom_type,  # atom type
                # "XX",  # atom "bonding type", i.e. bond class
                atom.atomic_number,
                atom.atom.mass._value,
                0.0,  # charge, overriden later in [ atoms ]
                "A",  # ptype
                a,
                b,
                c,
            )
        )
        top_file.write("\n")


def _write_moleculetype(top_file: IO):
    """Write the [ moleculetype ] section."""
    top_file.write("[ moleculetype ]\n")
    top_file.write("; Name\tnrexcl\n")
    top_file.write("MOL\t3\n\n")


def _write_atoms(
    top_file: IO,
    openff_sys: "Interchange",
    typemap: Dict,
    virtual_site_map: Dict,
):
    """Write the [ atoms ] and [ pairs ] sections for a molecule."""
    top_file.write("[ atoms ]\n")
    top_file.write(";num, type, resnum, resname, atomname, cgnr, q, m\n")

    charges = openff_sys.handlers["Electrostatics"].charges

    for atom in openff_sys.topology.mdtop.atoms:
        atom_idx = atom.index
        mass = atom.element.mass
        atom_type = typemap[atom.index]
        res_idx = atom.residue.index
        res_name = str(atom.residue)
        top_key = TopologyKey(atom_indices=(atom_idx,))
        charge = charges[top_key].m_as(unit.e)
        # TODO: Figure out why charge increments were applied as an array
        # to the anchor atom involved in a BondChargeVirtualSite?
        if type(charge) == np.ndarray:
            charge = charge[0]

        top_file.write(
            "{:6d} {:18s} {:6d} {:8s} {:8s} {:6d} "
            "{:18.8f} {:18.8f}\n".format(
                atom_idx + 1,
                atom_type,
                res_idx + 1,
                res_name,
                atom_type,
                atom_idx + 1,
                charge,
                mass,
            )
        )

    for virtual_site_key, index in virtual_site_map.items():
        atom_idx = index
        atom_type = "VS"
        res_idx = 1
        res_name = "1"
        charge
        charge_handler = openff_sys.handlers["Electrostatics"]
        charge = charge_handler.charges_with_virtual_sites[virtual_site_key].m_as(
            unit.e
        )
        mass = 0.0

        top_file.write(
            "{:6d} {:18s} {:6d} {:8s} {:8s} {:6d} "
            "{:18.8f} {:18.8f}\n".format(
                atom_idx,
                atom_type,
                res_idx,
                res_name,
                atom_type,
                atom_idx,
                charge,
                mass,
            )
        )

    top_file.write("[ pairs ]\n")
    top_file.write("; ai\taj\tfunct\n")

    _store_bond_partners(openff_sys.topology.mdtop)

    try:
        mixing_rule = openff_sys["vdW"].mixing_rule
        scale_lj = openff_sys["vdW"].scale_14
    except LookupError:
        mixing_rule = openff_sys["Buckingham-6"].mixing_rule
        scale_lj = openff_sys["Buckingham-6"].scale_14

    # Use a set to de-duplicate
    pairs: Set[Tuple] = {*_iterate_pairs(openff_sys.topology.mdtop)}
    for pair in pairs:
        indices = [a.index for a in pair]
        indices = sorted(indices)
        parameters1 = _get_lj_parameters(openff_sys, indices[0])
        sigma1 = parameters1["sigma"].to(unit.nanometer).magnitude
        epsilon1 = parameters1["epsilon"].to(unit.Unit("kilojoule / mole")).magnitude
        parameters2 = _get_lj_parameters(openff_sys, indices[1])
        sigma2 = parameters2["sigma"].to(unit.nanometer).magnitude
        epsilon2 = parameters2["epsilon"].to(unit.Unit("kilojoule / mole")).magnitude
        epsilon_mix = (epsilon1 * epsilon2) ** 0.5
        if mixing_rule == "lorentz-berthelot":
            sigma_mix = (sigma1 + sigma2) * 0.5
        elif mixing_rule == "geometric":
            sigma_mix = (sigma1 * sigma2) ** 0.5
        top_file.write(
            "{:7d} {:7d} {:6d} {:16g} {:16g}\n".format(
                indices[0] + 1,
                indices[1] + 1,
                1,
                sigma_mix,
                epsilon_mix * scale_lj,
            )
        )


def _write_virtual_sites(
    top_file: IO,
    openff_sys: "Interchange",
    virtual_site_map: Dict,
):
    if "VirtualSites" not in openff_sys.handlers:
        return

    virtual_site_handler = openff_sys["VirtualSites"]

    if not all(
        k.type in ["BondCharge", "MonovalentLonePair", "DivalentLonePair"]
        for k in virtual_site_handler.slot_map
    ):
        raise NotImplementedError("Only BondCharge virtual sites are implemented")

    started_virtual_sites2 = False
    started_virtual_sites3 = False
    # TODO: Cleaner implementation than filter + sort? Maybe split it up into each type
    # and do them sequentially?
    for virtual_site_key in sorted(
        (k for k in virtual_site_handler.slot_map.keys() if type(k) == VirtualSiteKey),
        key=lambda x: x.type,
    ):
        if virtual_site_key.type == "BondCharge":
            if not started_virtual_sites2:
                top_file.write("\n[ virtual_sites2 ]\n; site  ai  aj  funct   a\n")
                started_virtual_sites2 = True

            reference_atoms = virtual_site_key.atom_indices
            if len(reference_atoms) != 2:
                raise NotImplementedError

            virtual_site_index = virtual_site_map[virtual_site_key]
            atom1 = reference_atoms[0]
            atom2 = reference_atoms[1]
            func = 2

            distance = (
                virtual_site_handler.potentials[
                    virtual_site_handler.slot_map[virtual_site_key]
                ]
                .parameters["distance"]
                .m_as(unit.nanometer)
            )

            a = distance

            top_file.write(
                f"{virtual_site_index}\t\t{atom1+1}\t{atom2+1}\t{func}\t{a}\n"
            )

        if virtual_site_key.type == "MonovalentLonePair":
            if not started_virtual_sites3:
                top_file.write(
                    "\n[ virtual_sites3 ]\n; site  ai  aj  ak funct   a   b\n"
                )
                started_virtual_sites3 = True

            reference_atoms = tuple(sorted(virtual_site_key.atom_indices))
            if len(reference_atoms) != 3:
                raise NotImplementedError

            virtual_site_index = virtual_site_map[virtual_site_key]
            atom1 = reference_atoms[0]
            atom2 = reference_atoms[1]
            atom3 = reference_atoms[2]
            func = 3  # "3fad"

            out_of_plane_angle = (
                virtual_site_handler.potentials[
                    virtual_site_handler.slot_map[virtual_site_key]
                ]
                .parameters["outOfPlaneAngle"]
                .m_as(unit.radian)
            )

            if out_of_plane_angle != 0.0:
                raise NotImplementedError(
                    "Unclear how to do MonovalentLonePair virtual sites with GROMACS"
                )

            distance = (
                virtual_site_handler.potentials[
                    virtual_site_handler.slot_map[virtual_site_key]
                ]
                .parameters["distance"]
                .m_as(unit.nanometer)
            )

            in_plane_angle = (
                virtual_site_handler.potentials[
                    virtual_site_handler.slot_map[virtual_site_key]
                ]
                .parameters["inPlaneAngle"]
                .m_as(unit.degree)
            )

            in_plane_angle_transformed = 180 - in_plane_angle

            top_file.write(
                f"{virtual_site_index}\t\t{atom1 + 1}\t{atom2 + 1}\t{atom3 + 1}\t"
                f"{func}\t{in_plane_angle_transformed}\t{distance}\n"
            )

        if virtual_site_key.type == "DivalentLonePair":
            if not started_virtual_sites3:
                top_file.write(
                    "\n[ virtual_sites3 ]\n; site  ai  aj  ak funct   a   b\n"
                )
                started_virtual_sites3 = True

            # TODO: Cannot sort here. Atom ordering implies "chirality" of virtual sites,
            #  i.e. which side of a 5-site water each lone pair particle should go.
            reference_atoms = tuple(sorted(virtual_site_key.atom_indices))
            if len(reference_atoms) != 3:
                raise NotImplementedError

            virtual_site_index = virtual_site_map[virtual_site_key]
            atom1 = reference_atoms[0]
            atom2 = reference_atoms[1]
            atom3 = reference_atoms[2]
            func = 1

            bond1_key = TopologyKey(atom_indices=(atom1, atom2))
            bond1_length = (
                openff_sys["Bonds"]
                .potentials[openff_sys["Bonds"].slot_map[bond1_key]]
                .parameters["length"]
                .m_as(unit.nanometer)
            )

            bond2_key = TopologyKey(atom_indices=(atom1, atom3))
            bond2_length = (
                openff_sys["Bonds"]
                .potentials[openff_sys["Bonds"].slot_map[bond2_key]]
                .parameters["length"]
                .m_as(unit.nanometer)
            )

            if bond1_length != bond2_length:
                raise NotImplementedError

            angle_key = TopologyKey(atom_indices=(atom2, atom1, atom3))
            angle = (
                openff_sys["Angles"]
                .potentials[openff_sys["Angles"].slot_map[angle_key]]
                .parameters["angle"]
                .m_as(unit.radian)
            )

            distance = (
                virtual_site_handler.potentials[
                    virtual_site_handler.slot_map[virtual_site_key]
                ]
                .parameters["distance"]
                .m_as(unit.nanometer)
            )

            out_of_plane_angle = (
                virtual_site_handler.potentials[
                    virtual_site_handler.slot_map[virtual_site_key]
                ]
                .parameters["outOfPlaneAngle"]
                .m_as(unit.radian)
            )

            if out_of_plane_angle == 0:
                func = 1

                a = -1.0 * distance / (math.cos(angle / 2.0) * bond1_length) / 2.0

                top_file.write(
                    f"{virtual_site_index}\t\t{atom1+1}\t{atom2+1}\t{atom3+1}\t{func}\t{a}\t{a}\n"
                )

            else:
                func = 4
                a = (-1 * distance * math.cos(out_of_plane_angle)) / (
                    2 * bond1_length * math.cos(angle / 2)
                )
                c = (-1 * distance * math.sin(out_of_plane_angle)) / (
                    bond1_length ** 2 * math.sin(angle)
                )

                top_file.write(
                    f"{virtual_site_index}\t\t{atom1+1}\t{atom2+1}\t{atom3+1}\t{func}\t{a}\t{a}\t{c}\n"
                )

    top_file.write("\n[ exclusions ]\n")
    for virtual_site_key in virtual_site_handler.slot_map:
        parent_indices = virtual_site_key.atom_indices
        virtual_site_index = virtual_site_map[virtual_site_key]
        top_file.write(f"{virtual_site_index}\t")
        top_file.write("\t".join([str(i + 1) for i in parent_indices]))
        top_file.write("\n")

    top_file.write("\n")


def _write_valence(
    top_file: IO,
    openff_sys: "Interchange",
):
    """Write the [ bonds ], [ angles ], and [ dihedrals ] sections."""
    _write_bonds(top_file, openff_sys)
    _write_angles(top_file, openff_sys)
    _write_dihedrals(top_file, openff_sys)


def _write_bonds(top_file: IO, openff_sys: "Interchange"):
    if "Bonds" not in openff_sys.handlers.keys():
        return

    top_file.write("[ bonds ]\n")
    top_file.write("; ai\taj\tfunc\tr\tk\n")

    bond_handler = openff_sys.handlers["Bonds"]

    for bond in openff_sys.topology.mdtop.bonds:

        indices = tuple(sorted((bond.atom1.index, bond.atom2.index)))
        for top_key in bond_handler.slot_map:
            if top_key.atom_indices == indices:
                pot_key = bond_handler.slot_map[top_key]
            elif top_key.atom_indices == indices[::-1]:
                pot_key = bond_handler.slot_map[top_key]

        params = bond_handler.potentials[pot_key].parameters

        k = params["k"].m_as(unit.Unit("kilojoule / mole / nanometer ** 2"))
        length = params["length"].to(unit.nanometer).magnitude

        top_file.write(
            "{:7d} {:7d} {:4s} {:.16g} {:.16g}\n".format(
                indices[0] + 1,  # atom i
                indices[1] + 1,  # atom j
                str(1),  # bond type (functional form)
                length,
                k,
            )
        )

        del pot_key

    top_file.write("\n\n")


def _write_angles(top_file: IO, openff_sys: "Interchange"):
    if "Angles" not in openff_sys.handlers.keys():
        return

    _store_bond_partners(openff_sys.topology.mdtop)

    top_file.write("[ angles ]\n")
    top_file.write("; ai\taj\tak\tfunc\tr\tk\n")

    angle_handler = openff_sys.handlers["Angles"]

    for angle in _iterate_angles(openff_sys.topology.mdtop):
        indices = (
            angle[0].index,
            angle[1].index,
            angle[2].index,
        )
        for top_key in angle_handler.slot_map:
            if top_key.atom_indices == indices:
                pot_key = angle_handler.slot_map[top_key]

        params = angle_handler.potentials[pot_key].parameters
        k = params["k"].m_as(unit.Unit("kilojoule / mole / radian ** 2"))
        theta = params["angle"].to(unit.degree).magnitude

        top_file.write(
            "{:7d} {:7d} {:7d} {:4s} {:.16g} {:.16g}\n".format(
                indices[0] + 1,  # atom i
                indices[1] + 1,  # atom j
                indices[2] + 1,  # atom k
                str(1),  # angle type (functional form)
                theta,
                k,
            )
        )

    top_file.write("\n\n")


def _write_dihedrals(top_file: IO, openff_sys: "Interchange"):
    if "ProperTorsions" not in openff_sys.handlers:
        if "RBTorsions" not in openff_sys.handlers:
            if "ImproperTorsions" not in openff_sys.handlers:
                return

    _store_bond_partners(openff_sys.topology.mdtop)

    top_file.write("[ dihedrals ]\n")
    top_file.write(";    i      j      k      l   func\n")

    rb_torsion_handler = openff_sys.handlers.get("RBTorsions", [])
    proper_torsion_handler = openff_sys.handlers.get("ProperTorsions", [])
    improper_torsion_handler = openff_sys.handlers.get("ImproperTorsions", [])

    # TODO: Ensure number of torsions written matches what is expected
    for proper in _iterate_propers(openff_sys.topology.mdtop):
        if proper_torsion_handler:
            for top_key in proper_torsion_handler.slot_map:
                indices = tuple(a.index for a in proper)
                if top_key.atom_indices == indices:
                    pot_key = proper_torsion_handler.slot_map[top_key]
                    params = proper_torsion_handler.potentials[pot_key].parameters

                    k = params["k"].to(unit.Unit("kilojoule / mol")).magnitude
                    periodicity = int(params["periodicity"])
                    phase = params["phase"].to(unit.degree).magnitude
                    idivf = int(params["idivf"]) if "idivf" in params else 1
                    top_file.write(
                        "{:7d} {:7d} {:7d} {:7d} {:6d} {:16g} {:16g} {:7d}\n".format(
                            indices[0] + 1,
                            indices[1] + 1,
                            indices[2] + 1,
                            indices[3] + 1,
                            1,
                            phase,
                            k / idivf,
                            periodicity,
                        )
                    )
        # This should be `if` if a single quartet can be subject to both proper and RB torsions
        if rb_torsion_handler:
            for top_key in rb_torsion_handler.slot_map:
                indices = tuple(a.index for a in proper)
                if top_key.atom_indices == indices:
                    pot_key = rb_torsion_handler.slot_map[top_key]
                    params = rb_torsion_handler.potentials[pot_key].parameters

                    c0 = params["C0"].to(unit.Unit("kilojoule / mol")).magnitude
                    c1 = params["C1"].to(unit.Unit("kilojoule / mol")).magnitude
                    c2 = params["C2"].to(unit.Unit("kilojoule / mol")).magnitude
                    c3 = params["C3"].to(unit.Unit("kilojoule / mol")).magnitude
                    c4 = params["C4"].to(unit.Unit("kilojoule / mol")).magnitude
                    c5 = params["C5"].to(unit.Unit("kilojoule / mol")).magnitude

                    top_file.write(
                        "{:7d} {:7d} {:7d} {:7d} {:6d} "
                        "{:16g} {:16g} {:16g} {:16g} {:16g} {:16g} \n".format(
                            indices[0] + 1,
                            indices[1] + 1,
                            indices[2] + 1,
                            indices[3] + 1,
                            3,
                            c0,
                            c1,
                            c2,
                            c3,
                            c4,
                            c5,
                        )
                    )

    # TODO: Ensure number of torsions written matches what is expected
    for improper in _iterate_impropers(openff_sys.topology.mdtop):
        if improper_torsion_handler:
            for top_key in improper_torsion_handler.slot_map:
                indices = tuple(a.index for a in improper)
                if indices == top_key.atom_indices:
                    key = improper_torsion_handler.slot_map[top_key]
                    params = improper_torsion_handler.potentials[key].parameters

                    k = params["k"].to(unit.Unit("kilojoule / mol")).magnitude
                    periodicity = int(params["periodicity"])
                    phase = params["phase"].to(unit.degree).magnitude
                    idivf = int(params["idivf"])
                    top_file.write(
                        "{:7d} {:7d} {:7d} {:7d} {:6d} {:.16g} {:.16g} {:.16g}\n".format(
                            indices[0] + 1,
                            indices[1] + 1,
                            indices[2] + 1,
                            indices[3] + 1,
                            4,
                            phase,
                            k / idivf,
                            periodicity,
                        )
                    )


def _write_system(top_file: IO, openff_sys: "Interchange"):
    """Write the [ system ] section."""
    top_file.write("[ system ]\n")
    top_file.write("; name \n")
    top_file.write("System name\n\n")

    top_file.write("[ molecules ]\n")
    top_file.write("; Compound\tnmols\n")
    # TODO: Write molecules separately
    top_file.write("MOL\t1")

    top_file.write("\n")


def _get_lj_parameters(openff_sys: "Interchange", atom_idx: int) -> Dict:
    vdw_hander = openff_sys.handlers["vdW"]
    atom_key = TopologyKey(atom_indices=(atom_idx,))
    identifier = vdw_hander.slot_map[atom_key]
    potential = vdw_hander.potentials[identifier]
    parameters = potential.parameters

    return parameters


def _get_buck_parameters(openff_sys: "Interchange", atom_idx: int) -> Dict:
    buck_hander = openff_sys.handlers["Buckingham-6"]
    atom_key = TopologyKey(atom_indices=(atom_idx,))
    identifier = buck_hander.slot_map[atom_key]
    potential = buck_hander.potentials[identifier]
    parameters = potential.parameters

    return parameters

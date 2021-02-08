from pathlib import Path
from typing import IO, Dict

import ele
import numpy as np
from openff.toolkit.topology import FrozenMolecule, Topology

from openff.system import unit
from openff.system.components.system import System


def to_gro(openff_sys: System, file_path: Path):
    """
    Write a .gro file. See
    https://manual.gromacs.org/documentation/current/reference-manual/file-formats.html#gro
    for more details, including the recommended C-style one-liners

    This code is partially copied from InterMol, see
    https://github.com/shirtsgroup/InterMol/tree/v0.1/intermol/gromacs

    """
    with open(file_path, "w") as gro:
        gro.write("Generated by OpenFF System\n")
        gro.write(f"{openff_sys.positions.shape[0]}\n")
        for idx, atom in enumerate(openff_sys.topology.topology_atoms):  # type: ignore
            element_symbol = ele.element_from_atomic_number(atom.atomic_number).symbol
            # TODO: Make sure these are in nanometers
            pos = openff_sys.positions[idx].to(unit.nanometer).magnitude  # type:ignore
            gro.write(
                # If writing velocities:
                # "\n%5d%-5s%5s%5d%8.3f%8.3f%8.3f%8.4f%8.4f%8.4f" % (
                "%5d%-5s%5s%5d%8.3f%8.3f%8.3f\n"
                % (
                    1 % 100000,  # residue index
                    "FOO",  # residue name
                    element_symbol,  # atom name
                    (idx + 1) % 100000,
                    pos[0],
                    pos[1],
                    pos[2],
                )
            )

        # TODO: Ensure nanometers
        box = openff_sys.box.to(unit.nanometer).magnitude  # type: ignore
        # Check for rectangular
        if (box == np.diag(np.diagonal(box))).all():
            for i in range(3):
                gro.write("{:11.7f}".format(box[i, i]))
        else:
            for i in range(3):
                gro.write("{:11.7f}".format(box[i, i]))
            for i in range(3):
                for j in range(3):
                    if i != j:
                        gro.write("{:11.7f}".format(box[i, j]))

        gro.write("\n")


def to_top(openff_sys: System, file_path: Path):
    """
    Write a .gro file. See
    https://manual.gromacs.org/documentation/current/reference-manual/file-formats.html#top
    for more details.

    This code is partially copied from InterMol, see
    https://github.com/shirtsgroup/InterMol/tree/v0.1/intermol/gromacs

    """
    with open(file_path, "w") as top_file:
        top_file.write("; Generated by OpenFF System\n")
        _write_top_defaults(openff_sys, top_file)
        typemap = _write_atomtypes(openff_sys, top_file)
        # TODO: Write [ nonbond_params ] section
        molecule_map = _build_molecule_map(openff_sys.topology)
        for mol_name, mol_data in molecule_map.items():
            _write_moleculetype(top_file, mol_name)
            _write_atoms(top_file, mol_name, mol_data, openff_sys, typemap)
            _write_valence(top_file, mol_name, mol_data, openff_sys, typemap)
            # _write_valence(openff_sys, top_file)
        _write_system(top_file, molecule_map)


def _build_molecule_map(topology: "Topology") -> Dict:
    molecule_mapping = dict()
    counter = 0
    for ref_mol in topology.reference_molecules:
        if ref_mol.name == "":
            molecule_name = "MOL" + str(counter)
            counter += 1
        else:
            molecule_name = ref_mol.name
        num_ref_molecule = len(
            topology._reference_molecule_to_topology_molecules[ref_mol]
        )
        molecule_mapping.update(
            {
                molecule_name: {
                    "reference_molecule": ref_mol,
                    "n_mols": num_ref_molecule,
                }
            }
        )

    return molecule_mapping


def _write_top_defaults(openff_sys: System, top_file: IO):
    """Write [ defaults ] section"""
    top_file.write("[ defaults ]\n")
    top_file.write("; nbfunc\tcomb-rule\tgen-pairs\tfudgeLJ\tfudgeQQ\n")
    top_file.write(
        "{:6d}\t{:6s}\t{:6s} {:8.6f} {:8.6f}\n\n".format(
            # self.system.nonbonded_function,
            # self.lookup_gromacs_combination_rules[self.system.combination_rule],
            # self.system.genpairs,
            # self.system.lj_correction,
            # self.system.coulomb_correction,
            1,
            str(2),
            "yes",
            openff_sys.handlers["vdW"].scale_14,  # type: ignore
            openff_sys.handlers["Electrostatics"].scale_14,  # type: ignore
        )
    )


def _write_atomtypes(openff_sys: System, top_file: IO) -> Dict:
    """Write [ atomtypes ] section"""
    typemap = dict()
    elements: Dict[str, int] = dict()

    for atom_idx, atom in enumerate(openff_sys.topology.topology_atoms):  # type: ignore
        atomic_number = atom.atomic_number
        element_symbol = ele.element_from_atomic_number(atomic_number).symbol
        # TODO: Use this key to condense, see parmed.openmm._process_nobonded
        # parameters = _get_lj_parameters([*parameters.values()])
        # key = tuple([*parameters.values()])

        if element_symbol not in elements.keys():
            elements[element_symbol] = 0

        atom_type = f"{element_symbol}{elements[element_symbol]}"
        typemap[atom_idx] = atom_type

    top_file.write("[ atomtypes ]\n")
    top_file.write(
        ";type, bondingtype, atomic_number, mass, charge, ptype, sigma, epsilon\n"
    )

    for atom_idx, atom_type in typemap.items():
        atom = openff_sys.topology.atom(atom_idx)  # type: ignore
        element = ele.element_from_atomic_number(atom.atomic_number)
        parameters = _get_lj_parameters(openff_sys, atom_idx)
        sigma = parameters["sigma"].to(unit.nanometer).magnitude  # type: ignore
        epsilon = parameters["epsilon"].to(unit.Unit("kilojoule / mole")).magnitude  # type: ignore
        top_file.write(
            # "{0:<11s} {1:5s} {2:6d} {3:18.8f} {4:18.8f} {5:5s} {6:18.8e} {7:18.8e}".format(
            "{:<11s} {:6d} {:18.8f} {:18.8f} {:5s} {:18.8e} {:18.8e}".format(
                atom_type,  # atom type
                # "XX",  # atom "bonding type", i.e. bond class
                atom.atomic_number,
                element.mass,
                0.0,  # charge, overriden later in [ atoms ]
                "A",  # ptype
                sigma,
                epsilon,
            )
        )
        top_file.write("\n")

    return typemap


def _write_moleculetype(top_file: IO, mol_name: str, nrexcl: int = 3):
    """Write the [ moleculetype ] section for a single molecule"""
    top_file.write("[ moleculetype ]\n")
    top_file.write("; Name\tnrexcl\n")
    top_file.write(f"{mol_name}\t{nrexcl}\n\n")


def _write_atoms(
    top_file: IO,
    mol_name: str,
    mol_data: Dict,
    off_sys: System,
    typemap: Dict,
):
    """Write the [ atoms ] section for a molecule"""
    top_file.write("[ atoms ]\n")
    top_file.write(";num, type, resnum, resname, atomname, cgnr, q, m\n")

    ref_mol = mol_data["reference_molecule"]
    top_mol = off_sys.topology._reference_molecule_to_topology_molecules[ref_mol][0]  # type: ignore
    for atom_idx, atom in enumerate(top_mol.atoms):
        # atom in enumerate(ref_mol.atoms):  # type: ignore
        atom_type = typemap[atom_idx]
        element = ele.element_from_atomic_number(atom.atomic_number)
        mass = element.mass
        charge = (
            off_sys.handlers["Electrostatics"].charge_map[str((atom_idx,))].magnitude  # type: ignore
        )
        top_file.write(
            "{:6d} {:18s} {:6d} {:8s} {:8s} {:6d} "
            "{:18.8f} {:18.8f}\n".format(
                atom_idx + 1,
                atom_type,
                1,  # residue_index, always 1 while writing out per mol
                mol_name,  # residue_name,
                element.symbol,
                atom_idx + 1,  # cgnr
                charge,
                mass,
            )
        )


def _write_valence(
    top_file: IO, mol_name: str, mol_data: Dict, openff_sys: System, typemap: Dict
):
    """Write the [ bonds ], [ angles ], and [ dihedrals ] sections"""
    _write_bonds(top_file, openff_sys, mol_data["reference_molecule"])
    _write_angles(top_file, openff_sys, mol_data["reference_molecule"])
    _write_dihedrals(top_file, openff_sys, mol_data["reference_molecule"])


def _write_bonds(top_file: IO, openff_sys: System, ref_mol: FrozenMolecule):
    if len(openff_sys.handlers["Bonds"].potentials) == 0:
        return

    top_file.write("[ bonds ]\n")
    top_file.write("; ai\taj\tfunc\tr\tk\n")

    bond_handler = openff_sys.handlers["Bonds"]

    top_mol = openff_sys.topology._reference_molecule_to_topology_molecules[ref_mol][0]  # type: ignore

    for bond in top_mol.bonds:
        indices = tuple(sorted(a.topology_atom_index for a in bond.atoms))
        indices_as_str = str(indices)
        if indices_as_str in bond_handler.slot_map.keys():
            key = bond_handler.slot_map[indices_as_str]
        else:
            raise Exception("probably should have found parameters here ...")

        params = bond_handler.potentials[key].parameters

        k = params["k"].to(unit.Unit("kilojoule / mole / nanometer ** 2")).magnitude
        length = params["length"].to(unit.nanometer).magnitude

        top_file.write(
            "{:7d} {:7d} {:4s} {:18.8e} {:18.8e}\n".format(
                indices[0] + 1,  # atom i
                indices[1] + 1,  # atom j
                str(1),  # bond type (functional form)
                length,
                k,
            )
        )

    top_file.write("\n\n")


def _write_angles(top_file: IO, openff_sys: System, ref_mol: FrozenMolecule):
    if len(openff_sys.handlers["Angles"].potentials) == 0:
        return

    top_file.write("[ angles ]\n")
    top_file.write("; ai\taj\tak\tfunc\tr\tk\n")

    top_mol = openff_sys.topology._reference_molecule_to_topology_molecules[ref_mol][0]  # type: ignore

    angle_handler = openff_sys.handlers["Angles"]

    for angle in top_mol.angles:
        indices = tuple(a.topology_atom_index for a in angle)
        indices_as_str = str(indices)
        if indices_as_str in angle_handler.slot_map.keys():
            key = angle_handler.slot_map[indices_as_str]
        else:
            raise Exception

        params = angle_handler.potentials[key].parameters
        k = params["k"].to(unit.Unit("kilojoule / mole / radian ** 2")).magnitude
        theta = params["angle"].to(unit.degree).magnitude

        top_file.write(
            "{:7d} {:7d} {:7d} {:4s} {:18.8e} {:18.8e}\n".format(
                indices[0] + 1,  # atom i
                indices[1] + 1,  # atom j
                indices[2] + 1,  # atom k
                str(1),  # angle type (functional form)
                theta,
                k,
            )
        )

    top_file.write("\n\n")


def _write_dihedrals(top_file: IO, openff_sys: System, ref_mol: FrozenMolecule):
    if len(openff_sys.handlers["ProperTorsions"].potentials) == 0:
        if len(openff_sys.handlers["ImproperTorsions"].potentials) == 0:
            return

    top_file.write("[ dihedrals ]\n")
    top_file.write(";    i      j      k      l   func\n")

    top_mol = openff_sys.topology._reference_molecule_to_topology_molecules[ref_mol][0]  # type: ignore

    proper_torsion_handler = openff_sys.handlers["ProperTorsions"]
    improper_torsion_handler = openff_sys.handlers["ImproperTorsions"]

    # TODO: Ensure number of torsions written matches what is expected
    for proper in top_mol.propers:
        indices = tuple(a.topology_atom_index for a in proper)
        indices_as_str = str(indices)
        for torsion_key, key in proper_torsion_handler.slot_map.items():
            if indices_as_str == torsion_key.split("_")[0]:
                params = proper_torsion_handler.potentials[key].parameters

                k = params["k"].to(unit.Unit("kilojoule / mol")).magnitude
                periodicity = int(params["periodicity"])
                phase = params["phase"].to(unit.degree).magnitude
                idivf = int(params["idivf"])
                top_file.write(
                    "{:7d} {:7d} {:7d} {:7d} {:6d} {:18.8e} {:18.8e} {:7d}\n".format(
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

    # TODO: Ensure number of torsions written matches what is expected
    for improper in top_mol.impropers:

        indices = tuple(a.topology_atom_index for a in improper)
        indices_as_str = str(indices)
        for torsion_key, key in improper_torsion_handler.slot_map.items():
            if indices_as_str == torsion_key.split("_")[0]:
                key = improper_torsion_handler.slot_map[torsion_key]
                params = improper_torsion_handler.potentials[key].parameters

                k = params["k"].to(unit.Unit("kilojoule / mol")).magnitude
                periodicity = int(params["periodicity"])
                phase = params["phase"].to(unit.degree).magnitude
                idivf = int(params["idivf"])
                top_file.write(
                    "{:7d} {:7d} {:7d} {:7d} {:6d} {:18.8e} {:18.8e} {:18.8e}\n".format(
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


def _write_system(top_file: IO, molecule_map: Dict):
    """Write the [ system ] section"""
    top_file.write("[ system ]\n")
    top_file.write("; name \n")
    top_file.write("System name\n\n")

    top_file.write("[ molecules ]\n")
    top_file.write("; Compound\tnmols\n")
    for (
        mol_name,
        mol_data,
    ) in molecule_map.items():
        n_mols = mol_data["n_mols"]
        top_file.write(f"{mol_name}\t{n_mols}")

    top_file.write("\n")


def _get_lj_parameters(openff_sys: System, atom_idx: int) -> Dict:
    vdw_hander = openff_sys.handlers["vdW"]
    identifier = vdw_hander.slot_map[str((atom_idx,))]
    potential = vdw_hander.potentials[identifier]
    parameters = potential.parameters

    return parameters

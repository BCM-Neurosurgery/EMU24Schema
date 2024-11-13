from emu24.helper import *


def purge_patient(patient_name: str):
    """Purge all tables based on patient_id

    Args:
        patient_name (str): e.g. YFD
    """
    query = Patient() & f"emu_id='{patient_name}'"
    found = query.fetch()

    if len(found) == 0:
        raise KeyError(f"Did not find a patient with EMU id = {patient_name}")
    elif len(found) > 1:
        raise KeyError(f"Found multiple patients with this patient ID!")

    patient_id = found[0][0]
    print(patient_id)
    restriction = f"patient_id={patient_id}"

    (Patient & restriction).delete()


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(
        parents=[login_parser],
        description="""
            Helper script for purging the index database
            """,
        add_help=False,
    )
    arg_parser.add_argument(
        "--patient",
        type=str,
        help="Run the populate only for the patient with the given EMU identifier",
    )

    args = arg_parser.parse_args()
    connect(args)
    from emu24.schema import *

    purge_patient(args.patient)

import datajoint

from emu24.helper import *


def populate_all():
    print('Collecting matching NSP data chunks...')
    NSPChunks().populate(display_progress=True)

    print('Searching for task comments...')
    TaskComments().populate(display_progress=True)
    StartComments().populate(display_progress=True)
    TaskIDComments().populate(display_progress=True)
    StopComments().populate(display_progress=True)

    print('Stitching new data...')
    StitchedChunks().populate(display_progress=True)


def populate_patient(patient_name):

    query = Patient() & f"emu_id='{patient_name}'"
    found = query.fetch()

    if len(found) == 0:
        raise KeyError(f'Did not find a patient with EMU id = {patient_name}')
    elif len(found) > 1:
        raise KeyError(f'Found multiple patients with this patient ID!')

    patient_id = found[0][0]
    restriction = f'patient_id={patient_id}'

    print('Collecting matching NSP data chunks...')
    NSPChunks().populate(restriction, display_progress=True)

    print('Searching for task comments...')
    TaskComments().populate(restriction, display_progress=True)
    StartComments().populate(restriction, display_progress=True)
    TaskIDComments().populate(restriction, display_progress=True)
    StopComments().populate(restriction, display_progress=True)

    print('Stitching new data...')
    StitchedChunks().populate(restriction, display_progress=True)


if __name__ == '__main__':

    arg_parser = argparse.ArgumentParser(
        parents=[login_parser],
        description="""
            Helper script for populating the index database
            """,
        add_help=False
    )
    arg_parser.add_argument(
        '--patient',
        type=str,
        help='Run the populate only for the patient with the given EMU identifier'
    )

    args = arg_parser.parse_args()
    connect(args)
    from emu24.schema import *

    if args.patient:
        populate_patient(args.patient)
    else:
        populate_all()
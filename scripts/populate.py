import datajoint

from emu24.helper import *


def populate_all():
    print('Collecting matching NSP data chunks...')
    NSPChunks().populate(display_progress=True)

    print('Searching for task comments...')
    TaskComments().populate(display_progress=True, suppress_errors=True)
    StartComments().populate(display_progress=True, suppress_errors=True)
    TaskIDComments().populate(display_progress=True, suppress_errors=True)
    StopComments().populate(display_progress=True, suppress_errors=True)

    print('Stitching new data...')
    StitchedChunks().populate(display_progress=True, suppress_errors=True)


def populate_patient(patient_name):

    query = Patient() & f"emu_id='{patient_name}'"
    found = query.fetch()

    if len(found) == 0:
        raise KeyError(f'Did not find a patient with EMU id = {patient_name}')
    elif len(found) > 1:
        raise KeyError(f'Found multiple patients with this patient ID!')

    patient_id = found[0][0]
    print(patient_id)
    restriction = f'patient_id={patient_id}'

    print('Collecting matching NSP data chunks...')
    NSPChunks().populate(restriction, display_progress=True, suppress_errors=True)

    print('Searching for task comments...')
    TaskComments().populate(restriction, display_progress=True, suppress_errors=True)
    StartComments().populate(restriction, display_progress=True, suppress_errors=True)
    TaskIDComments().populate(restriction, display_progress=True, suppress_errors=True)
    StopComments().populate(restriction, display_progress=True, suppress_errors=True)

    print('Stitching new data...')
    StitchedChunks().populate(restriction, display_progress=True, suppress_errors=True)

def list_available_patients():
    patients = Patient().fetch('patient_id', 'emu_id', 'dob')
    print('Available patients:')
    for patient_id, emu_id, dob in zip(*patients):
        print(f'Patient ID: {patient_id}, EMU ID: {emu_id}, DOB: {dob}')

    admissions = Admission().fetch('admission_id', 'admission_date')
    print('Available admission:')
    for admission_id, admission_date in zip(*admissions):
        print(f'Admission ID: {admission_id}, Admission date: {admission_date}')


def list_available_nsp():
    # nevs = NEVChunks().fetch('chunk_id', 'nsp_id', 'nev_file')
    # print('Available nev:')
    # for chunk_id, nsp_id, nev_file in zip(*nevs):
    #     print(f'chunk_id: {chunk_id}, NSP ID: {nsp_id}, nev_file: {nev_file}')
    # Define the query to filter NSPChunks by patient_id
    query = NSPChunks & {'patient_id': "6"}
    
    # Fetch the results with the specified limit
    results = query.fetch('file', 'absolute_time', 'nev_file', 'ns3_file', 'ns5_file', limit=10, as_dict=True)
    
    key_source = NS3Chunks + NS5Chunks
    key_dict = key_source.fetch1('chunk_id', 'nsp_id', 'ns3_file', 'ns5_file')
    print(key_dict)

    print(results)
    return results


if __name__ == '__main__':

    arg_parser = argparse.ArgumentParser(
        parents=[make_login_parser()],
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

    arg_parser.add_argument(
        '--list-patients',
        action='store_true',
        help='List available patients and their EMU identifiers'
    )

    arg_parser.add_argument(
        '--list-nsps',
        action='store_true',
        help='List available nevs'
    )

    args = arg_parser.parse_args()
    connect(args)
    from emu24.schema import *

    if args.list_nsps:
        list_available_nsp()
    elif args.list_patients:
        list_available_patients()
    elif args.patient:
        populate_patient(args.patient)
    else:
        populate_all()
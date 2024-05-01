
print('Connecting to the database...')

from emu24.gen_helper import *
from emu24 import EMU24 as emu24


def make_admission(patient_pk):
    query = emu24.Admission & f'patient_id={patient_pk}'
    found = query.fetchall('admission_id')

    new_admission_pk = len(found)
    print('What date is the start of the admission?')
    date = input(" > ")

    emu24.Admission().insert1({
        'admission_id': new_admission_pk,
        'patient_id': patient_pk,
        'admission_date': date
    })


def make_patient():
    print('What is the EMU patient ID (ex: YAZ, YEY, etc)')
    emu_id = input(" > ")

    if emu_id == "":
        print("Empty input. Exiting...")
        exit()

    query = emu24.Patient & f'emu_id={emu_id}'
    found = query.fetch1('patient_id')

    if found is None:
        print("No patient found with this ID. Would you like to make a new patient?")
        response = input("(y/n) > ")
        if response == "n" or response.lower() == "no":
            make_patient()
        elif response == "":
            print("Empty input. Exiting...")

        print('Please enter the patient\'s date of birth')
        dob = input(" > ")

        patient_id = len(emu24.Patient())
        emu24.Patient().insert1({
            'patient_id': patient_id,
            'emu_id': emu_id,
            'dob': dob
        })
        print('Successfully made a new patient.')

    else:
        print('Found a patient with this ID')
        patient_id = found

    print('Would you like to register a new admission?')
    response = input("(y/n) > ")
    if response == "n" or "":
        print("Exiting")
        exit()
    else:
        make_admission(patient_id)


if __name__ == '__main__':
    make_patient()
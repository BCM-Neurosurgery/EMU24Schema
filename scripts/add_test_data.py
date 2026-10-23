import os

# This early connect call is required for connectivity  in the dev environment
from emu24.helper import *
from emu24.settings import environment
if environment == 'development':
    connect(username=os.environ.get('DJ_USER'), password=os.environ.get('DJ_PASSWORD'))
else:
    raise ValueError('Random data should only be used for development!')

from emu24.schema import Patient, Admission, StopComments, StartComments, StitchedChunks

PATIENT_NAME = 'EXP'
PATIENT_DOB = '1985-02-04'
ADMIT_DATE = '2024-02-04'


# Look for an existing patient with this ID
query = Patient() & f"emu_id='{PATIENT_NAME}'"
found = query.fetch()

if found.size == 0:
    patient_pk = len(Patient()) + 1
    Patient().insert1({
        'patient_id': patient_pk,
        'emu_id': PATIENT_NAME,
        'dob': PATIENT_DOB
    })
    print('Successfully made a new patient.')
else:
    patient_pk = found[0][0]


query = Admission & f"patient_id='{patient_pk}'"
found = query.fetch('admission_id')

admission_pk = found.size + 1
Admission().insert1({
    'admission_id': admission_pk,
    'patient_id': patient_pk,
    'admission_date': ADMIT_DATE
})
print('Successfully made a new admission.')


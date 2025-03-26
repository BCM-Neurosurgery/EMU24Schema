import os
import numpy as np

# This early connect call is required for connectivity  in the dev environment
from emu24.helper import *
from emu24.settings import environment
if environment == 'development' and __name__ == '__main__':
    connect(username=os.environ.get('DJ_USER'), password=os.environ.get('DJ_PASSWORD'))

from emu24.schema import Patient, StopComments, StartComments, StitchedChunks


NSP_IDS = [1, 2]



def dedupe_comment_table(table, patient_id, task_id, nsp_id, preference):

    id_str = f'patient_id={patient_id} and emu_id={task_id} and nsp_id={nsp_id}'
    matches = (table & id_str).fetch()

    # Check if any de-duplication needs to be done. In theory there should only be one result
    if len(matches) > 1:
        print(f'Found duplicates for: {id_str}')
        print(matches)

        timestamps = (table & id_str).fetch('timestamp')
        if preference == 'first':
            chosen_ts = max(timestamps)
        elif preference == 'last':
            chosen_ts = min(timestamps)
        else:
            raise ValueError(f'Invalid preference: {preference}')

        other_matches = [match for match in matches if match['timestamp'] != chosen_ts]
        for to_delete in other_matches:
            to_delete_str = id_str + f" and comment_id={to_delete['comment_id']}"
            (table & to_delete_str).delete()



if __name__ == '__main__':
    all_patients = Patient().fetch('patient_id')
    for patient in all_patients:

        all_task_ids = (StitchedChunks() & f'patient_id={patient}').fetch('emu_id')
        for task in all_task_ids:
            for nsp in NSP_IDS:

                dedupe_comment_table(StartComments, patient, task, nsp, preference='first')
                dedupe_comment_table(StopComments, patient, task, nsp, preference='last')


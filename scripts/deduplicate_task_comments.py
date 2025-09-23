import os
import numpy as np

# This early connect call is required for connectivity  in the dev environment
from emu24.helper import *
from emu24.settings import environment
if __name__ == '__main__':
    connect(username=os.environ.get('DJ_USER'), password=os.environ.get('DJ_PASSWORD'))

from emu24.schema import Patient, StopComments, StartComments, TaskComments, TaskIDComments, StitchedChunks


NSP_IDS = [1, 2]

def dedupe_comment(table, patient_id, task_id, nsp_id, preference, commit=False, no_confirm=False):
    """
    Ensure that there is only one comment in this table for this patient, task and nsp combination

    By default, will only print the data for the comments that would be deleted. Will only commit to the delete
    actions if commit is explicitly set to True

    :param table: datajoint table in which to search for duplicate comments (StartComments or StopComments)
    :param patient_id: numeric id of the patient in the database (as defined in Patent() table)
    :param task_id: emu number used to identify this task (ex: task EMU062 -> has task_id 62)
    :param nsp_id: numerid ID of the NSP for this data chunk (either 1 or 2)
    :param preference:
        - 'last': consider the comment with the largest timestamp to be the comment to keep
        - 'first': consider the comment with the smallest timestamp to be the comment to keep
    :param commit: boolean, whether to commit to deleting the data. False by default, will only print out duplicates
    :return:
    """


    id_str = f'patient_id={patient_id} and emu_id={task_id} and nsp_id={nsp_id}'
    matches = (table & id_str).fetch()

    if len(matches) == 0:
        print('No matches found: {}'.format(id_str))
        return

    # Check if any de-duplication needs to be done. In theory there should only be one result
    if len(matches) > 1:
        timestamps = (table & id_str).fetch('timestamp')
        if preference.lower() == 'first':
            chosen_ts = min(timestamps)
        elif preference.lower() == 'last':
            chosen_ts = max(timestamps)
        else:
            raise ValueError(f'Invalid preference: {preference}')

        chosen_comment = [match for match in matches if match['timestamp'] == chosen_ts][0]
        chosen_id_str = f"patient_id={patient_id} and comment_id={chosen_comment['comment_id']}"
        chosen_task_comment = (TaskComments & chosen_id_str).fetch()

        other_matches = [match for match in matches if match['timestamp'] != chosen_ts]
        for to_delete in other_matches:
            to_delete_str = f"patient_id={patient_id} and comment_id={to_delete['comment_id']}"
            taskid_comment = (TaskIDComments & id_str).fetch()
            task_comment = (TaskComments & to_delete_str).fetch()
            print(f'Comment slated for deletion from {table}: \n'
                  f'    {to_delete_str}\n'
                  f'    {taskid_comment[0]["comment"]} \n'
                  f'    This timestamp: {to_delete["timestamp"]}     chosen timestamp: {chosen_ts}\n'
                  f'    This comment_type: {task_comment[0]["type"]}    chosen type: {chosen_task_comment["type"]}\n')
            if commit:
                safemode = not no_confirm
                (TaskComments & to_delete_str).delete(safemode=safemode)  # Delete at the source to avoid re-population


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit', default=False, action='store_true')
    parser.add_argument('--noconfirm', default=False, action='store_true')
    args = parser.parse_args()

    all_patients = Patient().fetch('patient_id')
    for patient in all_patients:

        extant_task_ids = (StitchedChunks() & f'patient_id={patient}').fetch('emu_id')
        all_task_ids = list(range(1, max(extant_task_ids) + 1))
        for task in all_task_ids:
            for nsp in NSP_IDS:

                dedupe_comment(StartComments, patient, task, nsp, preference='first', commit=args.commit, no_confirm=args.noconfirm)
                dedupe_comment(StopComments, patient, task, nsp, preference='last', commit=args.commit, no_confirm=args.noconfirm)


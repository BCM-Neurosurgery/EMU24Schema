import json
from pathlib import Path

import datajoint as dj
import re
import os
import warnings
from emu24.settings import DATABASE_NAME, STITCHED_PATH
from brpylib import NsxFile
from pyNsXStitch.stitchers import StitchedNeVFile, StitchedNsXFile
from pyNsXStitch.helpers import get_all_nev_comments
import logging

# Configure logging
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        logging.FileHandler("/home/auto/CODE/emu/EMU24Schema/scripts/datajoint_computed_table.log"),
                        logging.StreamHandler()
                    ])

def get_emu_id(comment_text):
    """
    Extract the EMU ID number from the comment contents

    All comments linked to a task contain an EMU ID in the form 'EMU-####'
    """
    emu_match = re.search("EMU-([0-9]+)", comment_text)
    emu_id = int(emu_match.group(1), 10) if emu_match else 99999
    return emu_id


# Define the schema
schema = dj.schema(DATABASE_NAME)


# Define the tables
@schema
class Patient(dj.Manual):
    definition = """
    patient_id: int  # primary key
    ---
    dob: varchar(256) # secondary attribute
    emu_id: varchar(256)
    """


@schema
class Admission(dj.Manual):
    definition = """
    -> Patient
    admission_id: int  # primary key
    ---
    admission_date: varchar(256)  # secondary attribute
    """


@schema
class TOCInstance(dj.Manual):
    definition = """
    -> Admission
    toc_id: int
    ---
    base_file: varchar(256)  # secondary attribute
    """


@schema
class NEVChunks(dj.Manual):
    definition = """
    -> TOCInstance
    chunk_id: int  # primary key
    nsp_id: int
    ---
    nev_file: filepath@Ext_Chunk
    """


@schema
class NS3Chunks(dj.Manual):
    definition = """
    -> TOCInstance
    chunk_id: int  # primary key
    nsp_id: int
    ---
    ns3_file: filepath@Ext_Chunk
    """


@schema
class NS5Chunks(dj.Manual):
    definition = """
    -> TOCInstance
    chunk_id: int
    nsp_id: int
    ---
    ns5_file: filepath@Ext_Chunk
    """


@schema
class NSPChunks(dj.Computed):
    definition = """
    -> NEVChunks
    ---
    file: varchar(256)
    absolute_time: varchar(256)
    nev_file = NULL: filepath@Ext_Chunk
    ns3_file = NULL: filepath@Ext_Chunk
    ns5_file = NULL: filepath@Ext_Chunk
    """
    key_source = NS3Chunks + NS5Chunks

    def make(self, key):
        try:
            source = self.key_source

            # Debugging key source
            key_dict = (source & key).fetch1()

            # Get all the primary keys to look up the correct NeV file
            patient, admission = key_dict['patient_id'], key_dict['admission_id']
            toc, nsp, chunk = key_dict['toc_id'], key_dict['nsp_id'], key_dict['chunk_id']
            query = NEVChunks & (
                f"patient_id={patient} "
                f"AND admission_id={admission} "
                f"AND toc_id={toc} "
                f"AND nsp_id={nsp} "
                f"AND chunk_id={chunk}"
            )

            nev_file = query.fetch1('nev_file')

            # Load headers either from ns3 or ns5
            if key_dict['ns3_file'] is not None:
                nsx_fileobj = NsxFile(key_dict['ns3_file'])
                file_path = key_dict['ns3_file'][:-4]
            else:
                nsx_fileobj = NsxFile(key_dict['ns5_file'])
                file_path = key_dict['ns5_file'][:-4]
            key['file'] = Path(file_path).parts[-1]

            # Extract the absolute time
            header = nsx_fileobj.basic_header
            key['absolute_time'] = str(header['TimeOrigin'])

            key['nev_file'] = nev_file
            if key_dict['ns3_file'] is not None:
                key['ns3_file'] = key_dict['ns3_file']
            if key_dict['ns5_file'] is not None:
                key['ns5_file'] = key_dict['ns5_file']

            # Insert into database
            self.insert1(key)
        except dj.DataJointError as e:
            print(f"Failed to insert key {key}: {e}")
            with open('populate_errors.log', 'a') as f:
                f.write(f"Failed to insert key {key}: {e}\n")


@schema
class TaskComments(dj.Computed):
    definition = """
    -> NSPChunks
    comment_id: int  # primary key
    ---
    comment: varchar(256)  
    timestamp: bigint 
    type: varchar(256)  
    """

    comment_types = {
        '$TASKID': 'TASKID',
        '$TASKSTART': 'START',
        '$TASKSTOP': 'STOP',
        '$TASKKILL': 'KILL',
        '$TASKERROR': 'ERROR',
        '$TASKMETA': 'META',
    }

    def save_empty(self, max_id, key, file, reason=None):
        """
        Save a indicator that this chunk did not have any meaningful comments

        This is important to make sure the chunk is removed from the key source and we don't re-run the make
        function for this chunk every time we run populate()
        """
        reason = "This chunk did not contain any comments" if reason is None else reason
        key['comment_id'] = max_id
        key['timestamp'] = 0
        key['type'] = 'NOCOMMENT'
        key['comment'] = reason
        self.insert1(key)
        print(f'Saved NOCOMMENT for {file}')

    def make(self, key):

        # Prepare an auto-incrementing counter to ensure each comment has a unique ID
        max_id = len(TaskComments())

        # Get the file name, and extract all the comments out of that file
        file = (NSPChunks & key).fetch1('nev_file')
        df = get_all_nev_comments([file])

        # Check special case for if there are no comments in this file at all
        if df.empty:
            max_id += 1
            self.save_empty(max_id, key, file)
            return  # No need to continue here

        print(f'\n Found {len(df)} comment events in {file}')

        # Get the subset of all comments that are special command comments
        comments = df['Data'].str
        idx = comments.contains('$', regex=False)
        matched_entries = df[idx]

        # Check special case for if there are comments but no $TASK... style comments in this file
        if matched_entries.empty:
            max_id += 1
            self.save_empty(max_id, key, file, reason='No valid task comment commands in this file')
            return  # No need to continue here

        # Ignore duplicate comments (NSP issue) even if their timestamps are different
        unique_comments = matched_entries.drop_duplicates(subset=matched_entries.columns.difference(['timestamp']))

        for index, row in unique_comments.iterrows():
            max_id += 1
            key['comment_id'] = max_id
            key['timestamp'] = row['TimeStamps']

            # Extract the comment type and payload out of the comment string and map it to a known comment type
            raw_type, payload = re.search(r'(\$[A-Z]+) (.*)', row['Data']).groups()
            try:
                key['type'] = self.comment_types[raw_type]
            except KeyError:
                key['type'] = 'UNDEFINED'
            key['comment'] = payload

            try:
                self.insert1(key)
            except Exception as e:
                print(key)
                raise e


@schema
class StartComments(dj.Computed):
    definition = """
    -> TaskComments
    emu_id: int
    ---
    comment: varchar(256) 
    timestamp: bigint
    """
    key_source = TaskComments.proj(
        'type',
        comment='comment',
        timestamp='timestamp'
    ) & 'type = "START"'

    def make(self, key):

        comment, timestamp = (self.key_source & key).fetch1('comment', 'timestamp')
        key['comment'] = comment
        key['timestamp'] = timestamp
        key['emu_id'] = get_emu_id(comment)

        self.insert1(key)


@schema
class TaskIDComments(dj.Computed):
    definition = """
    -> TaskComments
    emu_id: int
    ---
    comment: varchar(256)
    timestamp: bigint
    task_name: varchar(256)
    """
    key_source = TaskComments.proj(
        'type',
        comment='comment',
        timestamp='timestamp'
    ) & 'type = "TASKID"'

    def make(self, key):
        comment, timestamp = (self.key_source & key).fetch1('comment', 'timestamp')
        key['comment'] = comment
        key['timestamp'] = timestamp
        key['emu_id'] = get_emu_id(comment)

        # Endeavor to parse the name of the task being performed our of the comment payload
        task_match = re.search("task-([a-zA-Z-0-9-]*)_", comment)
        task_name = task_match.group(1) if task_match else 'UNKNOWN'
        key['task_name'] = task_name

        self.insert1(key)


@schema
class StopComments(dj.Computed):
    definition = """
    -> TaskComments
    emu_id: int
    ---
    comment: varchar(256)
    timestamp: bigint
    """
    key_source = TaskComments.proj(
        'type',
        comment='comment',
        timestamp='timestamp'
    ) & ['type = "KILL"', 'type = "STOP"', 'type = "ERR"']

    def make(self, key):
        comment, timestamp = (self.key_source & key).fetch1('comment', 'timestamp')
        key['comment'] = comment
        key['timestamp'] = timestamp
        key['emu_id'] = get_emu_id(comment)

        self.insert1(key)


@schema
class StitchedChunks(dj.Computed):
    definition = """
    -> StartComments.proj('comment',start_fid='file_id',start_tid='comment_id',start_chunk='chunk_id')
    -> StopComments.proj('comment',stop_fid='file_id',stop_tid='comment_id',stop_chunk='chunk_id')
    ---
    start_filename: varchar(256)  # secondary attribute
    stop_filename: varchar(256)  # secondary attribute
    nev_file: filepath@Ext_Stitch
    ns3_file = NULL: filepath@Ext_Stitch
    ns5_file = NULL: filepath@Ext_Stitch
    """
    key_source = StartComments.proj(
        'emu_id',
        start_timestamp='timestamp',
        start_fid='file_id',
        start_tid='comment_id',
        start_chunk='chunk_id'
    ) * StopComments.proj(
        stop_timestamp='timestamp',
        stop_fid='file_id',
        stop_tid='comment_id',
        stop_chunk='chunk_id'
    )
    chunk_identifiers = ['patient_id', 'admission_id', 'toc_id', 'nsp_id', 'chunk_id']
    output = STITCHED_PATH

    def file_lookup(self, key, comment_id_col):
        """Lookup the file that a task is contained within"""
        comment_id = (self.key_source & key).fetch1(comment_id_col)
        chunk_keys = (TaskComments & f"comment_id={comment_id}").fetch1(*self.chunk_identifiers)
        chunk_id = chunk_keys[-1]  # Chunk_id is last because of order of identifiers

        nsp_lookup = [f'{name}={value}' for name, value in zip(self.chunk_identifiers, chunk_keys)]
        file_path = (NSPChunks & ' AND '.join(nsp_lookup)).fetch1('file')
        file_name = Path(file_path).name
        return file_name, chunk_id

    @staticmethod
    def do_stitching(key, out_path, all_nevs, all_nsxs, task_name, start_ts, end_ts):
        # Stitch the NEV files
        stitched_nev = StitchedNeVFile(all_nevs, start=start_ts, end=end_ts)
        full_nev_path = os.path.join(out_path, f'{task_name}.nev')
        if os.path.exists(full_nev_path):
            print(f'\nOverwriting old output file: {full_nev_path}')
            os.remove(full_nev_path)
        with open(full_nev_path, 'wb') as f:
            stitched_nev.write(f)
        key['nev_file'] = full_nev_path

        # Stitch and save the locations of the NSX files
        for filetype, files in all_nsxs.items():
            if not files:
                continue  # Skip filetypes that we don't have
            stitched_nsx = StitchedNsXFile(files, start=start_ts, end=end_ts, aggressive_concat=True)
            full_nsx_path = os.path.join(out_path, f'{task_name}.{filetype}')
            if os.path.exists(full_nsx_path):
                print(f'\nOverwriting old output file: {full_nsx_path}')
                try:
                    os.remove(full_nsx_path)
                except FileNotFoundError:
                    raise warnings.warn('File did not exist!')
            with open(full_nsx_path, 'wb+') as f:
                stitched_nsx.write(f)
            key[f'{filetype}_file'] = full_nsx_path

        return key

    def make(self, key):

        print(key)

        if (self.key_source & key).fetch1('emu_id') == 89:
            pass

        # Get the nev file associated with the start and stop comments
        start_file, start_chunk = self.file_lookup(key, 'start_tid')
        stop_file, stop_chunk = self.file_lookup(key, 'stop_tid')
        key['start_filename'] = start_file
        key['stop_filename'] = stop_file

        # Fetch the start and stop as NSP timestamps
        start_ts = (self.key_source & key).fetch1('start_timestamp')
        end_ts = (self.key_source & key).fetch1('stop_timestamp')

        # Determine the range of missing values and generate the missing filenames
        all_chunks = list(range(start_chunk, stop_chunk + 1))
        all_files = [f'{start_file[:-3]}{str(num).zfill(3)}' for num in all_chunks]

        # create list of missing entries
        all_nevs = []
        all_nsxs = {'ns3': [], 'ns5': []}
        for entry in all_files:
            nev, ns3, ns5 = (NSPChunks & 'file = "{}"'.format(entry)).fetch1('nev_file', 'ns3_file', 'ns5_file')
            all_nevs.append(nev)
            if ns3 is not None:
                all_nsxs['ns3'].append(ns3)
            if ns5 is not None:
                all_nsxs['ns5'].append(ns5)

        # Fetch any additional metadata needed for file naming
        patient = (Patient & f"patient_id='{key['patient_id']}'").fetch1('emu_id')
        id_comments = (
                TaskIDComments &
                f"emu_id = {key['emu_id']} "
                f"AND nsp_id = {key['nsp_id']} "
                f"AND patient_id = {key['patient_id']}"
        ).fetch()
        if len(id_comments):
            # Use the first TASKID comment payload to generate a name
            task_name = id_comments[0]['comment']
        else:
            # No suitable task comments found, use a auto-generated name
            task_name = f"EMU-{key['emu_id']}_subj-{patient}_task-UNKNOWN_NSP-{key['nsp_id']}"

        folder_name = '-'.join(task_name.split('_NSP-')[:-1])   # Drop the NSP id for the folder name
        out_path = os.path.join(self.output, patient, folder_name)
        os.makedirs(out_path, exist_ok=True)

        try:
            key = self.do_stitching(key, out_path, all_nevs, all_nsxs, task_name, start_ts, end_ts)
        except Exception as e:
            import sys, traceback, datetime
            exc_info = sys.exc_info()
            exception_info = traceback.format_exception(*exc_info)

            now = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            with open(os.path.join(out_path, f'error-{now}.txt'), 'w') as f:
                f.write('Error occured when processing:  ')
                f.write(json.dumps(key, indent=2))
                f.writelines(exception_info)

            warnings.warn("\n".join(exception_info))

        try:
            self.insert1(key, replace=True)
        except dj.DataJointError as e:
            warnings.warn(str(e))
